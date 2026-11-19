"""Owner-scoped, one-time report schedules and delivery."""
import asyncio
import json
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from asgiref.sync import async_to_sync
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import BooleanField, Case, Q, Value, When
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from . import dashboard_studio, whatsapp
from .access_admin import record_event
from .api import _can_replay_result, record_report_result
from .mcp_gateway import MAX_QUERY_CHARS, gateway_enabled, load_gateway_config, run_mcp_query
from .models import ScheduledReport
from .reporting_config import workspace_settings
from .share_channels import CHANNELS, describe
from .superset_publisher import PublishError

logger = logging.getLogger(__name__)
DHAKA = ZoneInfo('Asia/Dhaka')
LEASE = timedelta(seconds=90)
OVERDUE = timedelta(minutes=5)
HEARTBEAT_KEY = 'agentic:scheduled-reports:worker'
DESTINATIONS = {'history': 'My report runs', 'superset': 'Publish a private Superset dashboard',
                'powerbi': 'Prepare a Power BI workbook', 'whatsapp': 'WhatsApp', **CHANNELS}


def delivery_status(user, destination):
    return whatsapp.settings_status(user) if destination == 'whatsapp' else describe(destination, user)


def schedule_allowed(user):
    return user.is_active and user.has_perm('agentic_ai.schedule_report')


def available_destinations(user):
    choices = [('history', DESTINATIONS['history'])]
    if workspace_settings(user)['powerbi']['enabled']:
        choices.append(('powerbi', DESTINATIONS['powerbi']))
    if dashboard_studio.permitted(user):
        try:
            dashboard_studio.configuration(user)
        except PublishError:
            pass
        else:
            choices.append(('superset', DESTINATIONS['superset']))
    for channel in ('whatsapp', *CHANNELS):
        settings = delivery_status(user, channel)
        if settings['ready']:
            for recipient in settings['recipients']:
                display = '+' + recipient if channel == 'whatsapp' else recipient
                choices.append((f'{channel}|{recipient}', f'{DESTINATIONS[channel]} · {display}'))
    return choices


class ScheduleForm(forms.Form):
    question = forms.CharField(max_length=MAX_QUERY_CHARS, widget=forms.Textarea(attrs={'rows': 4}))
    run_at = forms.DateTimeField(label='Run at (Dhaka time)', input_formats=['%Y-%m-%dT%H:%M'],
                                widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}))
    delivery = forms.ChoiceField(label='What should happen to the result?')
    confirmed = forms.BooleanField(label='I authorize this report to run and use the selected destination at that time.')

    def clean_run_at(self):
        value = self.cleaned_data['run_at']
        if value <= timezone.now():
            raise forms.ValidationError('Choose a future date and time.')
        return value


@login_required
@permission_required('agentic_ai.schedule_report', raise_exception=True)
@require_http_methods(['GET', 'POST'])
@csrf_protect
def schedule_page(request):
    own = ScheduledReport.objects.filter(user=request.user)
    if request.method == 'POST' and request.POST.get('action') == 'cancel':
        identifier = request.POST.get('schedule_id', '')
        if identifier.isascii() and identifier.isdecimal() and len(identifier) <= 18:
            own.filter(pk=identifier, status='pending').update(status='cancelled')
        return redirect('agentic_ai:schedule_page')
    with timezone.override(DHAKA):
        form = ScheduleForm(request.POST if request.method == 'POST' else None)
        form.fields['delivery'].choices = available_destinations(request.user)
        if request.method == 'POST' and form.is_valid():
            data = form.cleaned_data
            destination, _, recipient = data['delivery'].partition('|')
            own.create(user=request.user, question=data['question'], run_at=data['run_at'], destination=destination, recipient=recipient)
            return redirect('agentic_ai:schedule_page')
        recent = [(job, DESTINATIONS[job.destination]) for job in own.order_by('-id')[:50]]
        response = render(request, 'agentic_ai/scheduled_reports.html', {'form': form, 'recent': recent})
    response['Cache-Control'] = 'no-store'
    return response


