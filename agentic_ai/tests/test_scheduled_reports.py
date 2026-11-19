from datetime import timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from django.contrib.auth.models import Permission, User
from django.http import JsonResponse
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from agentic_ai import scheduled_reports as schedules
from agentic_ai.models import ReportRun, ScheduledReport


class ScheduledReportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('schedule-user')
        self.permission = Permission.objects.get(codename='schedule_report')
        self.user.user_permissions.add(self.permission)
        self.client.force_login(self.user)
        self.url = reverse('agentic_ai:schedule_page')
        self.question = 'Show savings deposits for branch 0253 from 2026-10-01 through 2026-10-31.'
        self.result = {'ok': True, 'tool_executed': True, 'tool': 'savings-report', 'answer': 'Report ready',
                       'raw': {'structuredContent': {'columns': ['BRANCHCODE', 'TOTAL_DEPOSITS'],
                               'rows': [{'BRANCHCODE': '0253', 'TOTAL_DEPOSITS': '32.50'}]}}}

    def pending(self, destination='history', recipient=''):
        return ScheduledReport.objects.create(user=self.user, question=self.question,
            run_at=timezone.now() - timedelta(seconds=1), destination=destination, recipient=recipient)

    def test_only_owner_can_cancel_and_dhaka_form_requires_confirmation(self):
        dt = timezone.now().astimezone(ZoneInfo('Asia/Dhaka')) + timedelta(hours=1)
        payload = {'question': self.question, 'run_at': dt.strftime('%Y-%m-%dT%H:%M'), 'delivery': 'history'}
        self.assertEqual(self.client.post(self.url, payload).status_code, 200)
        self.assertFalse(ScheduledReport.objects.exists())
        self.assertEqual(self.client.post(self.url, {**payload, 'confirmed': 'on'}).status_code, 302)
        job = ScheduledReport.objects.get()
        self.assertEqual(job.run_at, dt.replace(second=0, microsecond=0))
        other = User.objects.create_user('other-scheduler')
        other.user_permissions.add(self.permission)
        self.client.force_login(other)
        self.client.post(self.url, {'action': 'cancel', 'schedule_id': job.pk})
        job.refresh_from_db()
        self.assertEqual(job.status, 'pending')
        self.assertNotContains(self.client.get(self.url), self.question)
        self.client.force_login(self.user)
        self.client.post(self.url, {'action': 'cancel', 'schedule_id': job.pk})
        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')

    @patch.object(schedules, 'run_mcp_query')
    def test_revoked_schedule_permission_blocks_execution(self, query):
        job = self.pending()
        self.user.user_permissions.remove(self.permission)
        self.assertTrue(schedules.process_due())
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        query.assert_not_called()

    @patch.object(schedules, 'gateway_enabled', return_value=True)
    @patch.object(schedules, '_can_replay_result', return_value=True)
    @patch.object(schedules, 'run_mcp_query')
    @patch.object(schedules, 'available_destinations')
    def test_configured_destinations_dispatch_once(self, destinations, query, replay, enabled):
        destinations.return_value = [('superset', 'Superset'), ('gmail|approved@example.org', 'Gmail')]
        query.side_effect = lambda **kwargs: dict(self.result)
        gmail = self.pending('gmail', 'approved@example.org')
        dashboard = self.pending('superset')
        dispatch = AsyncMock(return_value={'ok': True})
        with patch.object(schedules, 'delivery_status', return_value={
                'ready': True, 'tool': 'mail-tool', 'recipients': [gmail.recipient]}), patch.object(
                schedules.whatsapp, 'dispatch', new=dispatch), patch.object(
                schedules, 'load_gateway_config', return_value={'request_timeout': 50}), patch.object(
                schedules.dashboard_studio, 'permitted', return_value=True), patch.object(
                schedules.dashboard_studio, 'quick_publish', return_value=JsonResponse({'ok': True})) as publish:
            self.assertTrue(schedules.process_due())
            self.assertTrue(schedules.process_due())
            self.assertFalse(schedules.process_due())
        gmail.refresh_from_db()
        dashboard.refresh_from_db()
        self.assertEqual((gmail.status, dashboard.status), ('ready', 'ready'))
        self.assertEqual(ReportRun.objects.count(), 2)
        self.assertTrue(gmail.report_run_id)
        self.assertEqual(dispatch.await_args.kwargs['channel'], 'gmail')
        dispatch.assert_awaited_once()
        publish.assert_called_once()
        self.assertTrue(all(call.kwargs['scheduled'] for call in query.call_args_list))
