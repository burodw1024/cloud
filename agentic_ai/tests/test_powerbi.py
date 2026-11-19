from io import BytesIO

from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from agentic_ai.models import AgentTool, ReportingConnection, ReportRun
from agentic_ai.reporting_config import ReportingForm


class PowerBIExportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('powerbi-owner')
        self.owner.user_permissions.add(Permission.objects.get(codename='export_powerbi_snapshot'))
        self.client.force_login(self.owner)
        self.tool = AgentTool.objects.create(name='powerbi-report', allowed_roles=['staff'])
        self.config = ReportingConnection.objects.create(configuration={
            'POWERBI_MODE': 'manual', 'POWERBI_PORTAL_URL': 'https://powerbi.internal/reports',
        })
        self.run = ReportRun.objects.create(
            user=self.owner, question='Branch report', snapshot_token='powerbi-test',
            source={'tool': self.tool.name, 'plan': {'intent': 'mcp_tool_call'}},
            context={'source': self.tool.name, 'displayed_rows': 1, 'limited': True},
        )
        cache.set('agentic:summary:powerbi-test', {
            'user_id': self.owner.pk, 'report': {'columns': ['BRANCHCODE', 'GS_BALANCE', 'LABEL'],
                'rows': [{'BRANCHCODE': '0010', 'GS_BALANCE': '35.5', 'LABEL': '=1+1'}]},
        })
        self.url = reverse('agentic_ai:powerbi_export', args=[self.run.pk])

    def test_workbook_keeps_snapshot_values_and_scope(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'no-store')
        book = load_workbook(BytesIO(b''.join(response.streaming_content)))
        data = book['ReportData']
        self.assertIn('ReportData', data.tables)
        self.assertEqual(data['A2'].value, '0010')
        self.assertEqual(data['B2'].value, 35.5)
        self.assertEqual(data['C2'].value, '=1+1')
        self.assertEqual(data['C2'].data_type, 's')
        self.assertEqual(book['SourceScope']['B2'].value, self.tool.name)
        page = self.client.get(reverse('agentic_ai:report_run', args=[self.run.pk]))
        self.assertContains(page, 'Download Power BI workbook')
        self.assertContains(page, 'https://powerbi.internal/reports')

    def test_permission_ownership_expiry_and_revocation(self):
        other = User.objects.create_user('powerbi-other')
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        other.user_permissions.add(Permission.objects.get(codename='export_powerbi_snapshot'))
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.owner)
        self.tool.is_active = False
        self.tool.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.tool.is_active = True
        self.tool.save(update_fields=['is_active'])
        cache.delete('agentic:summary:powerbi-test')
        self.assertEqual(self.client.get(self.url).status_code, 410)

    def test_manual_mode_accepts_blank_portal_and_rejects_credentials(self):
        form_data = {'schema': 'agentic', 'database_port': 5432, 'database_sslmode': 'verify-full',
                     'default_template': 'comparison', 'powerbi_mode': 'manual', 'version': 'new'}
        self.assertTrue(ReportingForm(form_data).is_valid())
        for url in ('https://user:secret@reports.internal/reports', 'http://reports.internal/reports',
                    'https://reports.internal/reports?token=secret'):
            form = ReportingForm({**form_data, 'powerbi_portal_url': url})
            self.assertFalse(form.is_valid())
            self.assertIn('powerbi_portal_url', form.errors)
