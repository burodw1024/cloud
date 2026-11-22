import os
from unittest.mock import patch
from xml.etree import ElementTree as ET

from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from agentic_ai import scheduled_reports
from agentic_ai.models import AgentTool, ReportingConnection, ReportRun, ScheduledReport
from agentic_ai.powerbi_publish import RDL, PublicationError


class PowerBIPublishTests(TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            'AGENTIC_POWERBI_PUBLISH_USER': r'FILESERVER\publisher',
            'AGENTIC_POWERBI_PUBLISH_PASSWORD': 'test-password',
            'AGENTIC_POWERBI_PUBLISH_FOLDER': '/BURO Reports',
        })
        env.start()
        self.addCleanup(env.stop)
        self.user = User.objects.create_user('publisher')
        self.user.user_permissions.add(Permission.objects.get(codename='export_powerbi_snapshot'))
        self.client.force_login(self.user)
        self.tool = AgentTool.objects.create(name='report-tool', allowed_roles=['staff'])
        self.run = ReportRun.objects.create(
            user=self.user, question='Branch report', snapshot_token='rdl-test',
            source={'tool': self.tool.name, 'plan': {'intent': 'mcp_tool_call'}},
        )
        ReportingConnection.objects.create(configuration={
            'POWERBI_MODE': 'manual', 'POWERBI_PORTAL_URL': 'https://reports.internal/reports',
        })
        cache.set('agentic:summary:rdl-test', {
            'user_id': self.user.pk,
            'report': {'columns': ['Branch', '=1+1'], 'rows': [{'Branch': '0010 & west', '=1+1': '=2+2'}]},
        })
        self.addCleanup(cache.delete, 'agentic:summary:rdl-test')
        self.url = reverse('agentic_ai:powerbi_publish', args=[self.run.pk])

    def grant_publish(self):
        self.user.user_permissions.add(Permission.objects.get(codename='publish_powerbi_report'))

    @patch('agentic_ai.powerbi_publish.upload')
    def test_publish_creates_one_rdl_and_remembers_link(self, upload):
        self.grant_publish()
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url).status_code, 302)
        upload.assert_called_once()
        portal, _, _, folder, name, definition = upload.call_args.args
        self.assertEqual(folder, '/BURO Reports')
        root = ET.fromstring(definition)
        query = ET.fromstring(root.findtext(f'.//{{{RDL}}}CommandText'))
        self.assertEqual(query.findtext('XmlData/Rows/Row/Column0'), '0010 & west')
        self.assertEqual(query.findtext('XmlData/Rows/Row/Column1'), '=2+2')
        self.assertIn('="=1+1"', [x.text for x in root.iter(f'{{{RDL}}}Value')])
        self.run.refresh_from_db()
        self.assertEqual(self.run.powerbi_report_url, f'{portal}/report/BURO%20Reports/{name}')
        self.assertContains(self.client.get(reverse('agentic_ai:report_run', args=[self.run.pk])),
                            'Open published Power BI report')

    @patch('agentic_ai.powerbi_publish.upload')
    def test_publish_requires_grant_ownership_and_current_access(self, upload):
        self.assertEqual(self.client.post(self.url).status_code, 400)
        self.grant_publish()
        self.client.force_login(User.objects.create_user('other'))
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.client.force_login(self.user)
        self.tool.is_active = False
        self.tool.save(update_fields=['is_active'])
        self.assertEqual(self.client.post(self.url).status_code, 400)
        upload.assert_not_called()

    @patch('agentic_ai.powerbi_publish.upload', side_effect=PublicationError('Upload unconfirmed.'))
    def test_failure_does_not_save_published_link(self, upload):
        self.grant_publish()
        self.assertContains(self.client.post(self.url), 'Upload unconfirmed.', status_code=400)
        self.run.refresh_from_db()
        self.assertEqual(self.run.powerbi_report_url, '')

    @patch('agentic_ai.powerbi_publish.upload')
    def test_scheduled_publish_uses_worker_delivery_claim(self, upload):
        self.grant_publish()
        self.user.user_permissions.add(Permission.objects.get(codename='schedule_report'))
        job = ScheduledReport.objects.create(user=self.user, question=self.run.question,
                                             run_at=timezone.now(), destination='powerbi_publish')
        result = {'ok': True, 'tool_executed': True, **self.run.source}

        def verify_claim(*args):
            job.refresh_from_db()
            self.assertIsNotNone(job.delivery_started_at)

        upload.side_effect = verify_claim
        with patch.object(scheduled_reports, 'gateway_enabled', return_value=True), patch.object(
                scheduled_reports, 'run_mcp_query', return_value=result), patch.object(
                scheduled_reports, 'record_report_result', return_value={'report_run_id': self.run.pk}):
            self.assertTrue(scheduled_reports.process_due())
            self.assertFalse(scheduled_reports.process_due())
        job.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(job.status, 'ready')
        self.assertTrue(self.run.powerbi_report_url)
        upload.assert_called_once()
