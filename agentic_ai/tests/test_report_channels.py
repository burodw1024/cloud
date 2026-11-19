import json
from unittest.mock import AsyncMock, patch

from django.contrib.auth.models import Group, Permission, User
from django.core.cache import cache
from django.test import TestCase

from agentic_ai import mcp_gateway, whatsapp
from agentic_ai.models import AgentRunLog, AgentTool
from agentic_ai.share_channels import CHANNELS, accepted_delivery, setting_keys


class ReportChannelsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('sender')
        group = Group.objects.create(name='share')
        group.permissions.add(*Permission.objects.filter(codename__in=[f'share_report_{channel}' for channel in CHANNELS]))
        self.user.groups.add(group)
        self.settings = {}
        for channel in CHANNELS:
            tool, recipients = setting_keys(channel)
            self.settings[tool] = f'send-{channel}-report'
            self.settings[recipients] = json.dumps([self.recipient(channel)])
            AgentTool.objects.create(name=self.settings[tool], allowed_roles=['share'])
        self.settings['MCP_WHATSAPP_TOOL'] = 'send-whatsapp-text'
        self.report = AgentTool.objects.create(name='branch-report', allowed_roles=['share'])
        settings = patch('agentic_ai.share_channels.get_setting',
                         side_effect=lambda key, default='': self.settings.get(key, default))
        settings.start()
        self.addCleanup(settings.stop)
        enabled = patch.object(mcp_gateway, 'gateway_enabled', return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.client.force_login(self.user)
        result = {'ok': True, 'answer': 'Authorized report', 'tool': self.report.name,
                  'plan': {'intent': 'mcp_tool_call'}}
        whatsapp.remember_result(self.user, result)
        self.token = result['share_result_id']

    def recipient(self, channel):
        return 'subscriber123=' if channel == 'viber' else 'approved@example.org'

    def post(self, channel, **overrides):
        data = {'action': 'preview', 'recipient': self.recipient(channel),
                'result_id': self.token, 'mode': 'summary', **overrides}
        return self.client.post(f'/agentic/api/share/{channel}/', data, content_type='application/json')

    @patch.object(whatsapp, 'load_gateway_config', return_value={'request_timeout': 3})
    @patch.object(whatsapp, 'dispatch', new_callable=AsyncMock)
    def test_channels_require_matching_preview_and_each_deduplicates(self, dispatch, config):
        dispatch.return_value = {'ok': True, 'message': 'Accepted'}
        for channel in CHANNELS:
            with self.subTest(channel=channel):
                self.assertEqual(self.post(channel, action='send', confirmed=True).status_code, 400)
                self.assertEqual(self.post(channel, recipient='unapproved@example.org').status_code, 403)
                preview = self.post(channel).json()
                self.assertIn('Authorized report', preview['text'])
                self.assertEqual(bool(preview['subject']), channel != 'viber')
                self.assertEqual(self.post(channel, action='send', confirmed=True).status_code, 200)
                self.assertEqual(self.post(channel, action='send', confirmed=True).status_code, 409)
        self.assertEqual(dispatch.await_count, 3)
        self.assertEqual(AgentRunLog.objects.filter(status='success').count(), 3)

    def test_role_changes_and_sender_tools_hidden_from_planner(self):
        self.assertEqual(mcp_gateway._authorized_names(self.user), {self.report.name})
        self.assertContains(self.client.get('/agentic/'), 'id="share-gmail-recipient"')
        self.report.is_active = False
        self.report.save(update_fields=['is_active'])
        self.assertEqual(self.post('gmail').status_code, 403)

    def test_requires_explicit_acknowledgment(self):
        self.assertTrue(accepted_delivery({'structuredContent': {'accepted': True}}))
        for data in ({'ok': True}, {'accepted': 1}, {'isError': True, 'accepted': True}):
            self.assertFalse(accepted_delivery(data))
