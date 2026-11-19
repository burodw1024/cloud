from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import Client, TestCase
from agentic_ai import whatsapp
from . import test_whatsapp


class ServiceWorkflowTests(TestCase):
    setUp = test_whatsapp.WhatsAppTests.setUp
    check_url = '/agentic/api/services/whatsapp/check/'

    @patch.object(whatsapp, 'dispatch', new_callable=AsyncMock)
    @patch('agentic_ai.mcp_gateway.discover_gateway', new_callable=AsyncMock)
    def test_services_are_visible_without_automatic_external_calls(self, discover, dispatch):
        page = self.client.get('/agentic/services/')
        self.assertContains(page, 'WhatsApp')
        self.assertContains(page, 'Superset reporting')
        self.assertContains(page, 'Confirm and send')
        self.assertContains(page, 'Check WhatsApp connection')
        self.assertContains(page, 'Needs setup')
        discover.assert_not_awaited()
        dispatch.assert_not_awaited()
        self.assertEqual(page['Cache-Control'], 'no-store')

    @patch.object(whatsapp, 'dispatch', new_callable=AsyncMock)
    @patch.object(whatsapp, 'load_gateway_config', return_value={})
    @patch('agentic_ai.mcp_gateway.discover_gateway', new_callable=AsyncMock)
    def test_authorized_service_check_checks_text_schema_without_sending(self, discover, config, dispatch):
        discover.return_value = [SimpleNamespace(name=self.tool.name, inputSchema={
            'type': 'object', 'properties': {key: {} for key in ('messaging_product', 'to', 'type', 'text')}})]
        reply = self.client.post(self.check_url)
        self.assertEqual(reply.status_code, 200, reply.content)
        self.assertIn('No message was sent', reply.json()['message'])
        self.assertNotIn('8801700000000', reply.content.decode())
        dispatch.assert_not_awaited()
        self.assertEqual(self.client.post(self.check_url).status_code, 429)

    @patch.object(whatsapp, 'load_gateway_config', return_value={})
    @patch('agentic_ai.mcp_gateway.discover_gateway', new_callable=AsyncMock)
    def test_template_only_service_has_actionable_error(self, discover, config):
        discover.return_value = [SimpleNamespace(name=self.tool.name, inputSchema={
            'type': 'object', 'properties': {'template': {'type': 'object'}}, 'required': ['template']})]
        result = self.client.post(self.check_url)
        self.assertEqual(result.status_code, 400)
        self.assertIn('template-only', result.json()['error'])

    @patch('agentic_ai.mcp_gateway.discover_gateway', new_callable=AsyncMock)
    def test_unauthorized_check_does_not_discover_or_expose_recipients(self, discover):
        self.tool.allowed_roles = ['different-role']
        self.tool.save()
        result = self.client.post(self.check_url)
        self.assertEqual(result.status_code, 403)
        self.assertNotIn('8801700000000', result.content.decode())
        discover.assert_not_awaited()
        page = self.client.get('/agentic/services/')
        self.assertNotContains(page, 'id="whatsapp-service-check"')

    def test_service_checks_require_authentication_post_and_csrf(self):
        self.assertEqual(Client().post(self.check_url).status_code, 401)
        self.assertEqual(self.client.get(self.check_url).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.check_url).status_code, 403)
        self.assertEqual(self.client.post('/agentic/services/').status_code, 405)

    def test_superset_remains_discoverable_before_service_configuration(self):
        page = self.client.get('/agentic/')
        self.assertContains(page, 'Generate a Superset report')
        self.assertContains(page, 'Service status &amp; instructions')
        self.assertNotContains(page, 'id="generate-superset-report"')

    @patch('agentic_ai.workspace.gateway_enabled', return_value=True)
    def test_registered_whatsapp_service_opens_instructions_instead_of_a_generic_question(self, enabled):
        page = self.client.get('/agentic/')
        capability = next(tool for tool in page.context['capabilities'] if tool['name'] == self.tool.name)
        self.assertEqual(capability['service_url'], '/agentic/services/#whatsapp-service-title')
        self.assertContains(page, 'href="/agentic/services/">Services</a>')
