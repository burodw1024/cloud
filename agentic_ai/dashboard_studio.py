"""Authenticated dashboard preview, publication journal and cleanup endpoints."""
import json
import uuid

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect

from .api import _can_replay_result
from .models import DashboardExport
from .superset_publisher import PublishError, configuration, specification, publish, remove


def permitted(user):
    return (user.is_authenticated and user.is_active and user.is_staff
            and user.has_perm('agentic_ai.publish_dashboard'))


def response(body, status=200):
    reply = JsonResponse(body, status=status)
    reply['Cache-Control'] = 'no-store'
    return reply


def source_report(request):
    token = request.session.get('dashboard_report_token')
    saved = cache.get('agentic:summary:' + token) if isinstance(token, str) else None
    if not saved or saved['user_id'] != request.user.pk:
        raise PublishError('Run a report first. The displayed report snapshot expires after 30 minutes.')
    if not _can_replay_result(request.user, saved):
        raise PermissionError('Report access is no longer permitted.')
    return saved


def current_authorization(user_id, source):
    user = get_user_model().objects.get(pk=user_id)
    return permitted(user) and _can_replay_result(user, source)


def binding(config):
    return {key: config[key] for key in ('URL', 'DATABASE_ALIAS', 'DATABASE_ID', 'SCHEMA', 'OWNER_ID')}


@csrf_protect
def studio(request):
    if not permitted(request.user):
        return response({'ok': False, 'error': 'Dashboard publishing permission required.'}, 403)
    if request.method != 'GET':
        return response({'ok': False, 'error': 'GET required.'}, 405)
    readiness = 'Ready to create private dashboards.'
    try:
        configuration(request.user)
    except PublishError as exc:
        readiness = str(exc)
    result = render(request, 'agentic_ai/dashboard_studio.html', {
        'readiness': readiness, 'exports': DashboardExport.objects.filter(user=request.user)[:50]})
    result['Cache-Control'] = 'no-store'
    return result


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
        if action not in {'preview', 'publish', 'delete', 'open'}:
            raise PublishError('Choose preview, publish, open or delete.')
        allowed = {'action', 'title', 'dimension', 'metrics'} if action == 'preview' else {'action', 'export_id'}
        if set(body) - allowed:
            raise PublishError('Unexpected dashboard parameters. Report data and SQL cannot be submitted.')
        if action == 'preview':
            rate_key = f'agentic:dashboard:preview:{request.user.pk}'
            if cache.add(rate_key, 1, 60):
                count = 1
            else:
                count = cache.incr(rate_key)
            if count > 10:
                return response({'ok': False, 'error': 'Please wait a minute before creating another preview.'}, 429)
            saved = source_report(request)
            spec = specification(saved['report'], body.get('title', 'Report dashboard'),
                                 body.get('dimension'), body.get('metrics'))
            source = {k: v for k, v in saved.items() if k in {'tool', 'plan', 'loan_access'}}
            source['report_token'] = request.session['dashboard_report_token']
            export = DashboardExport.objects.create(user=request.user, title=spec['title'],
                                                     specification=spec, source=source)
            return response({'ok': True, 'export_id': str(export.pk), 'specification': spec,
                             'sample': saved['report']['rows'][:5]})
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
            return response({'ok': True, 'url': f'{config["URL"]}/superset/dashboard/{export.dashboard_id}/'})
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
        # Durable compare-and-set prevents duplicate dispatch, even after cache loss.
        export.source['binding'] = binding(config)
        if not DashboardExport.objects.filter(pk=export.pk, status='pending').update(
                status='creating', source=export.source):
            return response({'ok': False, 'error': 'This export was already requested. Review its recorded status.'}, 409)
        export.status = 'creating'
        try:
            publish(export, saved['report'], config,
                    lambda: current_authorization(request.user.pk, saved))
        except Exception as exc:
            export.status = 'failed'
            detail = str(exc) if isinstance(exc, PublishError) else 'Publication failed.'
            export.error = (detail + ' Recorded objects may exist; remove this export before creating another.')[:500]
            export.save(update_fields=['status', 'error'])
            return response({'ok': False, 'error': export.error, 'export_id': str(export.pk)}, 502)
        return response({'ok': True, 'url': f'{config["URL"]}/superset/dashboard/{export.dashboard_id}/',
                         'message': 'Private snapshot dashboard created. Sign in to Superset to view it.'})
    except PermissionError as exc:
        return response({'ok': False, 'error': str(exc)}, 403)
    except PublishError as exc:
        return response({'ok': False, 'error': str(exc)}, 400)
    except (ValueError, TypeError, UnicodeError):
        return response({'ok': False, 'error': 'Invalid dashboard request.'}, 400)
    except Exception:
        return response({'ok': False, 'error': 'Dashboard service unavailable. Check export records before retrying.'}, 503)
