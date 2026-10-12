import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase
from mcp.types import Tool, ToolAnnotations, CallToolResult, TextContent

from . import mcp_agent


class MCPAgentTests(SimpleTestCase):
    def session(self, destructive=False):
        tool = Tool(name='nifi-db-read', description='Read a branch summary',
                    inputSchema={'type': 'object', 'properties': {'branch': {'type': 'string'}},
                                 'required': ['branch'], 'additionalProperties': False},
                    annotations=ToolAnnotations(destructiveHint=destructive))
        return SimpleNamespace(
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[tool], nextCursor=None)),
            call_tool=AsyncMock(return_value=CallToolResult(
                content=[TextContent(type='text', text='{"balance": 42}')], isError=False)),
        )

    def execute(self, session, replies):
        with patch.dict(os.environ, {'AGENT_MCP_ALLOWED_TOOLS': 'nifi-db-read'}), \
             patch.object(mcp_agent, 'model_reply', side_effect=replies):
            return asyncio.run(mcp_agent.conversation(session, 'Branch B001 summary'))

    def call(self, name='nifi-db-read', args=None):
        return {'role': 'assistant', 'content': '', 'tool_calls': [
            {'function': {'name': name, 'arguments': args if args is not None else {'branch': 'B001'}}}]}

    def test_selection_execution_and_result_back_to_model(self):
        session = self.session()
        def reply(messages, tools):
            self.assertEqual(tools[0]['function']['parameters']['required'], ['branch'])
            if len(messages) == 2:
                return self.call()
            self.assertEqual(messages[-1]['role'], 'tool')
            self.assertIn('42', messages[-1]['content'])
            return {'content': 'Balance is 42.'}
        result = self.execute(session, reply)
        self.assertEqual(result['answer'], 'Balance is 42.')
        session.call_tool.assert_awaited_once_with('nifi-db-read', arguments={'branch': 'B001'})

    def test_unknown_tool_is_never_executed(self):
        session = self.session()
        with self.assertRaises(PermissionError):
            self.execute(session, [self.call('nifi-db-write')])
        session.call_tool.assert_not_awaited()

    def test_invalid_arguments_never_executed(self):
        from jsonschema import ValidationError
        session = self.session()
        with self.assertRaises(ValidationError):
            self.execute(session, [self.call(args={'sql': 'DELETE FROM loans'})])
        session.call_tool.assert_not_awaited()

    def test_destructive_tools_not_offered(self):
        session = self.session(destructive=True)
        with self.assertRaises(ValueError):
            self.execute(session, [])
        session.call_tool.assert_not_awaited()

    def test_tool_error_does_not_report_success(self):
        session = self.session()
        session.call_tool.return_value.isError = True
        with self.assertRaises(RuntimeError):
            self.execute(session, [self.call()])

    def test_repeated_tool_calls_are_bounded(self):
        session = self.session()
        with self.assertRaises(ValueError):
            self.execute(session, [self.call()] * 4)
        self.assertEqual(session.call_tool.await_count, 3)

    def test_clarification_without_tool_execution(self):
        session = self.session()
        result = self.execute(session, [{'content': 'Which branch?'}])
        self.assertTrue(result['ok'])
        session.call_tool.assert_not_awaited()

    @patch.object(mcp_agent, 'write_agent_log')
    @patch.object(mcp_agent, 'connect', new_callable=AsyncMock)
    def test_anonymous_user_blocked_before_connection(self, connect, audit):
        result = mcp_agent.run(SimpleNamespace(is_authenticated=False), 'query')
        self.assertFalse(result['ok'])
        connect.assert_not_awaited()
        self.assertEqual(audit.call_args.kwargs['status'], 'blocked')

    @patch.object(mcp_agent, 'write_agent_log')
    @patch.object(mcp_agent, 'connect', new_callable=AsyncMock)
    def test_errors_do_not_expose_secrets(self, connect, audit):
        connect.side_effect = RuntimeError('Bearer private-secret')
        result = mcp_agent.run(SimpleNamespace(is_authenticated=True, is_superuser=True), 'query')
        self.assertFalse(result['ok'])
        self.assertNotIn('private-secret', str(result))
        self.assertNotIn('private-secret', str(audit.call_args))


class MCPDiagnosticTests(SimpleTestCase):
    def test_missing_configuration_has_actionable_error(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with patch.dict(os.environ, {'AGENT_MCP_URL': '', 'AGENT_MCP_TOKEN': ''}):
            with self.assertRaisesMessage(CommandError, 'Set AGENT_MCP_URL'):
                call_command('check_mcp')

    def test_discovery_and_fixed_read_success(self):
        from django.core.management import call_command
        from io import StringIO
        output = StringIO()
        with patch.dict(os.environ, {'AGENT_MCP_URL': 'http://fixture/mcp', 'AGENT_MCP_TOKEN': 'test'}), \
             patch('agentic_ai.management.commands.check_mcp.probe', new_callable=AsyncMock) as probe:
            probe.return_value = ['nifi-db-read']
            call_command('check_mcp', '--test-read', stdout=output)
            probe.assert_awaited_once_with(True)
        self.assertIn('MCP_TEST=1', output.getvalue())

    def test_diagnostic_failure_hides_credentials(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with patch.dict(os.environ, {'AGENT_MCP_URL': 'http://fixture/mcp', 'AGENT_MCP_TOKEN': 'test'}), \
             patch('agentic_ai.management.commands.check_mcp.probe', new_callable=AsyncMock) as probe:
            probe.side_effect = RuntimeError('private-token')
            with self.assertRaises(CommandError) as error:
                call_command('check_mcp')
        self.assertNotIn('private-token', str(error.exception))