def reconcile_expired(now):
    """An abandoned dispatch is reviewed, never automatically dispatched again."""
    stale = ScheduledReport.objects.filter(status='running').filter(
        Q(lease_expires_at__lte=now) |
        Q(lease_expires_at__isnull=True, started_at__lte=now - timedelta(minutes=45)) |
        Q(lease_expires_at__isnull=True, started_at__isnull=True, run_at__lte=now - timedelta(minutes=45))
    )
    # Existing schedules from before phase tracking may already have dispatched.
    possibly_sent = Q(delivery_started_at__isnull=False) | Q(lease_expires_at__isnull=True)
    uncertain = stale.filter(possibly_sent).update(
        status='failed', outcome_uncertain=True,
        message='Worker interrupted after delivery may have started. Check the destination before scheduling again.')
    interrupted = stale.exclude(possibly_sent).update(
        status='failed', message='Worker interrupted before delivery started. Review the report run or schedule it again.')
    if uncertain or interrupted:
        logger.warning('Scheduled reports reconciled: %s uncertain, %s interrupted before delivery', uncertain, interrupted)


def claim_due(exclude_users=()):
    now = timezone.now()
    job = (ScheduledReport.objects.filter(status='pending', run_at__lte=now)
           .exclude(user_id__in=exclude_users).order_by('run_at', 'pk').first())
    if job and ScheduledReport.objects.filter(pk=job.pk, status='pending').update(
            status='running', started_at=now, lease_expires_at=now + LEASE):
        job.started_at = now
        return job
    return None


def renew_claims(job_ids, now):
    ScheduledReport.objects.filter(pk__in=job_ids, status='running',
                                   lease_expires_at__gt=now).update(lease_expires_at=now + LEASE)


def begin_delivery(job):
    now = timezone.now()
    return bool(ScheduledReport.objects.filter(
        pk=job.pk, status='running', lease_expires_at__gt=now, delivery_started_at__isnull=True,
    ).update(delivery_started_at=now))


def process_claimed(job, stop_requested):
    try:
        status, message = execute(job, stop_requested)
    except Exception as exc:
        logger.warning('Scheduled report %s could not complete (%s)', job.pk, type(exc).__name__)
        status, message = 'failed', 'Could not confirm completion. Check report history and delivery activity before scheduling again.'
    # Commit the terminal state and its dispatch warning together.
    ScheduledReport.objects.filter(pk=job.pk, status='running').update(
        status=status, message=message[:400],
        outcome_uncertain=Case(When(delivery_started_at__isnull=False, then=Value(status == 'failed')),
                               default=Value(False), output_field=BooleanField()),
    )


def process_due(stop_requested=lambda: False):
    if stop_requested():
        return False
    job = claim_due()
    if not job:
        return False
    process_claimed(job, stop_requested)
    return True


def health_counts(now=None):
    now = now or timezone.now()
    queue = ScheduledReport.objects
    return {
        'worker_alive': int(cache.get(HEARTBEAT_KEY) is not None),
        'overdue': queue.filter(status='pending', run_at__lt=now - OVERDUE).count(),
        'running': queue.filter(status='running').count(),
        'needs_review': queue.filter(status='failed', reviewed_at__isnull=True).count(),
        'uncertain': queue.filter(status='failed', outcome_uncertain=True, reviewed_at__isnull=True).count(),
    }


