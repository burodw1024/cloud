import base64
import json
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

    def test_streamable_endpoint_requires_auth_and_exposes_tools(self):
        with patch.dict(server.os.environ, {'REPORT_DELIVERY_MCP_TOKEN': 'test'}), TestClient(
                server.app, base_url='http://localhost:8080') as client:
            self.assertEqual(client.post('/mcp').status_code, 401)
            response = client.post('/mcp', headers={
                'Authorization': 'Bearer test', 'Accept': 'application/json, text/event-stream',
            }, json={'jsonrpc': '2.0', 'method': 'tools/list', 'id': 1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual({tool['name'] for tool in response.json()['result']['tools']},
                             {'deliver_gmail_report', 'deliver_outlook_report', 'deliver_viber_report'})
