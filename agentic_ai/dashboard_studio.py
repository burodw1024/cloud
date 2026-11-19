"""Authenticated dashboard preview, publication journal and cleanup endpoints."""
import json
import re
import uuid

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect

from .api import _can_replay_result
from .models import DashboardExport
from .report_catalog import CATALOG, DESIGNS
from .superset_publisher import PublishError, configuration, specification, publish, remove


def permitted(user):
    return (user.is_authenticated and user.is_active and user.is_staff
            and user.has_perm('agentic_ai.publish_dashboard'))


def response(body, status=200):
    reply = JsonResponse(body, status=status)
    reply['Cache-Control'] = 'no-store'
    return reply


def source_report(request, token=None):
    token = request.session.get('dashboard_report_token') if token is None else token
    valid = isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', token)
    saved = cache.get('agentic:summary:' + token) if valid else None
    if not saved or saved['user_id'] != request.user.pk:
        raise PublishError('Run a report first. The displayed report snapshot expires after 30 minutes.')
    if not _can_replay_result(request.user, saved):
        raise PermissionError('Report access is no longer permitted.')
    return {**saved, 'report_token': token}


def current_authorization(user_id, source):
    user = get_user_model().objects.get(pk=user_id)
    return permitted(user) and _can_replay_result(user, source)


def binding(config):
    result = {key: config[key] for key in ('URL', 'DATABASE_ALIAS', 'DATABASE_ID', 'SCHEMA', 'OWNER_ID')}
    if config.get('SNAPSHOT_DB'):
        result['database'] = {key: config['SNAPSHOT_DB'].get(key) for key in ('host','port','dbname','user')}
    return result


@csrf_protect
def studio(request):
    if not permitted(request.user):
        return response({'ok': False, 'error': 'Dashboard publishing permission required.'}, 403)
    if request.method != 'GET':
        return response({'ok': False, 'error': 'GET required.'}, 405)
    token = request.GET.get('report')
    if token is not None:
        try:
            source_report(request, token)
        except PublishError as exc:
            return response({'ok': False, 'error': str(exc)}, 400)
        except PermissionError as exc:
            return response({'ok': False, 'error': str(exc)}, 403)
    from .reporting_config import workspace_settings
    readiness = 'Ready to create private dashboards.'
    ready = True
    try:
        configuration(request.user)
    except PublishError as exc:
        readiness = str(exc)
        ready = False
    result = render(request, 'agentic_ai/dashboard_studio.html', {
        'readiness': readiness, 'publishing_ready':ready, 'report_token': token if token is not None else request.session.get('dashboard_report_token', ''),
        'reporting':workspace_settings(request.user), 'catalog': CATALOG, 'exports': DashboardExport.objects.filter(user=request.user)[:50]})
    result['Cache-Control'] = 'no-store'
    return result


def report_for_template(saved, template):
    return saved.get('dashboard_map_report', saved['report']) if template == 'satellite' else saved['report']


def dashboard_url(export, config):
    return f'{config.get("PUBLIC_URL", config["URL"])}/superset/dashboard/{export.dashboard_id}/'


def publish_export(export, saved, config):
    # Durable compare-and-set prevents duplicate dispatch, even after cache loss.
    export.source['binding'] = binding(config)
    if not DashboardExport.objects.filter(pk=export.pk, status='pending').update(
            status='creating', source=export.source):
        return response({'ok': False, 'error': 'This export was already requested. Review its recorded status.'}, 409)
    export.status = 'creating'
    try:
        report = report_for_template(saved, export.specification.get('template'))
        publish(export, report, config,
                lambda: current_authorization(export.user_id, saved))
    except Exception as exc:
        export.status = 'failed'
        detail = str(exc) if isinstance(exc, PublishError) else 'Publication failed.'
        export.error = (detail + ' Recorded objects may exist; remove this export before creating another.')[:500]
        export.save(update_fields=['status', 'error'])
        return response({'ok': False, 'error': export.error, 'export_id': str(export.pk)}, 502)
    return response({'ok': True, 'url': dashboard_url(export, config),
                     'template': export.specification.get('template', 'comparison'), 'fallback': export.specification.get('fallback', False),
                     'message': 'Private snapshot dashboard created. Sign in to Superset to view it.'})


