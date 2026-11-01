import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from mcp.types import Tool

from agentic_ai import mcp_gateway as gateway
from agentic_ai.loan_question_help import is_loan_question_help, loan_question_help
from agentic_ai.loan_report_contract import INPUT_SCHEMA


NAME = 'loan-due-report'
NATIVE = json.loads((Path(__file__).resolve().parents[2] /
                     'deploy/contracts/loan-due-query.input.schema.json').read_text())


def tool(schema, name=NAME):
    return Tool(name=name, inputSchema=schema)


class LoanQuestionHelpTests(SimpleTestCase):
    def test_english_bangla_and_common_typo_requests(self):
        for query in ('Give all possible questions I can ask about loans',
                      'What questions can I ask?', 'list loan question examples',
                      'give all posble queestion can ask on loan',
                      'ঋণ নিয়ে কী কী প্রশ্ন করতে পারি?', 'সব প্রশ্নের উদাহরণ দেখাও',
                      'ঋণ রিপোর্টের প্রশ্নের তালিকা দাও'):
            with self.subTest(query=query):
                self.assertTrue(is_loan_question_help(query))

    def test_financial_queries_are_not_intercepted(self):
        for query in ('Show all loans in branch 0253 as of 2026-09-30',
                      '0253 শাখার সব বকেয়া ঋণ দেখাও',
                      'সর্বোচ্চ বকেয়া থাকা ১০ জন সদস্য দেখাও',
                      'What is the overdue amount for member 000123?'):
            self.assertFalse(is_loan_question_help(query))

    def test_legacy_only_does_not_advertise_native_filters_or_ranking(self):
        schema = {'type': 'object', 'properties': {
            'branchCode': {'type': 'string'}, 'reportDate': {'type': 'string'}}}
        result = loan_question_help([tool(schema)], {NAME})
        self.assertIn('000123', result['answer'])
        self.assertNotIn('EMP001', result['answer'])
        self.assertNotIn('সর্বোচ্চ বকেয়া', result['answer'])
        self.assertEqual(result['help_tools'], [NAME])

    def test_native_examples_follow_declared_filters_levels_and_metrics(self):
        result = loan_question_help([tool(NATIVE)], {NAME})
        questions = [q for group in result['question_groups'] for q in group['questions']]
        self.assertGreater(len(questions), 75)
        for text in ('EMP001', '000123', '2025-01-01', 'কমপক্ষে 90 দিন', 'ডিভিশন'):
            self.assertIn(text, result['answer'])
        schema = json.loads(json.dumps(NATIVE))
        for field in ('employee_code', 'member_id', 'disbursed_from', 'min_days'):
            schema['properties'].pop(field)
        schema['properties']['level']['enum'] = ['LOAN', 'MEMBER', 'BRANCH', 'ZONE']
        schema['properties']['sort_by']['enum'] = ['DUEAMOUNT']
        reduced = loan_question_help([tool(schema)], {NAME})['answer']
        for text in ('EMP001', '000123', '2025-01-01', 'কমপক্ষে 90 দিন', 'ডিভিশন', 'অবশিষ্ট আসল'):
            self.assertNotIn(text, reduced)

    def test_operation_ranking_only_when_declared(self):
        result = loan_question_help([tool(INPUT_SCHEMA)], {NAME})
        self.assertIn('জোনে সদস্যের বকেয়া র‍্যাঙ্কিং', result['answer'])
        self.assertNotIn('কর্মীভিত্তিক', result['answer'])

    def test_unknown_or_missing_loan_tools_have_no_suggestions(self):
        for tools in ([], [tool(NATIVE, 'unrelated-tool')], [tool({'type': 'object'})]):
            result = loan_question_help(tools, {NAME})
            self.assertEqual(result['question_groups'], [])
            self.assertEqual(result['help_tools'], [])

    def test_examples_use_assigned_codes_without_changing_the_inserted_branch_again(self):
        result = loan_question_help([tool(NATIVE)], {NAME}, {'example_branch': '07', 'example_zone': '0253'})
        self.assertIn('07 শাখায়', result['answer'])
        self.assertIn('0253 জোনের', result['answer'])

    def test_gateway_help_discovers_authorized_tools_without_model_or_dispatch(self):
        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield None, None, None

        session = AsyncMock()
        session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(
            tools=[tool(NATIVE), tool(INPUT_SCHEMA, 'restricted-loan')], nextCursor=None)
        config = {'url': 'https://gateway.example/mcp', 'token': '', 'timeout': 1}
        with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
             patch('mcp.ClientSession', return_value=session), \
             patch.object(gateway, 'get_setting', side_effect=lambda key, default='': default), \
             patch.object(gateway, '_select_tool', new_callable=AsyncMock) as select:
            result = async_to_sync(gateway._run)(config, None,
                       'ঋণ নিয়ে কী কী প্রশ্ন করতে পারি?', {NAME})
        self.assertEqual(result['kind'], 'loan_question_help')
        self.assertEqual(result['help_tools'], [NAME])
        select.assert_not_awaited()
        session.call_tool.assert_not_awaited()
        self.assertFalse(result['tool_executed'])

    def test_guide_cannot_be_replayed_with_stale_capabilities(self):
        from agentic_ai.api import _can_replay_result
        self.assertFalse(_can_replay_result(None, {'kind': 'loan_question_help'}))
