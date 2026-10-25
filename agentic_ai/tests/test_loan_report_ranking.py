from copy import deepcopy
from contextlib import asynccontextmanager
from decimal import localcontext
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from jsonschema import ValidationError
from mcp.types import Tool, CallToolResult, TextContent
from agentic_ai import mcp_gateway as gateway
from agentic_ai.loan_report_contract import (
    INPUT_SCHEMA, RANK_OPERATION, prepare_tools, ranking_candidates,
    supports_ranking, validate_ranking_call)
from agentic_ai.zone_ranking import present_ranking, validate_request

NAME = 'loan-due-report'
ARGS = {'operation': RANK_OPERATION, 'zoneName': 'Example Zone',
        'reportDate': '2026-09-30', 'limit': 10}
QUERY = 'Show the top 10 overdue members in zone Example Zone as of 2026-09-30.'
CANONICAL = {key: value for key, value in ARGS.items() if key != 'operation'}


def loan(identifier, member, due, principal='100.00', branch='0007', days=30):
    return {'LOANID': identifier, 'MEMBERID': member, 'BRANCHCODE': branch,
            'BRANCHNAME': 'Synthetic ' + branch, 'ZONENAME': 'Example Zone',
            'DUEAMOUNT': due, 'OUTSTANDINGP': principal, 'DUE_DAYS': days,
            'PHONENO': 'PRIVATE', 'MEMBERNAME': 'PRIVATE'}


def snapshot(rows=None):
    rows = rows if rows is not None else [
        loan('L1', '000042', '60.10', days=20),
        loan('L2', '000043', '100.20', days=40),
        loan('L3', '000042', '40.20', days=72),
        loan('L4', '000044', '0.00'),
        loan('L5', '000042', '0.00', principal='50.50', days=0)]
    return {'status': 'ok', 'resultType': 'complete_loan_report', 'zoneCode': '07',
            'zoneName': 'Example Zone', 'reportDate': '2026-09-30', 'limit': 10,
            'scopeComplete': True, 'authorizationApplied': True, 'currency': 'BDT',
            'rowGrain': 'loan', 'loanKeyColumns': ['LOANID'],
            'sourceRowCount': len(rows), 'rows': rows}