def quick_publish(request, token):
    if not isinstance(token, str) or not token:
        raise PublishError('Choose a valid report snapshot.')
    saved = source_report(request, token)
    config = configuration(request.user)
    # Stable for this user and snapshot across retries, browser tabs and cache workers.
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, f'agentic:quick-publish:{request.user.pk}:{token}')
    export = DashboardExport.objects.filter(pk=identifier, user=request.user).first()
    if export is None:
        template = config.get('DEFAULT_TEMPLATE', 'comparison')
        if not isinstance(template, str) or template not in DESIGNS:
            template = 'comparison'
        report = report_for_template(saved, template)
        date = report.get('report_date')
        title = 'Report snapshot'
        if isinstance(date, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', date):
            title = f'{title} · {date}'
        try:
            spec = specification(report, title, template=template)
        except PublishError:
            if template == 'comparison':
                raise
            spec = specification(saved['report'], title, template='comparison')
            spec['fallback'] = True
        source = {key: saved[key] for key in ('tool', 'plan', 'loan_access') if key in saved}
        source['report_token'] = token
        export, _ = DashboardExport.objects.get_or_create(pk=identifier, user=request.user, defaults={
            'title': spec['title'], 'specification': spec, 'source': source,
        })
    if export.source.get('binding') and export.source['binding'] != binding(config):
        raise PublishError('Superset destination changed. An administrator must reconcile this export.')
    if export.status == 'ready':
        return response({'ok': True, 'url': dashboard_url(export, config),
                         'template': export.specification.get('template', 'comparison'), 'fallback': export.specification.get('fallback', False),
                         'message': 'This snapshot already has a private Superset dashboard.'})
    if export.status == 'deleted':
        raise PublishError('This dashboard was removed. Use the guided builder to publish this snapshot again.')
    if export.status == 'failed':
        return response({'ok': False, 'error': 'The previous attempt needs review. Open the dashboard export library before retrying.',
                         'export_id': str(export.pk)}, 409)
    if export.status == 'creating':
        return response({'ok': False, 'error': 'This dashboard is already being published. Check again shortly.'}, 409)
    if not current_authorization(request.user.pk, saved):
        raise PermissionError('Report access is no longer permitted.')
    return publish_export(export, saved, config)


@csrf_protect
def dashboard_api(request):
    if not permitted(request.user):
        return response({'ok': False, 'error': 'Dashboard publishing permission required.'}, 403)
    if request.method != 'POST':
        return response({'ok': False, 'error': 'POST required.'}, 405)
    try:
        if request.content_type != 'application/json' or len(request.body) > 4096:
            raise PublishError('Send a dashboard action as a small JSON object.')
        body = json.loads(request.body)
        if not isinstance(body, dict):
            raise PublishError('Expected an action object.')
        action = body.get('action')
        if action not in {'preview', 'publish', 'quick_publish', 'delete', 'open'}:
            raise PublishError('Choose preview, publish, quick publish, open or delete.')
        allowed = {'action', 'export_id'}
        if action == 'preview':
            allowed = {'action', 'title', 'dimension', 'secondary_dimension', 'metrics', 'template', 'report_token'}
        elif action == 'quick_publish':
            allowed = {'action', 'report_token'}
        if set(body) - allowed:
            raise PublishError('Unexpected dashboard parameters. Report data and SQL cannot be submitted.')
        if action == 'quick_publish':
            return quick_publish(request, body.get('report_token'))
        if action == 'preview':
            rate_key = f'agentic:dashboard:preview:{request.user.pk}'
            if cache.add(rate_key, 1, 60):
                count = 1
            else:
                count = cache.incr(rate_key)
            if count > 10:
                return response({'ok': False, 'error': 'Please wait a minute before creating another preview.'}, 429)
            if 'report_token' in body and not isinstance(body['report_token'], str):
                raise PublishError('Choose a valid report snapshot.')
            saved = source_report(request, body.get('report_token'))
            template = body.get('template', 'comparison')
            report = report_for_template(saved, template)
            spec = specification(report, body.get('title', 'Report dashboard'),
                                 body.get('dimension'), body.get('metrics'), template, body.get('secondary_dimension'))
            source = {k: v for k, v in saved.items() if k in {'tool', 'plan', 'loan_access'}}
            source['report_token'] = saved['report_token']
            export = DashboardExport.objects.create(user=request.user, title=spec['title'],
                                                     specification=spec, source=source)
            return response({'ok': True, 'export_id': str(export.pk), 'specification': spec,
                             'sample': report['rows'][:5]})
        try:
            identifier = uuid.UUID(str(body.get('export_id', '')))
        except ValueError:
            raise PublishError('Invalid export identifier.') from None
        export = DashboardExport.objects.filter(pk=identifier, user=request.user).first()
        if not export:
            return response({'ok': False, 'error': 'Export unavailable.'}, 404)
        config = configuration(request.user)
        if export.source.get('binding') and export.source['binding'] != binding(config):
            raise PublishError('Superset destination changed. An administrator must reconcile this export.')
        if action == 'open':
            if export.status != 'ready' or not current_authorization(request.user.pk, export.source):
                raise PermissionError('This dashboard is no longer available to your current access.')
            return response({'ok': True, 'url': dashboard_url(export, config)})
        if action == 'delete':
            # Allow removal after source access revocation. Lock against concurrent
            # publication and never delete objects from a different destination.
            if export.status in {'creating', 'pending'}:
                raise PublishError('Only completed or failed exports can be removed.')
            with transaction.atomic():
                locked = DashboardExport.objects.select_for_update().get(pk=export.pk)
                if locked.status == 'creating':
                    raise PublishError('An export operation is already running.')
                locked.status = 'creating'
                locked.save(update_fields=['status'])
            try:
                remove(export, config)
            except Exception:
                DashboardExport.objects.filter(pk=export.pk).update(status='failed', error='Cleanup incomplete. Review recorded remote objects.')
                raise PublishError('Cleanup incomplete. Review export records before retrying.') from None
            return response({'ok': True, 'message': 'Dashboard, charts, dataset and snapshot table removed.'})
        token = export.source.get('report_token')
        saved = cache.get('agentic:summary:' + token) if isinstance(token, str) else None
        if not saved or saved['user_id'] != request.user.pk:
            raise PublishError('Report expired. Run the report and preview again.')
        if not current_authorization(request.user.pk, saved):
            raise PermissionError('Report access is no longer permitted.')
        return publish_export(export, saved, config)
    except PermissionError as exc:
        return response({'ok': False, 'error': str(exc)}, 403)
    except PublishError as exc:
        return response({'ok': False, 'error': str(exc)}, 400)
    except (ValueError, TypeError, UnicodeError):
        return response({'ok': False, 'error': 'Invalid dashboard request.'}, 400)
    except Exception:
        return response({'ok': False, 'error': 'Dashboard service unavailable. Check export records before retrying.'}, 503)

