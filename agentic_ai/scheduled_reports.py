"""Owner-scoped, one-time report schedules and delivery."""
import asyncio
import json
import logging
from zoneinfo import ZoneInfo

from asgiref.sync import async_to_sync
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from . import dashboard_studio, whatsapp
from .api import _can_replay_result, record_report_result
from .mcp_gateway import MAX_QUERY_CHARS, gateway_enabled, load_gateway_config, run_mcp_query
from .models import ScheduledReport
from .reporting_config import workspace_settings
from .share_channels import CHANNELS, describe
from .superset_publisher import PublishError

logger = logging.getLogger(__name__)
DHAKA = ZoneInfo('Asia/Dhaka')
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


def process_due(stop_requested=lambda: False):
    now = timezone.now()
    job = ScheduledReport.objects.filter(status='pending', run_at__lte=now).order_by('run_at', 'pk').first()
    if not job or stop_requested():
        return False
    if not ScheduledReport.objects.filter(pk=job.pk, status='pending').update(status='running', started_at=now):
        return False
    try:
        status, message = execute(job, stop_requested)
    except Exception as exc:
        logger.warning('Scheduled report %s could not complete (%s)', job.pk, type(exc).__name__)
        status, message = 'failed', 'Could not confirm completion. Check report history and delivery activity before scheduling again.'
    ScheduledReport.objects.filter(pk=job.pk).update(status=status, message=message[:400])
    return True


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
        accepted = async_to_sync(asyncio.wait_for)(
            whatsapp.dispatch(config, settings['tool'], job.recipient, text, user, result, channel=destination),
            min(30, config['request_timeout']))
        if accepted.get('ok'):
            return 'ready', f'{DESTINATIONS[destination]} accepted the summary. Receipt is not confirmed.'
        if accepted.get('approval_required'):
            return 'failed', 'Delivery requires approval. Review the approval request.'
        return 'failed', 'Delivery acceptance could not be confirmed. Check provider activity before retrying.'
    return 'failed', 'Unknown destination.'
