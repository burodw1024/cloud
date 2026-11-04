import json
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from mcp.types import Tool

from agentic_ai import mcp_gateway as gateway
from agentic_ai.business_question_help import is_savings_report_tool
from .test_selection_repair import TOOL, ARGS, body
from .test_mcp_gateway import TOOL as LOAN

WHATSAPP = Tool(name='send-whatsapp-template', description='Send a savings result to WhatsApp',
    inputSchema={'type': 'object', 'properties': {
        key: {'type': 'string'} for key in ('messaging_product', 'to', 'type', 'text')},
        'required': ['messaging_product', 'to', 'type', 'text']})


class SavingsCatalogRoutingTests(SimpleTestCase):
    def setUp(self):
        self.config = {'provider': 'openai', 'model': 'test'}
        self.query = 'Show branch-wise savings deposits and withdrawals for last month.'

    @patch.object(gateway, '_model_response', new_callable=AsyncMock)
    def test_savings_prompt_only_advertises_authorized_savings_contract(self, model):
        model.return_value = body(ARGS)
        _, result = async_to_sync(gateway._select_tool)(self.config, self.query, [LOAN, WHATSAPP, TOOL])
        self.assertEqual(result, (TOOL.name, ARGS))
        definitions = model.call_args.args[1]['tools']
        self.assertEqual(len(definitions), 1)
        self.assertIn('savings-report:', definitions[0]['function']['description'])
        self.assertNotIn('send-whatsapp-template', json.dumps(definitions))
        self.assertNotIn('loans/aging.report', json.dumps(definitions))

    @patch.object(gateway, '_model_response', new_callable=AsyncMock)
    def test_no_savings_access_does_not_fall_back_to_messaging(self, model):
        message, result = async_to_sync(gateway._select_tool)(self.config, self.query, [LOAN, WHATSAPP])
        self.assertIsNone(result)
        self.assertIn('অনুমোদিত savings report tool', message['content'])
        model.assert_not_awaited()

    @patch.object(gateway, '_model_response', new_callable=AsyncMock)
    def test_bangla_savings_query_uses_same_restricted_catalog(self, model):
        model.return_value = body(ARGS)
        async_to_sync(gateway._select_tool)(self.config, 'গত মাসের সঞ্চয় জমা ও উত্তোলন শাখাভিত্তিক দেখাও।', [WHATSAPP, TOOL])
        self.assertEqual(len(model.call_args.args[1]['tools']), 1)

    def test_messaging_description_alone_cannot_make_it_a_savings_report_tool(self):
        self.assertFalse(is_savings_report_tool(WHATSAPP))
        self.assertFalse(is_savings_report_tool(LOAN))
        self.assertTrue(is_savings_report_tool(TOOL))

    @patch.object(gateway, '_model_response', new_callable=AsyncMock)
    def test_non_savings_selection_is_not_restricted_by_savings_routing(self, model):
        model.return_value = {'choices': [{'message': {'content': 'Please provide details.'}}]}
        async_to_sync(gateway._select_tool)(self.config, 'Which branch is overdue?', [LOAN, WHATSAPP, TOOL])
        self.assertEqual(len(model.call_args.args[1]['tools']), 3)

    @patch.object(gateway, '_model_response', new_callable=AsyncMock)
    def test_model_cannot_select_removed_messaging_alias(self, model):
        response = body(ARGS)
        response['choices'][0]['message']['tool_calls'][0]['function']['name'] = 'mcp_tool_1'
        model.return_value = response
        with self.assertRaises(gateway.GatewayError) as error:
            async_to_sync(gateway._select_tool)(self.config, self.query, [TOOL, WHATSAPP])
        self.assertEqual(error.exception.code, 'invalid_tool_call')
