# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2013 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <http://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from django.shortcuts import render_to_response, get_object_or_404
from django.views.decorators.cache import cache_page
from weblate.trans import appsettings
from django.core.servers.basehttp import FileWrapper
from django.utils.translation import ugettext as _
import django.utils.translation
from django.template import RequestContext, loader
from django.http import (
    HttpResponse, HttpResponseRedirect, HttpResponseNotFound, Http404
)
from django.contrib import messages
from django.contrib.auth.decorators import (
    login_required, permission_required, user_passes_test
)
from django.contrib.auth.models import AnonymousUser
from django.db.models import Q, Count, Sum
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.core.urlresolvers import reverse
from django.contrib.sites.models import Site
from django.utils.safestring import mark_safe

from weblate.trans.models import (
    Project, SubProject, Translation, Unit, Suggestion, Check,
    Dictionary, Change, Comment, get_versions
)
from weblate.lang.models import Language
from weblate.trans.checks import CHECKS
from weblate.trans.forms import (
    TranslationForm, UploadForm, SimpleUploadForm, ExtraUploadForm, SearchForm,
    MergeForm, AutoForm, WordForm, DictUploadForm, ReviewForm, LetterForm,
    AntispamForm, CommentForm
)
from weblate.trans.util import join_plural
from weblate.accounts.models import Profile, send_notification_email
import weblate

from whoosh.analysis import StandardAnalyzer, StemmingAnalyzer
import datetime
import logging
import os.path
import json
import csv
from xml.etree import ElementTree
import urllib2


# See https://code.djangoproject.com/ticket/6027
class FixedFileWrapper(FileWrapper):
    def __iter__(self):
        self.filelike.seek(0)
        return self

logger = logging.getLogger('weblate')


def home(request):
    '''
    Home page of Weblate showing list of projects, stats
    and user links if logged in.
    '''
    projects = Project.objects.all_acl(request.user)
    acl_projects = projects
    if projects.count() == 1:
        projects = SubProject.objects.filter(project=projects[0])

    # Warn about not filled in username (usually caused by migration of
    # users from older system
    if not request.user.is_anonymous() and request.user.get_full_name() == '':
        messages.warning(
            request,
            _('Please set your full name in your profile.')
        )

    # Load user translations if user is authenticated
    usertranslations = None
    if request.user.is_authenticated():
        profile = request.user.get_profile()

        usertranslations = Translation.objects.filter(
            language__in=profile.languages.all()
        ).order_by(
            'subproject__project__name', 'subproject__name'
        )

    # Some stats
    top_translations = Profile.objects.order_by('-translated')[:10]
    top_suggestions = Profile.objects.order_by('-suggested')[:10]
    last_changes = Change.objects.filter(
        translation__subproject__project__in=acl_projects,
    ).order_by('-timestamp')[:10]

    return render_to_response('index.html', RequestContext(request, {
        'projects': projects,
        'top_translations': top_translations,
        'top_suggestions': top_suggestions,
        'last_changes': last_changes,
        'last_changes_rss': reverse('rss'),
        'usertranslations': usertranslations,
    }))


def show_checks(request):
    '''
    List of failing checks.
    '''
    allchecks = Check.objects.filter(
        ignore=False
    ).values('check').annotate(count=Count('id'))
    return render_to_response('checks.html', RequestContext(request, {
        'checks': allchecks,
        'title': _('Failing checks'),
    }))


def show_check(request, name):
    '''
    Details about failing check.
    '''
    try:
        check = CHECKS[name]
    except KeyError:
        raise Http404('No check matches the given query.')

    checks = Check.objects.filter(
        check=name, ignore=False
    ).values('project__slug').annotate(count=Count('id'))

    return render_to_response('check.html', RequestContext(request, {
        'checks': checks,
        'title': check.name,
        'check': check,
    }))


