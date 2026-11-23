import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from agentic_ai.models import ReportRun
from agentic_ai.whatsapp import remember_result


class TelegramShareTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username='report-reader')
        self.permission = Permission.objects.get(codename='share_report_telegram')
        self.user.user_permissions.add(self.permission)
        self.client.force_login(self.user)
        config = patch.dict('os.environ', {
            'TELEGRAM_BOT_TOKEN': '12345:' + 't' * 36,
            'TELEGRAM_REPORT_ORIGIN': 'https://insight.example.org',
            'TELEGRAM_USER_CHATS': json.dumps({str(self.user.pk): '123456'}),
        })
        config.start()
        self.addCleanup(config.stop)
        self.run_record = ReportRun.objects.create(user=self.user, snapshot_token='test-snapshot',
            question='Private member ID 777123', source={}, context={})
        self.result = {'ok': True, 'report_run_id': str(self.run_record.pk), 'answer': 'Private member ID 777123'}
        remember_result(self.user, self.result)
        self.endpoint = reverse('agentic_ai:report_share_api', args=['telegram'])

    def report(self, level):
        cache.set('agentic:summary:test-snapshot', {'user_id': self.user.pk, 'report': {
            'level': level, 'columns': ['BRANCHCODE', 'LOAN_COUNT', 'MEMBERID'],
            'rows': [{'BRANCHCODE': '100', 'LOAN_COUNT': '25', 'MEMBERID': '777123'}],
        }})

    def request_share(self, action, **extra):
        return self.client.post(self.endpoint, {
            'action': action, 'mode': 'summary', 'result_id': self.result['telegram_result_id'],
            'recipient': '123456', 'confirmed': True, **extra,
        }, content_type='application/json')

    @patch('agentic_ai.telegram.bot_request')
    def test_preview_then_send_chart_with_report_link(self, bot):
        self.report('BRANCH')
        self.assertEqual(self.request_share('send').status_code, 400)
        preview = self.request_share('preview').json()
        self.assertTrue(preview['includes_chart'])
        self.assertIn('100: 25', preview['text'])
        self.assertNotIn('777123', preview['text'])
        self.assertTrue(preview['report_url'].endswith(reverse('agentic_ai:report_run', args=[self.run_record.pk])))
        bot.assert_not_called()
        bot.side_effect = [{'type': 'private', 'id': 123456}, {'message_id': 1}]
        self.assertEqual(self.request_share('send').status_code, 200)
        delivery = bot.call_args
        self.assertEqual(delivery.args[1], 'sendPhoto')
        self.assertEqual(delivery.kwargs['files']['photo'][1][:8], b'\x89PNG\r\n\x1a\n')
        self.assertIn(preview['report_url'], delivery.kwargs['data']['reply_markup'])
        self.assertEqual(self.request_share('send').status_code, 409)
        self.assertEqual(bot.call_count, 2)

    @patch('agentic_ai.telegram.bot_request')
    def test_member_rows_are_excluded_and_non_private_chat_is_rejected(self, bot):
        self.report('MEMBER')
        self.assertEqual(self.request_share('preview', mode='full').status_code, 400)
        self.assertEqual(self.request_share('preview', recipient='111111').status_code, 403)
        preview = self.request_share('preview').json()
        self.assertFalse(preview['includes_chart'])
        self.assertNotIn('777123', preview['text'])
        bot.return_value = {'type': 'group', 'id': 123456}
        self.assertEqual(self.request_share('send').status_code, 502)
        self.assertEqual(bot.call_count, 1)

    @patch('agentic_ai.telegram.bot_request')
    def test_member_report_sends_notification_only(self, bot):
        self.report('MEMBER')
        preview = self.request_share('preview').json()
        bot.side_effect = [{'type': 'private', 'id': 123456}, {'message_id': 2}]
        self.assertEqual(self.request_share('send').status_code, 200)
        self.assertEqual(bot.call_args.args[1], 'sendMessage')
        self.assertEqual(bot.call_args.kwargs['json']['text'], preview['text'])

    @patch('agentic_ai.telegram.bot_request')
    def test_revoked_permission_prevents_send(self, bot):
        self.report('BRANCH')
        self.assertEqual(self.request_share('preview').status_code, 200)
        self.user.user_permissions.remove(self.permission)
        self.assertEqual(self.request_share('send').status_code, 403)
        bot.assert_not_called()
