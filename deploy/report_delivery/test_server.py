import base64
import json
from urllib.parse import parse_qs
from unittest import TestCase
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

from . import server


class ProviderRequestsTests(TestCase):
    def test_provider_message_contracts(self):
        calls = []

        def handle(request):
            calls.append(request)
            if request.url.path.endswith('/token'):
                return httpx.Response(200, json={'access_token': 'test-token'})
            if request.url.host == 'gmail.googleapis.com':
                return httpx.Response(200, json={'id': 'gmail-message'})
            if request.url.host == 'graph.microsoft.com':
                return httpx.Response(202)
            return httpx.Response(200, json={'status': 0, 'message_token': 42})

        client_factory = httpx.Client
        with patch.object(server.httpx, 'Client', side_effect=lambda **kwargs: client_factory(
                transport=httpx.MockTransport(handle), **kwargs)), patch.object(
                server, 'secret', side_effect=lambda key: {
                    'OUTLOOK_TENANT_ID': '11111111-1111-1111-1111-111111111111',
                    'GMAIL_REPORT_SENDER': 'sender@example.org',
                }.get(key, 'configured')):
            self.assertTrue(server.deliver_gmail_report('mail@example.org', 'Report', 'Text')['accepted'])
            self.assertTrue(server.deliver_outlook_report('mail@example.org', 'Report', 'Text')['accepted'])
            self.assertTrue(server.deliver_viber_report('subscriber', 'text', 'Text')['accepted'])
        gmail, outlook, viber = [item for item in calls if not item.url.path.endswith('/token')]
        self.assertIn(b'To: mail@example.org', base64.urlsafe_b64decode(json.loads(gmail.content)['raw']))
        self.assertEqual(json.loads(outlook.content)['message']['body']['content'], 'Text')
        self.assertEqual(json.loads(viber.content)['receiver'], 'subscriber')

    def test_twilio_formats_recipients_and_preserves_message_sid(self):
        calls = []
        sid = 'SM' + '1' * 32
        account = 'AC' + '2' * 32

        def handle(request):
            calls.append(request)
            return httpx.Response(201, json={'sid': sid, 'status': 'queued'})

        client_factory = httpx.Client
        with patch.object(server.httpx, 'Client', side_effect=lambda **kwargs: client_factory(
                transport=httpx.MockTransport(handle), **kwargs)), patch.dict(server.os.environ, {
                    'TWILIO_ACCOUNT_SID': account,
                    'TWILIO_AUTH_TOKEN': 'test-token',
                    'TWILIO_WHATSAPP_FROM': '+15551234567',
                    'TWILIO_SMS_FROM': '+15551234568',
                }):
            for result in (
                server.deliver_twilio_whatsapp_report('whatsapp', '15551234569', 'text', {'body': 'Report'}),
                server.deliver_twilio_sms_report('15551234569', 'Report'),
            ):
                self.assertEqual(result, {'accepted': True, 'provider': 'Twilio', 'message_id': sid})
            self.assertFalse(server.deliver_twilio_sms_report('invalid', 'Report')['accepted'])

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].url.path, f'/2010-04-01/Accounts/{account}/Messages.json')
        self.assertEqual(parse_qs(calls[0].content.decode()), {
            'From': ['whatsapp:+15551234567'], 'To': ['whatsapp:+15551234569'], 'Body': ['Report'],
        })
        self.assertEqual(parse_qs(calls[1].content.decode()), {
            'From': ['+15551234568'], 'To': ['+15551234569'], 'Body': ['Report'],
        })

    def test_streamable_endpoint_requires_auth_and_exposes_tools(self):
        with patch.dict(server.os.environ, {'REPORT_DELIVERY_MCP_TOKEN': 'test'}), TestClient(
                server.app, base_url='http://localhost:8080') as client:
            self.assertEqual(client.post('/mcp').status_code, 401)
            response = client.post('/mcp', headers={
                'Authorization': 'Bearer test', 'Accept': 'application/json, text/event-stream',
            }, json={'jsonrpc': '2.0', 'method': 'tools/list', 'id': 1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual({tool['name'] for tool in response.json()['result']['tools']},
                             {'deliver_gmail_report', 'deliver_outlook_report', 'deliver_viber_report',
                              'deliver_twilio_whatsapp_report', 'deliver_twilio_sms_report'})