def show_check_project(request, name, project):
    '''
    Show checks failing in a project.
    '''
    prj = get_object_or_404(Project, slug=project)
    prj.check_acl(request)
    try:
        check = CHECKS[name]
    except KeyError:
        raise Http404('No check matches the given query.')
    units = Unit.objects.none()
    if check.target:
        langs = Check.objects.filter(
            check=name, project=prj, ignore=False
        ).values_list('language', flat=True).distinct()
        for lang in langs:
            checks = Check.objects.filter(
                check=name, project=prj, language=lang, ignore=False
            ).values_list('checksum', flat=True)
            res = Unit.objects.filter(
                checksum__in=checks,
                translation__language=lang,
                translation__subproject__project=prj,
                translated=True
            ).values(
                'translation__subproject__slug',
                'translation__subproject__project__slug'
            ).annotate(count=Count('id'))
            units |= res
    if check.source:
        checks = Check.objects.filter(
            check=name,
            project=prj,
            language=None,
            ignore=False
        ).values_list(
            'checksum', flat=True
        )
        for subproject in prj.subproject_set.all():
            lang = subproject.translation_set.all()[0].language
            res = Unit.objects.filter(
                checksum__in=checks,
                translation__language=lang,
                translation__subproject=subproject
            ).values(
                'translation__subproject__slug',
                'translation__subproject__project__slug'
            ).annotate(count=Count('id'))
            units |= res

    return render_to_response('check_project.html', RequestContext(request, {
        'checks': units,
        'title': '%s/%s' % (prj.__unicode__(), check.name),
        'check': check,
        'project': prj,
    }))


def show_check_subproject(request, name, project, subproject):
    '''
    Show checks failing in a subproject.
    '''
    subprj = get_object_or_404(
        SubProject,
        slug=subproject,
        project__slug=project
    )
    subprj.check_acl(request)
    try:
        check = CHECKS[name]
    except KeyError:
        raise Http404('No check matches the given query.')
    units = Unit.objects.none()
    if check.target:
        langs = Check.objects.filter(
            check=name,
            project=subprj.project,
            ignore=False
        ).values_list(
            'language', flat=True
        ).distinct()
        for lang in langs:
            checks = Check.objects.filter(
                check=name,
                project=subprj.project,
                language=lang,
                ignore=False
            ).values_list('checksum', flat=True)
            res = Unit.objects.filter(
                translation__subproject=subprj,
                checksum__in=checks,
                translation__language=lang,
                translated=True
            ).values(
                'translation__language__code'
            ).annotate(count=Count('id'))
            units |= res
    source_checks = []
    if check.source:
        checks = Check.objects.filter(
            check=name, project=subprj.project,
            language=None,
            ignore=False
        ).values_list('checksum', flat=True)
        lang = subprj.translation_set.all()[0].language
        res = Unit.objects.filter(
            translation__subproject=subprj,
            checksum__in=checks,
            translation__language=lang
        ).count()
        if res > 0:
            source_checks.append(res)
    return render_to_response(
        'check_subproject.html',
        RequestContext(request, {
            'checks': units,
            'source_checks': source_checks,
            'anychecks': len(units) + len(source_checks) > 0,
            'title': '%s/%s' % (subprj.__unicode__(), check.name),
            'check': check,
            'subproject': subprj,
        })
    )


def show_languages(request):
    return render_to_response('languages.html', RequestContext(request, {
        'languages': Language.objects.have_translation(),
        'title': _('Languages'),
    }))


def show_language(request, lang):
    obj = get_object_or_404(Language, code=lang)
    last_changes = Change.objects.filter(
        translation__language=obj
    ).order_by('-timestamp')[:10]
    dicts = Dictionary.objects.filter(
        language=obj
    ).values_list('project', flat=True).distinct()

    return render_to_response('language.html', RequestContext(request, {
        'object': obj,
        'last_changes': last_changes,
        'last_changes_rss': reverse('rss-language', kwargs={'lang': obj.code}),
        'dicts': Project.objects.filter(id__in=dicts),
    }))