@login_required
@require_http_methods(['GET', 'POST'])
@csrf_protect
def schedule_operations(request):
    if not request.user.is_active or not request.user.is_superuser:
        raise PermissionDenied
    error = ''
    if request.method == 'POST':
        identifier = request.POST.get('schedule_id', '')
        reason = request.POST.get('reason', '').strip()
        if not identifier.isascii() or not identifier.isdecimal() or len(identifier) > 18 or not 8 <= len(reason) <= 500:
            error = 'Enter a review note of 8–500 characters.'
        else:
            with transaction.atomic():
                job = ScheduledReport.objects.select_related('user').filter(pk=identifier, status='failed').first()
                if job and ScheduledReport.objects.filter(
                        pk=job.pk, status='failed', reviewed_at__isnull=True).update(reviewed_at=timezone.now()):
                    record_event(request.user, job.user, 'scheduled_report_reviewed', reason,
                                 {'schedule_id': job.pk, 'reviewed': False},
                                 {'schedule_id': job.pk, 'reviewed': True})
                    return redirect('agentic_ai:schedule_operations')
            error = 'The schedule is no longer awaiting review.'
    queue = ScheduledReport.objects.select_related('user').order_by('run_at', 'pk')
    waiting = Q(status='pending', run_at__lt=timezone.now() - OVERDUE)
    running = Q(status='running')
    review = Q(status='failed', reviewed_at__isnull=True)
    with timezone.override(DHAKA):
        response = render(request, 'agentic_ai/scheduled_report_operations.html', {
            'health': health_counts(), 'jobs': queue.filter(waiting | running | review)[:50], 'error': error,
        })
    response['Cache-Control'] = 'no-store'
    return response


def execute(job, stop_requested):
    user = get_user_model().objects.get(pk=job.user_id)
    destination = job.destination
    key = f'{destination}|{job.recipient}' if job.recipient else destination
    if not schedule_allowed(user) or key not in dict(available_destinations(user)):
        return 'failed', 'The schedule or its destination is no longer authorized or configured.'
    if not gateway_enabled():
        return 'failed', 'The report gateway is paused. Run the question after it is available.'
    result = run_mcp_query(user=user, query=job.question, scheduled=True)
    if not result.get('ok') or not result.get('tool_executed'):
        return 'failed', 'The request did not complete as a report. Check User attempts for clarification, approval or errors.'
    user = get_user_model().objects.get(pk=job.user_id)
    if not schedule_allowed(user) or not _can_replay_result(user, result):
        return 'failed', 'Report access changed while the request was running.'
    result = record_report_result(user, job.question, result)
    if not result.get('report_run_id'):
        return 'failed', 'No exportable report snapshot was produced.'
    job.report_run_id = result['report_run_id']
    job.save(update_fields=['report_run'])
    if destination == 'history':
        return 'ready', 'Report available in My report runs.'
    if destination == 'powerbi':
        return 'ready', 'Power BI workbook available in My report runs for 30 minutes. Publish from Desktop.'
    if stop_requested():
        return 'failed', 'Report saved. Delivery was interrupted by a release; review it in My report runs.'
    if destination == 'superset':
        if not dashboard_studio.permitted(user):
            return 'failed', 'Superset publishing permission changed. The report was saved.'
        if stop_requested() or not begin_delivery(job):
            return 'failed', 'The worker stopped before publishing. The report was saved.'
        reply = dashboard_studio.quick_publish(user, result['summary_token'])
        if json.loads(reply.content).get('ok'):
            return 'ready', 'Private dashboard published. Open it from My report runs.'
        return 'failed', 'Superset publishing needs review. Open the report run for export status.'
    if destination in {'whatsapp', *CHANNELS}:
        settings = delivery_status(user, destination)
        if not settings['ready'] or job.recipient not in settings['recipients']:
            return 'failed', 'Delivery access or the approved recipient changed. The report was saved.'
        config = load_gateway_config()
        text = whatsapp.export_text(result, 'summary')
        if stop_requested() or not begin_delivery(job):
            return 'failed', 'The worker stopped before sending. The report was saved.'
        accepted = async_to_sync(asyncio.wait_for)(
            whatsapp.dispatch(config, settings['tool'], job.recipient, text, user, result, channel=destination),
            min(30, config['request_timeout']))
        if accepted.get('ok'):
            return 'ready', f'{DESTINATIONS[destination]} accepted the summary. Receipt is not confirmed.'
        if accepted.get('approval_required'):
            return 'failed', 'Delivery requires approval. Review the approval request.'
        return 'failed', 'Delivery acceptance could not be confirmed. Check provider activity before retrying.'
    return 'failed', 'Unknown destination.'
