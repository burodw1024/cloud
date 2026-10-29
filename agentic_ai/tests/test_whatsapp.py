import json
from unittest.mock import AsyncMock, patch
from django.contrib.auth.models import User, Group
from django.core.cache import cache
from django.test import TestCase, Client
from agentic_ai.models import AgentTool, AgentRunLog
from agentic_ai import whatsapp as wa


class WhatsAppTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('wa-user')
        self.user.groups.add(Group.objects.create(name='report-users'))
        self.tool = AgentTool.objects.create(name='send-whatsapp-text', allowed_roles=['report-users'])
        self.report_tool = AgentTool.objects.create(name='loan-report', allowed_roles=['report-users'])
        self.settings = {'MCP_WHATSAPP_TOOL': self.tool.name, 'MCP_WHATSAPP_RECIPIENTS': '["8801700000000"]'}
        self.patch = patch.object(wa, 'get_setting', side_effect=lambda k, default='': self.settings.get(k, default))
        self.patch.start();self.addCleanup(self.patch.stop)
        self.enabled = patch.object(wa, 'gateway_enabled', return_value=True)
        self.enabled.start();self.addCleanup(self.enabled.stop)
        self.result = {'ok': True, 'answer': 'Due amount: 100', 'tool': 'loan-report',
                       'plan': {'intent': 'mcp_tool_call'},
                       'raw': {'structuredContent': {'columns': ['DUEAMOUNT'], 'rows': [{'DUEAMOUNT': 100}]}}}
        wa.remember_result(self.user, self.result)
        self.payload = {'action': 'preview', 'result_id': self.result['whatsapp_result_id'],
                        'mode': 'full', 'recipient': '8801700000000'}
        self.client.force_login(self.user)

    def post(self, **changes):
        return self.client.post('/agentic/api/whatsapp/', json.dumps({**self.payload, **changes}), content_type='application/json')

    def test_preview_uses_stored_result_and_includes_table(self):
        response = self.post(answer='FORGED RESULT')
        self.assertEqual(response.status_code, 200)
        self.assertIn('DUEAMOUNT', response.json()['text'])
        self.assertNotIn('FORGED', response.json()['text'])
        self.assertFalse(AgentRunLog.objects.exists())

    def test_other_user_cannot_export_snapshot(self):
        other = User.objects.create_user('other')
        other.groups.add(self.user.groups.first())
        self.client.force_login(other)
        self.assertEqual(self.post().status_code, 400)

    def test_recipient_and_current_permissions_enforced(self):
        self.assertEqual(self.post(recipient='8801800000000').status_code, 403)
        self.report_tool.is_active = False;self.report_tool.save()
        self.assertEqual(self.post().status_code, 403)

    def test_requires_explicit_confirmation(self):
        self.assertEqual(self.post(action='send').status_code, 400)

    @patch.object(wa, 'load_gateway_config', return_value={'request_timeout': 2})
    @patch.object(wa, 'dispatch', new_callable=AsyncMock)
    def test_send_is_audited_and_duplicate_blocked(self, dispatch, config):
        dispatch.return_value = {'ok': True, 'message': 'Accepted'}
        response = self.post(action='send', confirmed=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AgentRunLog.objects.get().status, 'success')
        self.assertEqual(self.post(action='send', confirmed=True).status_code, 409)
        dispatch.assert_awaited_once()
        self.assertEqual(dispatch.call_args.args[2], '8801700000000')

    def test_oversized_full_result_blocked_but_summary_available(self):
        self.result['answer'] = 'a' * 5000
        wa.remember_result(self.user, self.result)
        self.payload['result_id'] = self.result['whatsapp_result_id']
        self.assertEqual(self.post().status_code, 400)
        response = self.post(mode='summary')
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.json()['text']), 1100)

    def test_csrf_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post('/agentic/api/whatsapp/', self.payload).status_code, 403)

    def test_number_validation(self):
        self.assertEqual(wa.recipient_text('+8801700000000\n8801700000000'), ['8801700000000'])
        with self.assertRaises(ValueError):
            wa.recipient_text('not-a-number')

    def test_controls_visible_only_with_authorized_configuration(self):
        response = self.client.get('/agentic/')
        self.assertContains(response, 'Send summary')
        self.assertContains(response, 'Send full result')
        self.tool.is_active = False; self.tool.save()
        response = self.client.get('/agentic/')
        self.assertNotContains(response, 'id="whatsapp-recipient"')
        self.assertContains(response, 'WhatsApp tool is disabled')
        self.assertContains(response, 'Send summary')

    def test_defaults_and_explicit_empty_configuration(self):
        self.settings.clear()
        self.assertEqual(wa.configuration(), {'tool': 'send-whatsapp-template',
                                             'recipients': ['8801859135478']})
        self.settings.update(MCP_WHATSAPP_TOOL='', MCP_WHATSAPP_RECIPIENTS='[]')
        self.assertFalse(wa.settings_status(self.user)['ready'])

    def test_status_does_not_leak_unauthorized_recipients(self):
        self.tool.allowed_roles = ['other-role']
        self.tool.save()
        status = wa.settings_status(self.user)
        self.assertFalse(status['ready'])
        self.assertEqual(status['recipients'], [])
        self.assertIn('not authorized', status['status'])

    @patch('agentic_ai.setup_views.discover_gateway', new_callable=AsyncMock)
    @patch('agentic_ai.setup_views.load_gateway_config', return_value={})
    def test_check_settings_validates_schema_without_sending(self, config, discover):
        from types import SimpleNamespace
        self.user.is_staff = True; self.user.is_superuser = True; self.user.save()
        discover.return_value = [SimpleNamespace(name=self.tool.name, inputSchema={
            'type': 'object', 'properties': {key: {} for key in
                ['messaging_product', 'to', 'type', 'text']}})]
        response = self.client.post('/agentic/setup/', json.dumps({
            'action': 'check_whatsapp'}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('No message sent', response.json()['message'])
        self.assertFalse(AgentRunLog.objects.exists())
        discover.return_value[0].inputSchema = {'type': 'object', 'properties': {}}
        self.assertEqual(self.client.post('/agentic/setup/', json.dumps({
            'action': 'check_whatsapp'}), content_type='application/json').status_code, 400)

    @patch('agentic_ai.setup_views._save_settings')
    def test_admin_saves_recipient_list_and_tool(self, save):
        self.user.is_staff = True; self.user.is_superuser = True; self.user.save()
        response = self.client.post('/agentic/setup/', json.dumps({
            'action': 'save_whatsapp', 'tool': self.tool.name,
            'recipients': '+8801700000000\n8801800000000'}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        save.assert_called_once_with({'MCP_WHATSAPP_TOOL': self.tool.name,
            'MCP_WHATSAPP_RECIPIENTS': '["8801700000000", "8801800000000"]'})

    def test_regular_user_cannot_edit_settings(self):
        self.user.is_staff = True; self.user.save()
        self.assertNotEqual(self.client.post('/agentic/setup/', json.dumps({
            'action': 'save_whatsapp', 'tool': self.tool.name, 'recipients': '8801800000000'}),
            content_type='application/json').status_code, 200)

    @patch.object(wa, 'load_gateway_config', return_value={'request_timeout': 2})
    @patch.object(wa, 'dispatch', new_callable=AsyncMock)
    def test_approval_required_is_not_reported_as_sent(self, dispatch, config):
        dispatch.return_value = {'ok': False, 'approval_required': True, 'http_status': 202,
                                 'answer': 'Approval required.'}
        response = self.post(action='send', confirmed=True)
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()['ok'])
        self.assertEqual(AgentRunLog.objects.get().status, 'approval_required')

    @patch('agentic_ai.api.execute_query')
    @patch('agentic_ai.bangla_summary.generate_summary', new_callable=AsyncMock)
    def test_combined_query_summary_and_whatsapp_preview(self, summarize, execute):
        report = {key: value for key, value in self.result.items() if key != 'whatsapp_result_id'}
        execute.return_value = report
        response = self.client.post('/agentic/api/query/', json.dumps({'message': 'Show loan report'}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertIn('summary_token', result)
        self.assertIn('whatsapp_result_id', result)
        summarize.return_value = {'ok': True, 'summary': 'পরিশোধযোগ্য বকেয়া 100।', 'model': 'openrouter/auto', 'usage': {}}
        analysis = self.client.post('/agentic/api/summary/', json.dumps({'summary_token': result['summary_token']}), content_type='application/json')
        self.assertEqual(analysis.status_code, 200)
        self.payload['result_id'] = result['whatsapp_result_id']
        preview = self.post(mode='summary')
        self.assertEqual(preview.status_code, 200)
        self.assertIn('Due amount: 100', preview.json()['text'])
        execute.assert_called_once()
        summarize.assert_awaited_once()
        page = self.client.get('/agentic/')
        for control in ('bangla-summary-button', 'report-chart-button', 'whatsapp-confirm'):
            self.assertContains(page, 'id="' + control + '"')


class GatewayDispatchTests(TestCase):
    def setUp(self):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        self.SimpleNamespace = SimpleNamespace
        self.session = AsyncMock()
        self.session.call_tool.return_value = SimpleNamespace(model_dump=lambda **kwargs: {
            'structuredContent': {'messages': [{'id': 'wamid.test'}]}})
        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield (None, None, None)
        @asynccontextmanager
        async def client(*args, **kwargs):
            yield self.session
        self.tool = SimpleNamespace(name='send-whatsapp-text', inputSchema={'type': 'object'})
        patches = [patch('mcp.client.streamable_http.streamablehttp_client', transport),
                   patch('mcp.ClientSession', client),
                   patch.object(wa, 'discover_tools', new=AsyncMock(return_value=[self.tool])),
                   patch.object(wa, '_decision', new=lambda *args: None)]
        for p in patches:
            p.start(); self.addCleanup(p.stop)

    def run_dispatch(self):
        from asgiref.sync import async_to_sync
        return async_to_sync(wa.dispatch)({'url': 'https://gateway.example/mcp', 'token': '', 'timeout': 2},
                                         self.tool.name, '8801700000000', 'Report result', None)

    def test_cloud_api_body_sent_to_exact_mcp_tool(self):
        self.assertTrue(self.run_dispatch()['ok'])
        self.session.call_tool.assert_awaited_once_with('send-whatsapp-text', arguments={
            'messaging_product': 'whatsapp', 'to': '8801700000000', 'type': 'text',
            'text': {'body': 'Report result'}})

    def test_error_response_not_reported_as_success(self):
        self.session.call_tool.return_value = self.SimpleNamespace(model_dump=lambda **kwargs: {
            'structuredContent': {'error': {'message': 'Not in service window'}}})
        self.assertFalse(self.run_dispatch()['ok'])

    def test_schema_mismatch_prevents_send(self):
        self.tool.inputSchema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
        with self.assertRaises(ValueError):
            self.run_dispatch()
        self.session.call_tool.assert_not_awaited()

    @patch.object(wa, '_decision', new=lambda *args: {'ok': False, 'approval_required': True})
    def test_approval_prevents_gateway_execution(self):
        self.assertTrue(self.run_dispatch()['approval_required'])
        self.session.call_tool.assert_not_awaited()