def show_engage(request, project, lang=None):
    # Get project object
    obj = get_object_or_404(Project, slug=project)
    obj.check_acl(request)

    # Handle language parameter
    language = None
    if lang is not None:
        try:
            django.utils.translation.activate(lang)
        except:
            # Ignore failure on activating language
            pass
        try:
            language = Language.objects.get(code=lang)
        except Language.DoesNotExist:
            pass

    context = {
        'object': obj,
        'project': obj.name,
        'languages': obj.get_language_count(),
        'total': obj.get_total(),
        'percent': obj.get_translated_percent(language),
        'url': obj.get_absolute_url(),
        'language': language,
    }

    # Render text
    if language is None:
        status_text = _(
            '<a href="%(url)s">Translation project for %(project)s</a> '
            'currently contains %(total)s strings for translation and is '
            '<a href="%(url)s">being translated into %(languages)s languages'
            '</a>. Overall, these translations are %(percent)s%% complete.'
        )
    else:
        # Translators: line of text in engagement widget, please use your
        # language name instead of English
        status_text = _(
            '<a href="%(url)s">Translation project for %(project)s</a> into '
            'English currently contains %(total)s strings for translation and '
            'is %(percent)s%% complete.'
        )
        if 'English' in status_text:
            status_text = status_text.replace('English', language.name)

    context['status_text'] = mark_safe(status_text % context)

    return render_to_response('engage.html', RequestContext(request, context))


def show_project(request, project):
    obj = get_object_or_404(Project, slug=project)
    obj.check_acl(request)

    dicts = Dictionary.objects.filter(
        project=obj
    ).values_list(
        'language', flat=True
    ).distinct()

    last_changes = Change.objects.filter(
        translation__subproject__project=obj
    ).order_by('-timestamp')[:10]

    return render_to_response('project.html', RequestContext(request, {
        'object': obj,
        'dicts': Language.objects.filter(id__in=dicts),
        'last_changes': last_changes,
        'last_changes_rss': reverse(
            'rss-project',
            kwargs={'project': obj.slug}
        ),
    }))


def show_subproject(request, project, subproject):
    obj = get_object_or_404(SubProject, slug=subproject, project__slug=project)
    obj.check_acl(request)

    last_changes = Change.objects.filter(
        translation__subproject=obj
    ).order_by('-timestamp')[:10]

    return render_to_response('subproject.html', RequestContext(request, {
        'object': obj,
        'last_changes': last_changes,
        'last_changes_rss': reverse(
            'rss-subproject',
            kwargs={'subproject': obj.slug, 'project': obj.project.slug}
        ),
    }))


def review_source(request, project, subproject):
    '''
    Listing of source strings to review.
    '''
    obj = get_object_or_404(SubProject, slug=subproject, project__slug=project)
    obj.check_acl(request)

    if not obj.translation_set.exists():
        raise Http404('No translation exists in this subproject.')

    # Grab first translation in subproject
    # (this assumes all have same source strings)
    source = obj.translation_set.all()[0]

    # Grab search type and page number
    rqtype = request.GET.get('type', 'all')
    limit = request.GET.get('limit', 50)
    page = request.GET.get('page', 1)

    # Fiter units
    sources = source.unit_set.filter_type(rqtype, source)

    paginator = Paginator(sources, limit)

    try:
        sources = paginator.page(page)
    except PageNotAnInteger:
        # If page is not an integer, deliver first page.
        sources = paginator.page(1)
    except EmptyPage:
        # If page is out of range (e.g. 9999), deliver last page of results.
        sources = paginator.page(paginator.num_pages)

    return render_to_response('source-review.html', RequestContext(request, {
        'object': obj,
        'source': source,
        'sources': sources,
        'title': _('Review source strings in %s') % obj.__unicode__(),
    }))


