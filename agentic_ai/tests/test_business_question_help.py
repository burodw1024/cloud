from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User, Group
from django.test import TestCase, SimpleTestCase
from mcp.types import Tool

from agentic_ai import mcp_gateway as gateway
from agentic_ai.api import _can_replay_result
from agentic_ai.business_question_help import savings_question_help, question_domain, business_question_help
from agentic_ai.models import AgentTool, BusinessModule

NAME = 'savings-report'
SCHEMA = {'type': 'object', 'properties': {
    'operation': {'type': 'string', 'enum': ['period-summary', 'branch-summary', 'zone-summary',
        'zone-branches', 'branch-product-summary', 'zone-product-summary']},
    **{key: {'type': 'string'} for key in ('branch_code', 'zone_code', 'from_date', 'to_date_exclusive')}},
    'required': ['operation', 'from_date', 'to_date_exclusive']}


def tool(schema=None, name=NAME):
    return Tool(name=name, description='Savings deposits and withdrawals', inputSchema=schema or SCHEMA)


class SavingsGuideSchemaTests(SimpleTestCase):
    def test_dates_use_previous_complete_month_and_only_supported_operations(self):
        groups, sources = savings_question_help([tool()], date(2026, 11, 4))
        self.assertEqual(len(groups[0]['questions']), 6)
        text = str(groups)
        for expected in ('2026-10-01', '2026-11-01', 'শেষ তারিখ বাদ', '[অনুমোদিত শাখা কোড]'):
            self.assertIn(expected, text)
        self.assertEqual(sources, [NAME])
        schema = deepcopy(SCHEMA)
        schema['properties']['operation']['enum'] = ['period-summary']
        groups, _ = savings_question_help([tool(schema)])
        self.assertEqual(len(groups[0]['questions']), 1)
        schema['properties'].pop('branch_code')
        self.assertEqual(savings_question_help([tool(schema)])[0], [])

    def test_previous_month_handles_year_boundary(self):
        text = str(savings_question_help([tool()], date(2026, 1, 1)))
        self.assertIn('2025-12-01', text)
        self.assertIn('2026-01-01', text)

    def test_explicit_savings_is_not_routed_to_loan_help(self):
        for query in ('সঞ্চয় নিয়ে কী কী প্রশ্ন করতে পারি?', 'What questions can I ask about savings?'):
            self.assertEqual(question_domain(query), 'savings')
        self.assertEqual(question_domain('ঋণ নিয়ে কী কী প্রশ্ন করতে পারি?'), 'loans')
        self.assertEqual(question_domain('ঋণ ও সঞ্চয় প্রশ্নের উদাহরণ'), 'business')


class BusinessGuideAccessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('savings-user')
        self.group = Group.objects.create(name='savings_users'); self.user.groups.add(self.group)
        self.module = BusinessModule.objects.create(name='Savings reports', slug='savings-reports',
            starter_questions='Approved savings example')
        self.record = AgentTool.objects.create(name=NAME, allowed_roles=['savings_users'],
            description='Savings deposits and withdrawals', module=self.module)

    def guide(self, query='সঞ্চয় নিয়ে কী কী প্রশ্ন করতে পারি?'):
        return business_question_help(self.user, query, [tool()], {'loan-due-report'})

    def test_savings_guide_uses_only_savings_questions(self):
        result = self.guide()
        self.assertEqual(result['help_tools'], [NAME])
        self.assertEqual(len(result['question_groups'][0]['questions']), 6)
        self.assertFalse(result['tool_executed'])
        self.assertFalse(_can_replay_result(self.user, result))
        self.assertNotIn('loan-due-report', result['answer'])

    def test_paused_module_and_removed_group_hide_examples(self):
        self.module.is_active = False; self.module.save()
        self.assertEqual(self.guide()['question_groups'], [])
        self.module.is_active = True; self.module.save()
        self.user.groups.clear()
        self.assertEqual(self.guide()['help_tools'], [])

    def test_generic_module_guide_includes_only_authorized_published_starters(self):
        hidden = BusinessModule.objects.create(name='Secret payroll', slug='payroll', starter_questions='PRIVATE EXAMPLE')
        AgentTool.objects.create(name='payroll-tool', allowed_roles=['payroll'], module=hidden)
        result = self.guide('ব্যবসার কাজে কী কী প্রশ্ন করতে পারি?')
        self.assertIn('Approved savings example', result['answer'])
        self.assertNotIn('PRIVATE EXAMPLE', result['answer'])
        self.assertNotIn('Secret payroll', result['answer'])

    def test_custom_module_guide_does_not_show_unrelated_savings_templates(self):
        module = BusinessModule.objects.create(name='HR services', slug='hr', starter_questions='ছুটির আবেদন কীভাবে করব?')
        AgentTool.objects.create(name='hr-help', allowed_roles=['savings_users'], module=module)
        result = business_question_help(self.user, 'HR services মডিউলের প্রশ্নের উদাহরণ দেখাও।',
            [tool(), Tool(name='hr-help', inputSchema={'type': 'object'})], {'loan-due-report'})
        self.assertIn('ছুটির আবেদন', result['answer'])
        self.assertNotIn('সঞ্চয় জমা', result['answer'])
        self.assertEqual(result['help_tools'], ['hr-help'])

    def test_gateway_discovers_but_does_not_execute_or_call_model_for_savings_help(self):
        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield None, None, None
        session = AsyncMock(); session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(tools=[tool()], nextCursor=None)
        with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
             patch('mcp.ClientSession', return_value=session), \
             patch.object(gateway, '_select_tool', new_callable=AsyncMock) as model:
            result = async_to_sync(gateway._run)({'url': 'http://example/mcp', 'timeout': 1, 'token': ''}, self.user,
                'সঞ্চয় নিয়ে কী কী প্রশ্ন করতে পারি?', {NAME})
        self.assertEqual(result['kind'], 'business_question_help')
        self.assertEqual(result['help_tools'], [NAME])
        model.assert_not_awaited(); session.call_tool.assert_not_awaited()

    @patch('agentic_ai.workspace.gateway_enabled', return_value=True)
    def test_savings_workspace_has_savings_button_without_loan_button(self, enabled):
        self.client.force_login(self.user)
        response = self.client.get('/agentic/?module=savings-reports')
        self.assertContains(response, 'id="savings-question-help"')
        self.assertNotContains(response, 'id="loan-question-help"')
        self.assertContains(response, 'Approved savings example')