class LoanRankingContractTests(SimpleTestCase):
    def setUp(self):
        self.tool = Tool(name=NAME, inputSchema=deepcopy(INPUT_SCHEMA))

    def test_existing_report_inputs_remain_valid_and_ranking_has_one_zone(self):
        validator = gateway._validator(INPUT_SCHEMA)
        for arguments in ({}, {'branchCode': '', 'reportDate': ''}, {'branchCode': '0007'}, {'branchCode': '0007', 'reportDate': '2026-09-30'}, ARGS):
            validator.validate(arguments)
        for arguments in ({**ARGS, 'branchCode': '0007'}, {**ARGS, 'zoneCode': '07'},
                          {**ARGS, 'limit': 101}, {'zoneName': 'Example Zone'},
                          {**ARGS, 'memberId': '000042'}, {**ARGS, 'reportDate': '2026-02-30'}):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                validator.validate(arguments)

    def test_legacy_tool_does_not_get_virtual_ranking_parameters(self):
        old = Tool(name=NAME, inputSchema={'type': 'object', 'properties': {
            'branchCode': {'type': 'string'}, 'reportDate': {'type': 'string'}}})
        self.assertFalse(supports_ranking(old, {NAME}))
        self.assertIs(prepare_tools([old], {NAME})[0], old)
        self.assertEqual(ranking_candidates([old], {NAME}), [])
        self.assertNotIn('operation', old.inputSchema['properties'])

    def test_same_tool_is_preferred_to_legacy_separate_tool_without_mutation(self):
        legacy = Tool(name='zone-overdue-ranking', inputSchema={})
        before = deepcopy(self.tool.inputSchema)
        prepared = prepare_tools([self.tool], {NAME})[0]
        self.assertIn(RANK_OPERATION, prepared.description)
        self.assertEqual(self.tool.inputSchema, before)
        self.assertEqual(ranking_candidates([legacy, self.tool], {NAME}), [self.tool])
        self.assertEqual(validate_ranking_call(NAME, ARGS, [self.tool], {NAME}, QUERY), CANONICAL)
        self.assertEqual(ARGS['operation'], RANK_OPERATION)
        self.assertFalse(supports_ranking(self.tool, set()))

    def test_zone_cannot_be_inferred_from_a_date_or_unrelated_number(self):
        with self.assertRaises(ValueError):
            validate_request({'zoneCode': '2026', 'reportDate': '2026-09-30', 'limit': 10}, QUERY)
        validate_request({'zoneName': 'Example Zone', 'reportDate': '2026-09-30', 'limit': 10}, QUERY)

    def test_other_metrics_and_extra_branch_filters_are_rejected(self):
        for question in (
            'Show bottom 10 overdue members in zone Example Zone as of 2026-09-30.',
            'Show top 10 members by overdue days in zone Example Zone as of 2026-09-30.',
            'Show top 10 members by outstanding principal in zone Example Zone as of 2026-09-30.',
            'Show top 10 overdue members in zone Example Zone, branch 0007 as of 2026-09-30.'):
            with self.subTest(question=question), self.assertRaises(ValueError):
                validate_request(CANONICAL, question)

    def test_one_existing_mcp_tool_executes_both_supported_ranking_responses(self):
        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield None, None, None

        ranked = {'status': 'ok', 'zoneCode': '07', 'zoneName': 'Example Zone',
                  'reportDate': ARGS['reportDate'], 'limit': 10, 'ranking': 'member_due_desc',
                  'aggregation': 'branch_member', 'scopeComplete': True, 'authorizationApplied': True,
                  'eligibleMemberCount': 1, 'rows': [{**loan('L1', '000042', '125.50'),
                                                    'RANK': 1, 'ZONECODE': '07'}]}
        for data in (snapshot(), ranked):
            session = AsyncMock()
            session.__aenter__.return_value = session
            session.list_tools.return_value = SimpleNamespace(tools=[self.tool], nextCursor=None)
            session.call_tool.return_value = CallToolResult(content=[TextContent(type='text', text=json.dumps(data))])
            with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
                 patch('mcp.ClientSession', return_value=session), \
                 patch.object(gateway, '_select_tool', new=AsyncMock(return_value=({}, (NAME, ARGS)))) as select, \
                 patch.object(gateway, '_decision', new=lambda *args: None), \
                 patch.object(gateway, 'get_setting', return_value=NAME):
                result = async_to_sync(gateway._run)({'url': 'http://gateway.example/mcp',
                    'token': '', 'timeout': 1, 'summarize_reports': False}, None, QUERY, {NAME})
            session.call_tool.assert_awaited_once_with(NAME, arguments=ARGS)
            self.assertEqual([tool.name for tool in select.call_args.args[2]], [NAME])
            self.assertEqual(result['tool'], NAME)
            self.assertTrue(result['tool_executed'])
            self.assertEqual(result['raw']['structuredContent']['rows'][0]['MEMBERID'], '000042')
            self.assertNotIn('PRIVATE', str(result))

    def test_ranking_backend_error_never_returns_an_unfiltered_report(self):
        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield None, None, None

        session = AsyncMock()
        session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(tools=[self.tool], nextCursor=None)
        session.call_tool.return_value = CallToolResult(isError=True,
            content=[TextContent(type='text', text='PRIVATE')])
        with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
             patch('mcp.ClientSession', return_value=session), \
             patch.object(gateway, '_select_tool', new=AsyncMock(return_value=({}, (NAME, ARGS)))), \
             patch.object(gateway, '_decision', new=lambda *args: None), \
             patch.object(gateway, 'get_setting', return_value=NAME), \
             self.assertRaises(gateway.GatewayError) as caught:
            async_to_sync(gateway._run)({'url': 'http://gateway.example/mcp', 'token': '', 'timeout': 1}, None, QUERY, {NAME})
        self.assertEqual(caught.exception.code, 'ranking_backend_error')
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_ranking_keeps_existing_approval_barrier(self):
        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield None, None, None

        session = AsyncMock()
        session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(tools=[self.tool], nextCursor=None)
        with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
             patch('mcp.ClientSession', return_value=session), \
             patch.object(gateway, '_select_tool', new=AsyncMock(return_value=({}, (NAME, ARGS)))), \
             patch.object(gateway, '_decision', new=lambda *args: {'ok': False, 'approval_required': True}), \
             patch.object(gateway, 'get_setting', return_value=NAME):
            result = async_to_sync(gateway._run)({'url': 'http://gateway.example/mcp', 'token': '', 'timeout': 1}, None, QUERY, {NAME})
        self.assertTrue(result['approval_required'])
        session.call_tool.assert_not_awaited()


