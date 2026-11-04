import json
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from mcp.types import Tool, CallToolResult, TextContent

from agentic_ai import mcp_gateway as gateway
from .test_mcp_gateway import TOOL as LOAN_TOOL

SCHEMA = {'type': 'object', 'properties': {
    'operation': {'type': 'string', 'enum': ['branch-summary', 'period-summary']},
    'from_date': {'type': 'string', 'format': 'date'},
    'to_date_exclusive': {'type': 'string', 'format': 'date'},
    'branch_code': {'type': 'string', 'pattern': '^[0-9]{4}$'},
    'zone_code': {'type': 'string', 'minLength': 1}},
    'required': ['operation', 'from_date', 'to_date_exclusive'],
    'additionalProperties': False,
    'allOf': [{'if': {'properties': {'operation': {'const': 'period-summary'}}, 'required': ['operation']},
               'then': {'required': ['branch_code'], 'not': {'required': ['zone_code']}}}]}
TOOL = Tool(name='savings-report', description='Savings deposits and withdrawals', inputSchema=SCHEMA)
ARGS = {'operation': 'branch-summary', 'from_date': '2026-10-01', 'to_date_exclusive': '2026-11-01'}


def body(args):
    return {'choices': [{'message': {'tool_calls': [{'function': {'name': 'mcp_tool_0', 'arguments': json.dumps(args)}}]}}]}


class SelectionRepairTests(SimpleTestCase):
    def setUp(self):
        self.config = {'provider': 'openai', 'model': 'test', 'model_url': 'https://example/v1',
                       'model_key': '', 'timeout': 1}
        self.query = 'Show branch-wise savings deposits and withdrawals for last month.'

    @patch.object(gateway, '_model_response', new_callable=AsyncMock)
    @patch.object(gateway.timezone, 'localdate', return_value=date(2026, 11, 4))
    def test_mixed_catalog_invalid_optional_value_is_repaired_once(self, today, model):
        model.side_effect = [body({**ARGS, 'zone_code': ''}), body(ARGS)]
        _, selection = async_to_sync(gateway._select_tool)(self.config, self.query, [LOAN_TOOL, TOOL])
        self.assertEqual(selection, ('savings-report', ARGS))
        self.assertEqual(model.await_count, 2)
        prompt = model.call_args.args[1]['messages'][0]['content']
        self.assertIn('from_date=2026-10-01', prompt)
        self.assertIn('to_date_exclusive=2026-11-01', prompt)
        self.assertIn('failed at zone_code', prompt)
        self.assertIn('branch-summary', prompt)

    @patch.object(gateway, '_model_response', new_callable=AsyncMock)
    def test_repeated_invalid_arguments_fail_closed_with_field_and_rule(self, model):
        model.return_value = body({**ARGS, 'zone_code': ''})
        with self.assertRaises(gateway.GatewayError) as error:
            async_to_sync(gateway._select_tool)(self.config, self.query, [LOAN_TOOL, TOOL])
        self.assertEqual(model.await_count, 2)
        self.assertEqual(error.exception.code, 'invalid_arguments')
        self.assertIn('zone_code', str(error.exception))
        self.assertIn('minLength', str(error.exception))

    @patch.object(gateway, '_model_response', new_callable=AsyncMock)
    def test_http_timeout_is_not_retried(self, model):
        model.side_effect = httpx.ReadTimeout('private upstream detail')
        with self.assertRaises(httpx.ReadTimeout):
            async_to_sync(gateway._select_tool)(self.config, self.query, [LOAN_TOOL, TOOL])
        model.assert_awaited_once()

    def test_diagnostics_never_log_argument_values_or_extra_keys(self):
        invalid = body({**ARGS, 'private_key_name': 'secret-financial-data'})['choices'][0]['message']
        with self.assertLogs('agentic_ai.mcp_gateway', level='WARNING') as logs:
            with self.assertRaises(gateway.GatewayError) as error:
                gateway._validate_call(invalid, {'mcp_tool_0': 'savings-report'}, {'mcp_tool_0': SCHEMA})
        for text in (str(logs.output), str(error.exception), error.exception.selection_feedback):
            self.assertNotIn('private_key_name', text)
            self.assertNotIn('secret-financial-data', text)
            self.assertNotIn('2026-10-01', text)

    @patch.object(gateway, '_model_response', new_callable=AsyncMock)
    def test_missing_required_branch_is_not_invented_by_application(self, model):
        model.return_value = body({**ARGS, 'operation': 'period-summary'})
        with self.assertRaises(gateway.GatewayError):
            async_to_sync(gateway._select_tool)(self.config, self.query, [LOAN_TOOL, TOOL])
        self.assertIn('branch_code', model.call_args.args[1]['messages'][0]['content'])
        self.assertNotIn('"0253"', str(model.call_args.args[1]))

    @patch.object(gateway, '_model_response', new_callable=AsyncMock)
    def test_repaired_selection_dispatches_report_exactly_once(self, model):
        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield None, None, None
        session = AsyncMock(); session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(tools=[LOAN_TOOL, TOOL], nextCursor=None)
        session.call_tool.return_value = CallToolResult(content=[TextContent(type='text', text='Report result')])
        model.side_effect = [body({**ARGS, 'zone_code': None}), body(ARGS)]
        config = {**self.config, 'url': 'http://gateway/mcp', 'token': ''}
        with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
             patch('mcp.ClientSession', return_value=session), \
             patch.object(gateway, '_decision', return_value=None):
            result = async_to_sync(gateway._run)(config, None, self.query, {LOAN_TOOL.name, TOOL.name})
        self.assertTrue(result['ok'], result)
        session.call_tool.assert_awaited_once_with('savings-report', arguments=ARGS)
