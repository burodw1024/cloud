from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from agentic_ai.models import AgentTool, DashboardExport, ReportRun


class ReportHistoryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_superuser('report-owner')
        self.client.force_login(self.owner)
        self.tool = AgentTool.objects.create(name='branch-balances', is_active=True, allowed_roles=['admin'])
        self.payload = {
            'ok': True, 'tool': self.tool.name,
            'plan': {'intent': 'mcp_tool_call', 'params': {'branch_code': '0042'}},
            'raw': {'structuredContent': {
                'columns': ['BRANCHCODE', 'GS_BALANCE'], 'rows': [{'BRANCHCODE': '0042', 'GS_BALANCE': 678}],
                'scope': {'branch_code': '0042'}, 'report_date': '2026-11-18', 'row_count': 4, 'truncated': True,
            }},
        }

    def run_report(self):
        with patch('agentic_ai.api.execute_query', return_value=self.payload):
            result = self.client.post(reverse('agentic_ai:agentic_query_api'), {'message': 'Branch savings last week'})
        self.assertEqual(result.status_code, 200)
        run = ReportRun.objects.get(pk=result.json()['report_run_id'])
        self.assertEqual(result.json()['report_context']['source_rows'], 4)
        return run

    def test_search_reopen_expiry_and_dashboard_status(self):
        run = self.run_report()
        self.assertNotIn('678', str(run.context))
        self.assertNotIn('678', str(run.source))
        url = reverse('agentic_ai:report_run', args=[run.pk])
        DashboardExport.objects.create(user=self.owner, title='Published snapshot', dashboard_id=51,
                                       status='ready', source={'report_token': run.snapshot_token})
        listing = self.client.get(reverse('agentic_ai:report_history'), {'q': 'savings'})
        self.assertContains(listing, 'Dashboard ready')
        self.assertContains(listing, 'Branch savings')
        detail = self.client.get(url)
        self.assertEqual(detail['Cache-Control'], 'no-store')
        self.assertContains(detail, '678')
        self.assertContains(detail, 'Open in Superset')
        self.assertContains(detail, 'Filter · Branch')
        cache.delete('agentic:summary:' + run.snapshot_token)
        expired = self.client.get(url)
        self.assertContains(expired, 'temporary report rows have expired')
        self.assertContains(expired, 'Open in Superset')
        self.assertNotContains(expired, '>678<')
        workspace = self.client.get(reverse('agentic_ai:query_page'), {'reuse': run.pk})
        self.assertEqual(workspace.context['draft_question'], run.question)

    def test_foreign_users_and_revoked_tools_cannot_reopen(self):
        run = self.run_report()
        url = reverse('agentic_ai:report_run', args=[run.pk])
        self.client.force_login(User.objects.create_superuser('another-admin'))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertNotContains(self.client.get(reverse('agentic_ai:report_history')), run.question)
        self.client.force_login(self.owner)
        self.tool.is_active = False
        self.tool.save(update_fields=['is_active'])
        revoked = self.client.get(url)
        self.assertContains(revoked, 'Report access has changed')
        self.assertNotContains(revoked, '0042')
        self.assertNotContains(self.client.get(reverse('agentic_ai:report_history')), run.question)
        workspace = self.client.get(reverse('agentic_ai:query_page'), {'reuse': run.pk})
        self.assertEqual(workspace.context['draft_question'], '')