class CompleteReportRankingTests(SimpleTestCase):
    def render(self, data):
        return present_ranking({'structuredContent': data}, CANONICAL)['raw']['structuredContent']

    def test_exact_totals_group_all_loans_before_ranking_and_exclude_zero_due_members(self):
        result = self.render(snapshot())
        self.assertEqual(result['row_count'], 2)
        self.assertEqual(result['sourceRowCount'], 5)
        self.assertEqual(result['calculation_mode'], 'application_complete_report')
        first = result['rows'][0]
        self.assertEqual(first['MEMBERID'], '000042')
        self.assertEqual(first['DUEAMOUNT'], '100.30')
        self.assertEqual(first['OUTSTANDINGP'], '250.50')
        self.assertEqual(first['DUE_DAYS'], 72)
        self.assertNotIn('PRIVATE', str(result))
        self.assertNotIn('LOANID', first)

    def test_top_ten_are_selected_from_the_entire_source_not_the_first_hundred(self):
        rows = [loan(f'L{i}', f'{i:06}', f'{i}.01') for i in range(150)]
        result = self.render(snapshot(rows))
        self.assertEqual(len(result['rows']), 10)
        self.assertEqual(result['rows'][0]['MEMBERID'], '000149')
        self.assertEqual(result['rows'][-1]['MEMBERID'], '000140')
        self.assertEqual(result['row_count'], 150)

    def test_composite_loan_key_can_use_existing_report_columns(self):
        data = snapshot([loan('ignored', '000042', '60.10'), loan('ignored', '000042', '40.20')])
        data['loanKeyColumns'] = ['BRANCHCODE', 'MEMBERID', 'LOANCYCLE']
        for cycle, row in enumerate(data['rows'], 1):
            row.pop('LOANID')
            row['LOANCYCLE'] = cycle
        self.assertEqual(self.render(data)['rows'][0]['DUEAMOUNT'], '100.30')

    def test_stable_ties_and_exact_decimal_arithmetic_under_low_global_precision(self):
        data = snapshot([loan('L1', '000043', '100.20'), loan('L2', '000042', '100.20'),
                         loan('L3', '000044', '123456789012345678901.12345678'),
                         loan('L4', '000044', '0.01')])
        with localcontext() as context:
            context.prec = 3
            rows = self.render(data)['rows']
        self.assertEqual(rows[0]['DUEAMOUNT'], '123456789012345678901.13345678')
        self.assertEqual([row['MEMBERID'] for row in rows[1:]], ['000042', '000043'])

    def test_empty_complete_snapshot_is_valid(self):
        result = self.render(snapshot([]))
        self.assertEqual(result['row_count'], 0)
        self.assertEqual(result['rows'], [])

    def test_missing_or_inconsistent_snapshot_metadata_never_produces_a_ranking(self):
        invalid = []
        for key, value in [('scopeComplete', False), ('authorizationApplied', False),
                           ('sourceRowCount', 100), ('zoneName', 'Other Zone'), ('zoneCode', None),
                           ('currency', None), ('rowGrain', 'unknown'), ('loanKeyColumns', []),
                           ('reportDate', '2026-09-29'), ('limit', 100)]:
            data = snapshot(); data[key] = value; invalid.append(data)
        data = snapshot(); del data['scopeComplete']; invalid.append(data)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.render(data)

    def test_bad_rows_duplicate_loans_and_mixed_currencies_fail_closed(self):
        invalid = []
        for key, value in [('ZONENAME', 'Other Zone'), ('CURRENCY', 'EUR'),
                           ('DUEAMOUNT', 1.25), ('DUEAMOUNT', 'NaN'), ('DUEAMOUNT', '-1'),
                           ('OUTSTANDINGP', None), ('MEMBERID', 42), ('DUE_DAYS', True),
                           ('LOANID', None)]:
            data = snapshot(); data['rows'][0][key] = value; invalid.append(data)
        data = snapshot(); data['rows'][1]['LOANID'] = 'L1'; invalid.append(data)
        data = snapshot(); data['rows'][1]['LOANID'] = 2; invalid.append(data)
        data = snapshot(); data['rows'][1]['BRANCHNAME'] = 'Other branch name'; invalid.append(data)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.render(data)

    def test_wrapped_rest_payload_preserves_metadata(self):
        result = present_ranking({'content': [{'type': 'text', 'text': json.dumps({'data': snapshot()})}]}, CANONICAL)
        self.assertEqual(result['raw']['structuredContent']['row_count'], 2)

    def test_adapter_errors_cannot_be_hidden_by_a_successful_nested_payload(self):
        for wrapper in ({'success': False, 'data': snapshot()},
                        {'error': 'failed', 'data': snapshot()},
                        {**snapshot(), 'isError': True}):
            with self.subTest(wrapper=wrapper), self.assertRaises(ValueError):
                self.render(wrapper)

    def test_optional_model_intro_receives_metadata_only(self):
        from agentic_ai.report_presentation import summarize_report
        presentation = present_ranking({'structuredContent': snapshot()}, CANONICAL)
        model = AsyncMock(return_value={'message': {'content': '{"summary":"Here are the requested member review priorities."}'}})
        result = async_to_sync(summarize_report)({'provider': 'ollama', 'model': 'synthetic-model'}, presentation, model)
        self.assertIn('Here are the requested member review priorities.', result['answer'])
        prompt = str(model.call_args.args[1]['messages'])
        self.assertNotIn('000042', prompt)
        self.assertNotIn('100.30', prompt)
        self.assertNotIn('250.50', prompt)
        self.assertNotIn('PRIVATE', prompt)
