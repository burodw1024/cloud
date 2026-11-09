"""Fixed NiFi scope remains group-restricted and cannot exceed user grants."""
from contextlib import asynccontextmanager
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from django.test import TestCase
from mcp.types import Tool, CallToolResult, TextContent

from agentic_ai import mcp_gateway as gateway
from agentic_ai.api import _can_replay_result
from agentic_ai.loan_access import authorize, current_access, validate_response, FixedLoanGrant, LoanAccessDenied
from agentic_ai.models import AgentTool, LoanAccessPolicy
from . import test_setup as setup_tests
from . import test_loan_access as loan_tests
from .test_loan_access import NAME, scoped_tool
from .test_native_loan_query import SCHEMA, ARGS, QUERY, envelope


class FixedLoanScopeTests(TestCase):
    setUp = loan_tests.LoanAccessTests.setUp

    def enable_fixed(self, codes=None):
        self.record.loan_access_mode = 'FIXED'
        self.record.fixed_branch_codes = codes or ['0253']
        self.record.save()

    def grant(self, args=None):
        return authorize(self.user, NAME, ARGS if args is None else args, Tool(name=NAME, inputSchema=SCHEMA))

    def test_fixed_requires_explicit_mode_and_valid_nonempty_string_scope(self):
        with self.assertRaises(LoanAccessDenied):
            self.grant()
        for codes in ([], [253], ['0253', '0253'], [' 0253'], '0253'):
            self.record.loan_access_mode = 'FIXED'
            self.record.fixed_branch_codes = codes
            with self.assertRaises(ValidationError):
                self.record.full_clean()

    def test_no_signing_key_or_token_or_receipt_needed(self):
        self.enable_fixed()
        with patch.dict(os.environ, {'LOAN_ACCESS_SIGNING_KEY': ''}):
            grant = self.grant()
        self.assertIsInstance(grant, FixedLoanGrant)
        validate_response({'structuredContent': envelope()}, grant)
        self.assertTrue(current_access(self.user, grant.access))

    def test_entire_backend_scope_must_be_granted_even_with_narrow_request(self):
        self.enable_fixed(['0253', '0254'])
        with self.assertRaises(LoanAccessDenied):
            self.grant()
        self.policy.zones.add(self.zone)
        self.assertIsInstance(self.grant(), FixedLoanGrant)

    def test_requested_scope_and_server_authorization_cannot_be_spoofed(self):
        self.enable_fixed()
        for args in ({**ARGS, 'branch_code': '0254'}, {**ARGS, 'zone_code': '08'},
                     {**ARGS, 'loan_access_token': 'fake'}, {**ARGS, 'auth.branch_codes': '["0254"]'}):
            with self.assertRaises(LoanAccessDenied):
                self.grant(args)
        with self.assertRaises(LoanAccessDenied):
            authorize(self.user, NAME, ARGS, Tool(name=NAME, inputSchema=SCHEMA), {**ARGS, 'branch_code': '0254'})
        with self.assertRaises(LoanAccessDenied):
            authorize(self.user, NAME, ARGS, scoped_tool())

    def test_fixed_response_checks_nested_branches_and_scope_count(self):
        self.enable_fixed()
        grant = self.grant()
        for body in ({'rows': [{'BRANCHCODE': '0254'}]}, {'scope': {'authorized_branch_count': 2}}):
            with self.assertRaises(LoanAccessDenied):
                validate_response({'content': [{'type': 'text', 'text': json.dumps(body)}]}, grant)

    def test_replay_denied_after_config_or_policy_or_role_changes(self):
        self.enable_fixed()
        metadata = self.grant().access
        result = {'tool': NAME, 'plan': {'intent': 'mcp_tool_call'}, 'loan_access': metadata}
        self.assertTrue(_can_replay_result(self.user, result))
        self.record.fixed_branch_codes = ['0254']; self.record.save()
        self.assertFalse(_can_replay_result(self.user, result))
        self.enable_fixed()
        self.user.groups.clear()
        self.assertFalse(_can_replay_result(self.user, result))
        self.policy.mode = 'NONE'; self.policy.save()
        self.assertFalse(current_access(self.user, metadata))

    def test_approval_and_group_controls_still_apply(self):
        self.enable_fixed()
        self.record.requires_approval = True; self.record.save()
        tool = Tool(name=NAME, inputSchema=SCHEMA)
        self.assertEqual(gateway._decision(self.user, NAME, ARGS, tool)['status'], 'approval_required')
        self.user.groups.clear()
        self.assertEqual(gateway._decision(self.user, NAME, ARGS, tool)['code'], 'permission_denied')

    def run_gateway(self, revoke=False, outside=False):
        self.enable_fixed()
        @asynccontextmanager
        async def transport(*a, **kw):
            yield None, None, None
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(tools=[Tool(name=NAME, inputSchema=SCHEMA)], nextCursor=None)
        async def call(name, arguments):
            assert 'loan_access_token' not in arguments
            data = envelope()
            if outside:
                data['scope']['authorized_branch_count'] = 2
            if revoke:
                await sync_to_async(LoanAccessPolicy.objects.filter(pk=self.policy.pk).update)(mode='NONE')
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(data))])
        session.call_tool.side_effect = call
        config = {'url': 'https://gateway.example/mcp', 'token': '', 'timeout': 2, 'request_timeout': 10}
        with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
             patch('mcp.ClientSession', return_value=session), \
             patch.object(gateway, 'load_config', return_value=config), \
             patch.object(gateway, '_select_tool', new=AsyncMock(return_value=({}, (NAME, ARGS)))):
            return gateway.run_mcp_query(self.user, QUERY), session

    def test_complete_fixed_flow_dispatches_without_token_and_returns_verified_report(self):
        with patch.dict(os.environ, {'LOAN_ACCESS_SIGNING_KEY': ''}):
            result, session = self.run_gateway()
        self.assertTrue(result['ok'], result)
        session.call_tool.assert_awaited_once()
        self.assertEqual(result['loan_access']['mode'], 'FIXED')

    def test_complete_flow_hides_results_after_revocation_or_excess_scope(self):
        for options in ({'outside': True}, {'revoke': True}):
            result, _ = self.run_gateway(**options)
            self.assertEqual(result['code'], 'loan_scope_unverified', result)


