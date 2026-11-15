from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.test import TestCase
from mcp.types import Tool, CallToolResult

from agentic_ai import mcp_gateway as gateway
from agentic_ai.application_scope import grant, tool_supported, validate_response
from agentic_ai.loan_access import LoanAccessDenied, snapshot
from agentic_ai.models import AgentTool, LoanAccessPolicy, LoanBranch, LoanZone, UserToolAccess
from .test_setup import GuidedSetupTests


SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'operation': {'type': 'string', 'enum': ['period-summary', 'branch-summary', 'zone-summary',
        'zone-branches', 'branch-product-summary', 'zone-product-summary']},
    'branch_code': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,100}$'},
    'zone_code': {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$'},
    'from_date': {'type': 'string', 'format': 'date'},
    'to_date_exclusive': {'type': 'string', 'format': 'date'}},
    'required': ['operation', 'from_date', 'to_date_exclusive'], 'allOf': [
        {'if': {'properties': {'operation': {'const': 'period-summary'}}, 'required': ['operation']},
         'then': {'required': ['branch_code'], 'not': {'required': ['zone_code']}}},
        {'if': {'properties': {'operation': {'const': 'zone-branches'}}, 'required': ['operation']},
         'then': {'required': ['zone_code']}}]}
TOOL = Tool(name='savings-report', description='Savings deposits and withdrawals', inputSchema=SCHEMA)
ARGS = {'operation': 'branch-summary', 'from_date': '2026-10-01', 'to_date_exclusive': '2026-11-01'}


def response(query):
    return {'success': True, 'scope': {
        **{key: query[key] for key in ('branch_code', 'from_date', 'to_date_exclusive')},
        'zone_code': query.get('zone_code'), 'authorized_branch_count': 1},
        'data': [{'branch_code': query['branch_code'], 'deposits': '123.45'}]}


class SavingsSetupTests(TestCase):
    setUp = GuidedSetupTests.setUp
    post = GuidedSetupTests.post

    @patch('agentic_ai.setup_views._save_settings')
    def test_live_schema_can_save_user_access_modes(self, save):
        with patch('agentic_ai.setup_views.discover_gateway', new=AsyncMock(return_value=[TOOL])):
            for mode in ('UI', 'APP'):
                self.assertTrue(self.post('discover').json()['tools'][0]['application_scope_supported'])
                self.post('check_model')
                result = self.post('save_tools', tools=[dict(name=TOOL.name, enabled=True,
                    roles=['manager'], requires_approval=False, requires_loan_scope=False,
                    loan_access_mode=mode, fixed_branch_codes=[])])
                self.assertEqual(result.status_code, 200, result.content)
                record = AgentTool.objects.get(name=TOOL.name)
                self.assertEqual(record.loan_access_mode, mode)
                self.assertTrue(record.requires_loan_scope)


class SavingsScopeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('savings-user')
        self.zone = LoanZone.objects.create(code='07', name='Zone')
        self.branch = LoanBranch.objects.create(code='0253', name='Branch', zone=self.zone)
        self.policy = LoanAccessPolicy.objects.create(user=self.user, mode='SELECTED')
        self.policy.branches.add(self.branch)
        self.record = AgentTool.objects.create(name=TOOL.name, loan_access_mode='UI', allowed_roles=['finance'])
        UserToolAccess.objects.create(user=self.user).tools.add(self.record)

    def grant(self, args=None):
        return grant(snapshot(self.user), TOOL.name, ARGS if args is None else args, TOOL)

    def test_single_permitted_branch_is_injected_and_dates_are_preserved(self):
        access = self.grant()
        self.assertEqual(access.query, {**ARGS, 'branch_code': '0253'})
        self.assertEqual(access.report_kind, 'savings')
        validate_response({'structuredContent': response(access.query)}, access)

    def test_multiple_branches_require_an_explicit_permitted_branch(self):
        other = LoanBranch.objects.create(code='0254', name='Other', zone=self.zone)
        self.policy.branches.add(other)
        for args in (ARGS, {**ARGS, 'zone_code': '07'}, {**ARGS, 'branch_code': '9999'}):
            with self.subTest(args=args), self.assertRaises(LoanAccessDenied):
                self.grant(args)
        self.assertEqual(self.grant({**ARGS, 'branch_code': '0254'}).branches, ['0254'])

    def test_invalid_dates_operations_and_zone_constraints_block_dispatch(self):
        for changes in ({'to_date_exclusive': '2026-09-01'}, {'from_date': '2025-01-01'},
                        {'from_date': '2026-02-30'}, {'operation': 'unknown'},
                        {'operation': 'zone-branches'},
                        {'operation': 'period-summary', 'branch_code': '0253', 'zone_code': '07'}):
            with self.subTest(changes=changes), self.assertRaises(LoanAccessDenied):
                self.grant({**ARGS, **changes})

    def test_response_must_confirm_scope_and_dates_and_exclude_other_branches(self):
        access = self.grant()
        for change in ('missing', 'count', 'branch', 'dates', 'row'):
            data = response(access.query)
            if change == 'missing': data.pop('scope')
            if change == 'count': data['scope']['authorized_branch_count'] = 2
            if change == 'branch': data['scope']['branch_code'] = '9999'
            if change == 'dates': data['scope']['from_date'] = '2026-09-01'
            if change == 'row': data['data'][0]['branch_code'] = '9999'
            with self.subTest(change=change), self.assertRaises(LoanAccessDenied):
                validate_response({'structuredContent': data}, access)

    def test_schema_without_branch_filter_is_not_supported(self):
        schema = deepcopy(SCHEMA)
        schema['properties'].pop('branch_code')
        self.assertFalse(tool_supported(TOOL.model_copy(update={'inputSchema': schema})))

    def run_gateway(self, revoke=False, bad_scope=False):
        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield None, None, None
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(tools=[TOOL], nextCursor=None)
        async def call(name, arguments):
            if revoke:
                await sync_to_async(LoanAccessPolicy.objects.filter(pk=self.policy.pk).update)(mode='NONE')
            data = response(arguments)
            if bad_scope: data.pop('scope')
            return CallToolResult(content=[], structuredContent=data)
        session.call_tool.side_effect = call
        config = {'url': 'https://gateway.example/mcp', 'token': '', 'timeout': 2, 'request_timeout': 10}
        with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
             patch('mcp.ClientSession', return_value=session), \
             patch.object(gateway, 'load_config', return_value=config), \
             patch.object(gateway, '_select_tool', new=AsyncMock(return_value=({}, (TOOL.name, ARGS)))):
            result = gateway.run_mcp_query(self.user, 'Show savings deposits and withdrawals for last month.')
        return result, session

    def test_gateway_dispatches_once_with_scope_without_loan_fields(self):
        result, session = self.run_gateway()
        self.assertTrue(result['ok'], result)
        session.call_tool.assert_awaited_once_with(TOOL.name, arguments={**ARGS, 'branch_code': '0253'})
        self.assertEqual(result['loan_access']['branches'], ['0253'])

    def test_gateway_blocks_unverified_response_and_revocation(self):
        for options in ({'bad_scope': True}, {'revoke': True}):
            result, _ = self.run_gateway(**options)
            self.assertFalse(result['ok'], result)
            self.assertEqual(result['code'], 'loan_scope_unverified')
            self.assertNotIn('raw', result)
