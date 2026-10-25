import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from mcp.types import Tool, CallToolResult, TextContent
from agentic_ai import mcp_gateway as gateway
from agentic_ai.member_scope import scoped_tools, member_call, filter_member_rows
from agentic_ai.report_presentation import present_report

NAME = 'loan-due-report'
ARGS = {'branchCode': '0007', 'reportDate': '2026-09-30', 'memberId': '0000000042'}
QUERY = 'Show outstanding principal for member 0000000042 in branch 0007 as of 2026-09-30.'
SCHEMA = {'type': 'object', 'properties': {'branchCode': {'type': 'string'},
          'reportDate': {'type': 'string'}}, 'additionalProperties': False}
SCOPE = {'member_id': '0000000042', 'branch_code': '0007', 'report_date': '2026-09-30',
         'mode': 'application_report_filter'}
ROWS = [{'MEMBERID': '0000000042', 'BRANCHCODE': '0007', 'OUTSTANDINGP': '125.50', 'PHONENO': 'PRIVATE'},
        {'MEMBERID': '00000000420', 'BRANCHCODE': '0007', 'OUTSTANDINGP': '99999.00'}]


class MemberScopeTests(SimpleTestCase):
    def setUp(self):
        self.tool = Tool(name=NAME, inputSchema=SCHEMA)

    def test_virtual_schema_preserves_catalog_and_requires_date_branch_for_member(self):
        prepared = scoped_tools([self.tool], {NAME})[0]
        self.assertNotIn('memberId', self.tool.inputSchema['properties'])
        gateway._validator(prepared.inputSchema).validate(ARGS)
        from jsonschema import ValidationError
        with self.assertRaises(ValidationError):
            gateway._validator(prepared.inputSchema).validate({'memberId': '0000000042'})
        self.assertIs(scoped_tools([self.tool], set())[0], self.tool)

    def test_local_member_argument_is_never_sent_to_legacy_nifi(self):
        outgoing, scope = member_call(NAME, ARGS, [self.tool], {NAME}, QUERY)
        self.assertEqual(outgoing, {'branchCode': '0007', 'reportDate': '2026-09-30'})
        self.assertEqual(scope, SCOPE)
        self.assertIn('memberId', ARGS)

    def test_native_parameter_is_preserved_but_result_scope_is_still_checked(self):
        schema = {**SCHEMA, 'properties': {**SCHEMA['properties'], 'memberId': {'type': 'string'}}}
        tool = Tool(name=NAME, inputSchema=schema)
        outgoing, scope = member_call(NAME, ARGS, [tool], {NAME}, QUERY)
        self.assertEqual(outgoing, ARGS)
        self.assertEqual(scope['mode'], 'backend_member_filter')
        with self.assertRaises(ValueError):
            filter_member_rows(ROWS, scope)

    def test_member_must_be_supplied_exactly_and_dates_must_be_valid(self):
        for arguments, question in [(ARGS, QUERY.replace('branch 0007', 'branch unspecified')),
                                    (ARGS, QUERY.replace('2026-09-30', 'an unspecified date')),
                                    (ARGS, QUERY.replace('0000000042', '42')),
                                    ({**ARGS, 'memberId': 42}, QUERY),
                                    ({**ARGS, 'reportDate': '2026-02-30'}, QUERY),
                                    ({**ARGS, 'branchCode': None}, QUERY)]:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                member_call(NAME, arguments, [self.tool], {NAME}, question)

    def test_exact_filter_preserves_zeroes_and_rejects_wrong_branch_or_missing_ids(self):
        self.assertEqual(filter_member_rows(ROWS, SCOPE), ROWS[:1])
        self.assertEqual(filter_member_rows([{'MEMBERID': '42', 'BRANCHCODE': '0007'}], SCOPE), [])
        for rows in ([{'MEMBERID': ARGS['memberId'], 'BRANCHCODE': '9999'}],
                     [{'OUTSTANDINGP': 3}], [{'MEMBERID': 42}]):
            with self.assertRaises(ValueError):
                filter_member_rows(rows, SCOPE)

    def test_presentation_sees_only_member_rows_and_never_sends_balances_to_model(self):
        config = {'provider': 'ollama', 'model': 'selected-model', 'member_scope': SCOPE,
                  'summarize_reports': False}
        model = AsyncMock(return_value={'message': {'content': json.dumps({
            'columns': ['OUTSTANDINGP'], 'clarification': ''})}})
        result = async_to_sync(present_report)(config, QUERY, {'structuredContent': {'rows': ROWS}}, model)
        self.assertIn('125.50', result['answer'])
        self.assertIn('0000000042', result['answer'])
        self.assertNotIn('99999', str(result))
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual(result['raw']['structuredContent']['row_count'], 1)
        model.assert_not_awaited()
        self.assertEqual(result['raw']['structuredContent']['field_selection'], 'explicit_contract')

    def test_no_match_never_claims_zero_balance_and_skips_inference(self):
        model = AsyncMock()
        result = async_to_sync(present_report)({'member_scope': SCOPE}, QUERY,
            {'structuredContent': {'rows': ROWS[1:]}}, model)
        self.assertIn('does not mean', result['answer'])
        self.assertEqual(result['raw']['structuredContent']['rows'], [])
        model.assert_not_awaited()

    def test_real_gateway_path_preserves_approval_and_strips_only_local_scope(self):
        @asynccontextmanager
        async def transport(*args, **kwargs): yield None, None, None
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(tools=[self.tool], nextCursor=None)
        session.call_tool.return_value = CallToolResult(content=[TextContent(type='text', text=json.dumps({'rows': ROWS}))])
        config = {'provider': 'ollama', 'model': 'selected-model', 'url': 'http://gateway.example/mcp',
                  'token': '', 'timeout': 1, 'summarize_reports': False}
        model = AsyncMock(return_value={'message': {'content': json.dumps({'columns': ['OUTSTANDINGP'], 'clarification': ''})}})
        for blocked in (None, {'ok': False, 'approval_required': True}):
            session.call_tool.reset_mock()
            with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
                 patch('mcp.ClientSession', return_value=session), \
                 patch.object(gateway, '_select_tool', new=AsyncMock(return_value=({}, (NAME, ARGS)))), \
                 patch.object(gateway, '_decision', new=lambda *args: blocked), \
                 patch.object(gateway, 'get_setting', return_value=NAME), \
                 patch.object(gateway, '_model_response', new=model):
                result = async_to_sync(gateway._run)(config, None, QUERY, {NAME})
            if blocked:
                session.call_tool.assert_not_awaited()
            else:
                session.call_tool.assert_awaited_once_with(NAME, arguments={k: v for k, v in ARGS.items() if k != 'memberId'})
                self.assertEqual(result['plan']['params']['memberId'], ARGS['memberId'])
                self.assertNotIn('99999', str(result))

    def test_actual_selection_protocol_accepts_member_questions(self):
        questions = [
            'What is outstanding principal for member 0000000042 in branch 0007 as of 2026-09-30?',
            'Show overdue amounts and days for member 0000000042, branch 0007, as of 2026-09-30.',
            'Show installment amount and frequency for member 0000000042 in branch 0007 as of 2026-09-30.',
            'Show loan products and maturity dates for member 0000000042 in branch 0007 as of 2026-09-30.',
            'Show savings balances recorded in the report for member 0000000042 in branch 0007 as of 2026-09-30.',
        ]
        for question in questions:
            model = AsyncMock(return_value={'message': {'tool_calls': [{'function': {
                'name': 'mcp_tool_0', 'arguments': ARGS}}]}})
            with self.subTest(question=question), patch.object(gateway, '_model_response', new=model):
                _, selected = async_to_sync(gateway._select_tool)(
                    {'provider': 'ollama', 'model': 'selected-model'}, question, scoped_tools([self.tool], {NAME}))
                self.assertEqual(selected, (NAME, ARGS))
                parameters = model.call_args.args[1]['tools'][0]['function']['parameters']
                self.assertIn('memberId', parameters['properties'])