class FixedScopeSetupTests(TestCase):
    setUp = setup_tests.GuidedSetupTests.setUp
    post = setup_tests.GuidedSetupTests.post
    tool = setup_tests.GuidedSetupTests.tool

    @patch('agentic_ai.setup_views._save_settings')
    def test_setup_uses_user_access_and_rejects_legacy_permission_modes(self, save):
        self.post('discover'); self.post('check_model')
        for mode in ('SIGNED', 'FIXED'):
            response=self.post('save_tools', tools=[self.tool(loan_access_mode=mode, fixed_branch_codes=['0250'])])
            self.assertEqual(response.status_code,400,response.content)
            self.assertFalse(AgentTool.objects.exists())
        response=self.post('save_tools',tools=[self.tool(loan_access_mode='UI',fixed_branch_codes=[])])
        self.assertEqual(response.status_code,200,response.content)
        record=AgentTool.objects.get()
        self.assertEqual(record.loan_access_mode,'UI')
        self.assertEqual(record.fixed_branch_codes,[])

    def test_bad_scope_rejected_before_any_configuration_is_saved(self):
        self.post('discover'); self.post('check_model')
        for codes in ([], [250], ['0250', '0250'], '0250'):
            response = self.post('save_tools', tools=[self.tool(loan_access_mode='FIXED', fixed_branch_codes=codes)])
            self.assertEqual(response.status_code, 400, response.content)
            self.assertFalse(AgentTool.objects.exists())

    def test_configuration_is_only_available_to_setup_administrators(self):
        self.user.is_superuser = False; self.user.save()
        response = self.post('save_tools', tools=[self.tool(loan_access_mode='FIXED', fixed_branch_codes=['0250'])])
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AgentTool.objects.exists())