def show_source(request, project, subproject):
    '''
    Show source strings summary and checks.
    '''
    obj = get_object_or_404(SubProject, slug=subproject, project__slug=project)
    obj.check_acl(request)
    if not obj.translation_set.exists():
        raise Http404('No translation exists in this subproject.')

    # Grab first translation in subproject
    # (this assumes all have same source strings)
    source = obj.translation_set.all()[0]

    return render_to_response('source.html', RequestContext(request, {
        'object': obj,
        'source': source,
        'title': _('Source strings in %s') % obj.__unicode__(),
    }))


def show_translation(request, project, subproject, lang):
    obj = get_object_or_404(
        Translation,
        language__code=lang,
        subproject__slug=subproject,
        subproject__project__slug=project,
        enabled=True
    )
    obj.check_acl(request)
    last_changes = Change.objects.filter(
        translation=obj
    ).order_by('-timestamp')[:10]

    # Check locks
    obj.is_locked(request)

    # How much is user allowed to configure upload?
    if request.user.has_perm('trans.author_translation'):
        form = ExtraUploadForm()
    elif request.user.has_perm('trans.overwrite_translation'):
        form = UploadForm()
    else:
        form = SimpleUploadForm()

    # Is user allowed to do automatic translation?
    if request.user.has_perm('trans.automatic_translation'):
        autoform = AutoForm(obj)
    else:
        autoform = None

    # Search form for everybody
    search_form = SearchForm()

    # Review form for logged in users
    if request.user.is_anonymous():
        review_form = None
    else:
        review_form = ReviewForm(
            initial={
                'date': datetime.date.today() - datetime.timedelta(days=31)
            }
        )

    return render_to_response('translation.html', RequestContext(request, {
        'object': obj,
        'form': form,
        'autoform': autoform,
        'search_form': search_form,
        'review_form': review_form,
        'last_changes': last_changes,
        'last_changes_rss': reverse(
            'rss-translation',
            kwargs={
                'lang': obj.language.code,
                'subproject': obj.subproject.slug,
                'project': obj.subproject.project.slug
            }
        ),
    }))


def not_found(request):
    '''
    Error handler showing list of available projects.
    '''
    template = loader.get_template('404.html')
    return HttpResponseNotFound(
        template.render(RequestContext(request, {
            'request_path': request.path,
            'title': _('Page Not Found'),
            'projects': Project.objects.all_acl(request.user),
        }
    )))


def about(request):
    context = {}
    versions = get_versions()
    totals =  Profile.objects.aggregate(Sum('translated'), Sum('suggested'))
    total_strings = 0
    for project in SubProject.objects.iterator():
        try:
            total_strings += project.translation_set.all()[0].total
        except Translation.DoesNotExist:
            pass
    context['title'] = _('About Weblate')
    context['total_translations'] = totals['translated__sum']
    context['total_suggestions'] = totals['suggested__sum']
    context['total_users'] = Profile.objects.count()
    context['total_strings'] = total_strings
    context['total_languages'] = Language.objects.filter(
        translation__total__gt=0
    ).distinct().count()
    context['total_checks'] = Check.objects.count()
    context['ignored_checks'] = Check.objects.filter(ignore=True).count()
    context['versions'] = versions

    return render_to_response('about.html', RequestContext(request, context))


def data_root(request):
    site = Site.objects.get_current()
    return render_to_response('data-root.html', RequestContext(request, {
        'site_domain': site.domain,
        'api_docs': weblate.get_doc_url('api', 'exports'),
        'rss_docs': weblate.get_doc_url('api', 'rss'),
        'projects': Project.objects.all_acl(request.user),
    }))


def data_project(request, project):
    obj = get_object_or_404(Project, slug=project)
    obj.check_acl(request)
    site = Site.objects.get_current()
    return render_to_response('data.html', RequestContext(request, {
        'object': obj,
        'site_domain': site.domain,
        'api_docs': weblate.get_doc_url('api', 'exports'),
        'rss_docs': weblate.get_doc_url('api', 'rss'),
    }))

