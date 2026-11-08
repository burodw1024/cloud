from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from mcp.types import Tool

from agentic_ai.access_readiness import report_readiness
from agentic_ai.models import AgentTool


class AccessReadinessTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser('root', password='test')
        self.record = AgentTool.objects.create(name='loan-due-report', requires_loan_scope=True)
        self.client.force_login(self.root)

    def schema(self, signed=True):
        return Tool(name=self.record.name, inputSchema={'type': 'object',
            'properties': {'loan_access_token': {'type': 'string'}} if signed else {},
            'required': ['loan_access_token'] if signed else []})

    def test_missing_token_and_signing_and_fixed_modes_are_distinguished(self):
        def code(tools, key=True):
            return report_readiness([self.record], tools, key, {'loan-due-report'})[0]['code']
        self.assertEqual(code([self.schema(False)]), 'missing_token_contract')
        self.assertEqual(code([self.schema()], False), 'missing_signing_key')
        self.assertEqual(code([self.schema()]), 'schema_detected')
        self.assertEqual(code([]), 'not_discovered')
        self.record.loan_access_mode = 'FIXED'
        self.assertEqual(code([self.schema()]), 'fixed_scope')
        self.record.is_active = False
        self.assertEqual(code([self.schema()]), 'disabled')

    @patch('agentic_ai.mcp_gateway.gateway_enabled', return_value=True)
    @patch('agentic_ai.mcp_gateway.discover_gateway')
    @patch('agentic_ai.mcp_gateway.load_gateway_config', return_value={'timeout':1500, 'request_timeout':1800})
    def test_check_discovers_without_executing_tools_or_revealing_secrets(self, config, discovery, enabled):
        async def discover(config):
            self.assertEqual(config['request_timeout'], 25)
            return [self.schema(False)]
        discovery.side_effect = discover
        response = self.client.post('/agentic/api/access-readiness/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['tools'][0]['code'], 'missing_token_contract')
        self.assertFalse(response.json()['report_executed'])
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_root_and_csrf_are_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.root)
        self.assertEqual(client.post('/agentic/api/access-readiness/').status_code, 403)
        staff = User.objects.create_user('staff', is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.post('/agentic/api/access-readiness/').status_code, 403)

    @patch('agentic_ai.mcp_gateway.gateway_enabled', return_value=True)
    @patch('agentic_ai.mcp_gateway.load_gateway_config', side_effect=ValueError('secret-value'))
    def test_configuration_errors_are_sanitized(self, config, enabled):
        response = self.client.post('/agentic/api/access-readiness/')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('secret-value', response.content.decode())

class AccessReadinessCommandTests(TestCase):
    def setUp(self):
        AgentTool.objects.create(name='loan-due-report', requires_loan_scope=True)

    @patch('agentic_ai.management.commands.check_loan_backend.gateway_enabled', return_value=True)
    @patch('agentic_ai.management.commands.check_loan_backend.load_gateway_config', return_value={'timeout':1500, 'request_timeout':1800})
    @patch('agentic_ai.management.commands.check_loan_backend.discover_gateway')
    def test_management_check_rejects_missing_schema_without_calling_report(self, discover, config, enabled):
        import io
        from django.core.management import call_command
        from django.core.management.base import CommandError
        async def discovery(config):
            return [Tool(name='loan-due-report', inputSchema={'type':'object','properties':{}})]
        discover.side_effect = discovery
        output = io.StringIO()
        with self.assertRaises(CommandError):
            call_command('check_loan_backend', tool='loan-due-report', stdout=output)
        self.assertIn('missing_token_contract', output.getvalue())
        self.assertIn('"report_executed": false', output.getvalue())

    @patch('agentic_ai.management.commands.check_loan_backend.gateway_enabled', return_value=True)
    @patch('agentic_ai.management.commands.check_loan_backend.load_gateway_config', side_effect=ValueError('secret-value'))
    def test_management_discovery_error_is_sanitized(self, config, enabled):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError) as error:
            call_command('check_loan_backend', tool='loan-due-report')
        self.assertNotIn('secret-value', str(error.exception))
