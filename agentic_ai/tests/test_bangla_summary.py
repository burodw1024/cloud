import json
from unittest.mock import AsyncMock, patch
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, Client
from agentic_ai import bangla_summary as summary
from agentic_ai.models import AgentTool


class BanglaSummaryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username='summary-user')
        self.client.force_login(self.user)
        self.tool = AgentTool.objects.create(name='loan-due-report', allowed_roles=['staff'])
        from django.contrib.auth.models import Group
        self.user.groups.add(Group.objects.get_or_create(name='staff')[0])
        from agentic_ai.models import LoanZone, LoanBranch, LoanAccessPolicy
        from agentic_ai.loan_access import snapshot
        zone = LoanZone.objects.create(code='07', name='Test zone')
        LoanBranch.objects.create(code='0253', name='Test branch', zone=zone)
        LoanAccessPolicy.objects.create(user=self.user, mode='ALL')
        access = snapshot(self.user)
        self.result = {'ok': True, 'tool': self.tool.name, 'plan': {'intent': 'mcp_tool_call'},
            'raw': {'structuredContent': {'columns': ['DUEAMOUNT', 'MEMBERID', 'PHONENO', 'MEMBERNAME'],
                'rows': [{'DUEAMOUNT': '574528', 'MEMBERID': 'private-id', 'PHONENO': 'private-phone', 'MEMBERNAME': 'private-name'}],
                'row_count': 1, 'report_date': '2026-09-30', 'truncated': False}}}
        self.result['loan_access'] = {'subject': access['subject'], 'version': access['version']}
        self.token = summary.remember_report(self.user, self.result)['summary_token']
        self.url = '/agentic/api/summary/'

    def post(self, body=None, client=None):
        return (client or self.client).post(self.url, data=json.dumps(body or {'summary_token': self.token}), content_type='application/json')

    def test_report_is_server_held_and_personal_fields_removed(self):
        saved = cache.get('agentic:summary:' + self.token)
        self.assertEqual(saved['report']['rows'], [{'DUEAMOUNT': '574528'}])
        for private in ['private-name', 'private-phone', 'private-id']:
            self.assertNotIn(private, json.dumps(saved))

    @patch.object(summary, 'generate_summary', new_callable=AsyncMock)
    def test_summary_once_and_cached_replay_without_query(self, generate):
        generate.return_value = {'ok': True, 'summary': 'বকেয়া 574528।', 'model': 'openrouter/auto', 'usage': {}}
        self.assertEqual(self.post().status_code, 200)
        self.assertTrue(self.post().json()['replayed'])
        generate.assert_awaited_once()
        self.assertEqual(generate.call_args.args[0]['rows'], [{'DUEAMOUNT': '574528'}])

    @patch.object(summary, 'generate_summary', new_callable=AsyncMock)
    def test_other_user_cannot_summarize(self, generate):
        other = get_user_model().objects.create_user(username='other')
        self.client.force_login(other)
        self.assertEqual(self.post().status_code, 404)
        generate.assert_not_called()

    @patch.object(summary, 'generate_summary', new_callable=AsyncMock)
    def test_roles_rechecked(self, generate):
        self.tool.is_active = False
        self.tool.save()
        self.assertEqual(self.post().status_code, 403)
        generate.assert_not_called()

    @patch.object(summary, 'generate_summary', new_callable=AsyncMock)
    def test_data_scope_revocation_blocks_stored_summary(self, generate):
        from agentic_ai.models import LoanAccessPolicy
        LoanAccessPolicy.objects.filter(user=self.user).update(mode='NONE')
        self.assertEqual(self.post().status_code, 403)
        generate.assert_not_called()

    def test_anonymous_and_csrf(self):
        self.assertEqual(self.post(client=Client()).status_code, 401)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(self.post(client=client).status_code, 403)

    def test_cannot_submit_arbitrary_financial_data(self):
        self.assertEqual(self.post({'summary_token': self.token, 'rows': [{'DUEAMOUNT': 1}]}).status_code, 400)
        self.assertEqual(self.post({'summary_token': 'x'}).status_code, 400)

    def test_expired_and_pending(self):
        key = 'agentic:summary:' + self.token
        cache.add(key + ':pending', True, 150)
        self.assertEqual(self.post().status_code, 409)
        cache.delete(key)
        self.assertEqual(self.post().status_code, 404)

    def test_no_button_for_failed_or_empty_result(self):
        self.result['ok'] = False
        self.assertNotIn('summary_token', summary.remember_report(self.user, self.result))

    @patch.object(summary, 'generate_summary', new_callable=AsyncMock, side_effect=RuntimeError('secret provider body'))
    def test_provider_error_preserves_report_and_hides_details(self, generate):
        response = self.post()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('secret provider body', response.content.decode())
        self.assertIsNotNone(cache.get('agentic:summary:' + self.token))

    @patch.object(summary, 'get_setting')
    def test_gateway_model_endpoint_and_token(self, setting):
        values = {'MCP_GATEWAY_URL': 'http://gateway:4444/servers/abc/mcp', 'MCP_GATEWAY_TOKEN': 'gateway-token'}
        setting.side_effect = lambda key, default='': values.get(key, default)
        config = summary.summary_config()
        self.assertEqual(config['model_url'], 'http://gateway:4444/v1')
        self.assertEqual(config['model'], 'openrouter/auto')
        self.assertEqual(config['model_key'], 'gateway-token')
        values['MCP_SUMMARY_BASE_URL'] = 'https://other.example/v1'
        with self.assertRaises(summary.GatewayError):
            summary.summary_config()

    @patch.object(summary, 'summary_config', return_value={'model': 'openrouter/auto'})
    @patch.object(summary, '_model_response', new_callable=AsyncMock)
    def test_generation_preserves_supplied_numbers(self, model, config):
        from asgiref.sync import async_to_sync
        model.return_value = {'choices': [{'message': {'content': 'বকেয়া 574528।'}}], 'usage': {'total_tokens': 99}}
        result = async_to_sync(summary.generate_summary)({'rows': [{'DUEAMOUNT': '574528'}]})
        self.assertEqual(result['summary'], 'বকেয়া 574528।')
        payload = model.call_args.args[1]
        self.assertFalse(payload['stream'])
        self.assertNotIn('tools', payload)
        model.return_value['choices'][0]['message']['content'] = 'মোট বকেয়া 999999।'
        with self.assertRaises(ValueError):
            async_to_sync(summary.generate_summary)({'rows': [{'DUEAMOUNT': '574528'}]})

    @patch.object(summary, 'generate_summary', new_callable=AsyncMock)
    def test_access_revoked_during_inference(self, generate):
        async def revoke(report):
            from asgiref.sync import sync_to_async
            await sync_to_async(AgentTool.objects.filter(pk=self.tool.pk).update)(is_active=False)
            return {'ok': True, 'summary': 'বিশ্লেষণ', 'model': 'openrouter/auto', 'usage': {}}
        generate.side_effect = revoke
        self.assertEqual(self.post().status_code, 403)

    def test_equivalent_formatted_numbers_and_bangla_date_are_accepted(self):
        body = {'choices': [{'message': {'content': '৩০ সেপ্টেম্বর ২০২৬ অনুযায়ী বকেয়া ৫৭৪,৫২৮.০০।'}}]}
        report = {'report_date': '2026-09-30', 'rows': [{'DUEAMOUNT': '574528'}]}
        self.assertEqual(summary.validate_summary(body, report), body['choices'][0]['message']['content'])
        body['choices'][0]['message']['content'] = 'বকেয়া ৫,৭৪,৫২৮।'
        summary.validate_summary(body, report)
        body['choices'][0]['message']['content'] = 'বকেয়া 574,529.00।'
        with self.assertRaises(summary.SummaryValidationError):
            summary.validate_summary(body, report)

    @patch.object(summary, 'summary_config', return_value={'model': 'openrouter/auto'})
    @patch.object(summary, '_model_response', new_callable=AsyncMock)
    def test_invalid_summary_gets_one_retry_and_both_calls_usage_is_counted(self, model, config):
        from asgiref.sync import async_to_sync
        def answer(text):
            return {'choices': [{'message': {'content': text}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}
        model.side_effect = [answer('বকেয়া 999999।'), answer('বকেয়া 574528।')]
        result = async_to_sync(summary.generate_summary)({'rows': [{'DUEAMOUNT': '574528'}]})
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['usage']['total_tokens'], 30)
        self.assertEqual(model.await_count, 2)

    @patch.object(summary, 'summary_config', return_value={'model': 'openrouter/auto'})
    @patch.object(summary, '_model_response', new_callable=AsyncMock)
    def test_http_failure_is_not_retried(self, model, config):
        from asgiref.sync import async_to_sync
        import httpx
        model.side_effect = httpx.ReadTimeout('private provider detail')
        with self.assertRaises(httpx.ReadTimeout):
            async_to_sync(summary.generate_summary)({'rows': []})
        model.assert_awaited_once()

    @patch.object(summary, 'generate_summary', new_callable=AsyncMock)
    def test_failure_code_logged_without_provider_data_or_report_contents(self, generate):
        generate.side_effect = RuntimeError('secret provider body private-name 574528')
        with self.assertLogs('agentic_ai.bangla_summary', level='WARNING') as logs:
            response = self.post()
        self.assertEqual(response.json()['code'], 'summary_failed')
        self.assertNotIn('llm.invoke', response.json()['error'])
        for secret in ('secret provider body', 'private-name', '574528', self.token):
            self.assertNotIn(secret, str(logs.output))
            self.assertNotIn(secret, response.content.decode())

    @patch.object(summary, 'generate_summary', new_callable=AsyncMock)
    def test_validation_failure_has_specific_code_and_preserves_original(self, generate):
        generate.side_effect = summary.SummaryValidationError('summary_unsupported_numbers')
        response = self.post()
        self.assertEqual(response.json()['code'], 'summary_unsupported_numbers')
        self.assertIsNotNone(cache.get('agentic:summary:' + self.token))
