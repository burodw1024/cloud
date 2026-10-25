from copy import deepcopy
from django.test import SimpleTestCase
from agentic_ai.zone_ranking import validate_request, present_ranking

ARGS = {'zoneCode': '07', 'reportDate': '2026-09-30', 'limit': 10}
QUERY = 'Show top 10 overdue members in zone 07 as of 2026-09-30.'


def envelope():
    return {'status': 'ok', 'zoneCode': '07', 'zoneName': 'Example Zone',
            'reportDate': '2026-09-30', 'limit': 10, 'ranking': 'member_due_desc',
            'aggregation': 'branch_member', 'scopeComplete': True,
            'authorizationApplied': True, 'eligibleMemberCount': 2,
            'rows': [{'RANK': i, 'ZONECODE': '07', 'ZONENAME': 'Example Zone',
                      'BRANCHCODE': '0007', 'BRANCHNAME': 'Example', 'MEMBERID': member,
                      'DUEAMOUNT': due, 'OUTSTANDINGP': '1000.00', 'DUE_DAYS': 35,
                      'PHONENO': 'private'}
                     for i, member, due in [(1, '000042', '200.00'), (2, '000043', '100.00')]]}


class ZoneRankingTests(SimpleTestCase):
    def test_request_requires_exact_scope_date_and_bounded_count(self):
        validate_request(ARGS, QUERY)
        for args, query in [({**ARGS, 'limit': 11}, QUERY), (ARGS, QUERY.replace('07', '08')),
                            (ARGS, QUERY.replace('2026-09-30', 'yesterday')),
                            ({**ARGS, 'limit': 101}, QUERY),
                            ({**ARGS, 'zoneName': 'Other'}, QUERY)]:
            with self.subTest(args=args), self.assertRaises(Exception):
                validate_request(args, query)
        validate_request({'zoneName': 'Example Zone', 'reportDate': ARGS['reportDate'], 'limit': 10},
                         'Top 10 overdue members in Example Zone as of 2026-09-30')

    def test_exact_ranked_values_preserved_and_private_fields_stripped(self):
        result = present_ranking({'structuredContent': envelope()}, ARGS)
        rows = result['raw']['structuredContent']['rows']
        self.assertEqual(rows[0]['MEMBERID'], '000042')
        self.assertEqual(rows[0]['DUEAMOUNT'], '200.00')
        self.assertNotIn('PHONENO', rows[0])

    def test_rejects_partial_scope_invalid_money_duplicate_and_wrong_order(self):
        invalid = []
        for key, value in [('scopeComplete', False), ('authorizationApplied', False),
                           ('zoneCode', '08'), ('reportDate', '2026-09-29'),
                           ('eligibleMemberCount', 20), ('scopeComplete', 1)]:
            data = envelope(); data[key] = value; invalid.append(data)
        for value in [1.5, 'NaN', '-1', True]:
            data = envelope(); data['rows'][0]['DUEAMOUNT'] = value; invalid.append(data)
        data = envelope(); data['rows'][1]['MEMBERID'] = '000042'; invalid.append(data)
        data = envelope(); data['rows'][1]['DUEAMOUNT'] = '300'; invalid.append(data)
        data = envelope(); del data['rows'][0]['DUE_DAYS']; invalid.append(data)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                present_ranking({'structuredContent': data}, ARGS)

    def test_empty_and_ambiguous_zone(self):
        data = envelope(); data.update(rows=[], eligibleMemberCount=0)
        self.assertIn('Top 0', present_ranking({'structuredContent': data}, ARGS)['answer'])
        result = present_ranking({'structuredContent': {'status': 'ambiguous_zone'}}, ARGS)
        self.assertTrue(result['clarification_required'])

    def test_gateway_dispatch_uses_ranking_validation_and_retains_approval(self):
        import json
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from asgiref.sync import async_to_sync
        from mcp.types import Tool, CallToolResult, TextContent
        from agentic_ai import mcp_gateway as gateway
        from agentic_ai.zone_ranking import TOOL_NAME, INPUT_SCHEMA, DESCRIPTION

        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield None, None, None

        session = AsyncMock()
        session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(tools=[Tool(
            name=TOOL_NAME, inputSchema=INPUT_SCHEMA, description=DESCRIPTION)], nextCursor=None)
        session.call_tool.return_value = CallToolResult(content=[TextContent(
            type='text', text=json.dumps(envelope()))])
        config = {'provider': 'ollama', 'model': 'selected-model', 'url': 'http://gateway.example/mcp',
                  'token': '', 'timeout': 1}
        for blocked in (None, {'ok': False, 'approval_required': True}):
            session.call_tool.reset_mock()
            with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
                 patch('mcp.ClientSession', return_value=session), \
                 patch.object(gateway, '_select_tool', new=AsyncMock(return_value=({}, (TOOL_NAME, ARGS)))), \
                 patch.object(gateway, '_decision', new=lambda *args: blocked), \
                 patch.object(gateway, 'get_setting', return_value='loan-due-report'):
                result = async_to_sync(gateway._run)(dict(config), None, QUERY, {TOOL_NAME})
            if blocked:
                session.call_tool.assert_not_awaited()
            else:
                session.call_tool.assert_awaited_once_with(TOOL_NAME, arguments=ARGS)
                self.assertEqual(result['raw']['structuredContent']['rows'][0]['MEMBERID'], '000042')


    def test_ranking_question_never_falls_back_to_branch_report(self):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from asgiref.sync import async_to_sync
        from mcp.types import Tool
        from agentic_ai import mcp_gateway as gateway
        from agentic_ai.zone_ranking import TOOL_NAME, INPUT_SCHEMA, DESCRIPTION
        branch = Tool(name='loan-due-report', inputSchema={
            'type': 'object', 'properties': {'branchCode': {'type': 'string'}, 'reportDate': {'type': 'string'}}})
        zone = Tool(name=TOOL_NAME, inputSchema=INPUT_SCHEMA, description=DESCRIPTION)
        question = 'Show the top 10 overdue members in zone Example Zone as of 2026-09-30'

        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield None, None, None

        for tools in ([branch], [branch, zone]):
            session = AsyncMock()
            session.__aenter__.return_value = session
            session.list_tools.return_value = SimpleNamespace(tools=tools, nextCursor=None)
            select = AsyncMock(return_value=({}, ('loan-due-report', {'reportDate': '2026-09-30'})))
            with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
                 patch('mcp.ClientSession', return_value=session), \
                 patch.object(gateway, '_select_tool', new=select), \
                 patch.object(gateway, 'get_setting', return_value='loan-due-report'):
                result = async_to_sync(gateway._run)({
                    'url': 'http://gateway.example/mcp', 'token': '', 'timeout': 1},
                    None, question, {tool.name for tool in tools})
            session.call_tool.assert_not_awaited()
            self.assertEqual(result['code'], 'zone_ranking_unavailable')
            self.assertFalse(result['tool_executed'])
            if len(tools) == 1:
                select.assert_not_awaited()
            else:
                self.assertEqual([tool.name for tool in select.call_args.args[2]], [TOOL_NAME])

    def test_projection_never_exposes_ordinary_rows_for_a_ranking_question(self):
        from asgiref.sync import async_to_sync
        from unittest.mock import AsyncMock
        from agentic_ai.report_presentation import present_report
        model = AsyncMock()
        question = 'Show the top 10 overdue members in zone Example Zone as of 2026-09-30'
        result = async_to_sync(present_report)({}, question, {'structuredContent': {'rows': [
            {'BRANCHNAME': 'Synthetic branch', 'DUE_DAYS': 80, 'DUEAMOUNT': '7.50'}]}}, model)
        self.assertTrue(result['clarification_required'])
        self.assertEqual(result['raw']['structuredContent']['rows'], [])
        model.assert_not_awaited()

    def test_ranking_intent_does_not_block_ordinary_member_listing_or_projection(self):
        from agentic_ai.zone_ranking import is_ranking_request, is_zone_due_ranking
        self.assertTrue(is_zone_due_ranking('Show top 10 overdue members in zone Example Zone'))
        self.assertTrue(is_zone_due_ranking('Rank members with highest arrears in zone 07'))
        self.assertFalse(is_ranking_request('List the first 10 member IDs in ascending order'))
        self.assertFalse(is_ranking_request('Show overdue amount and zone name for member 42'))
        self.assertFalse(is_zone_due_ranking('Show top 10 overdue members in branch 0007'))
        with self.assertRaises(ValueError):
            validate_request(ARGS, 'Show top 10 overdue branches in zone 07 as of 2026-09-30')
