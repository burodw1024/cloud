from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from importlib import import_module
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import SimpleTestCase

from django.contrib.auth.models import Group, User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from mcp.types import Tool, CallToolResult

from agentic_ai import mcp_gateway as gateway
from agentic_ai.api import _can_replay_result
from agentic_ai.application_scope import ApplicationLoanGrant, grant, validate_response
from agentic_ai.loan_access import LoanAccessDenied, current_access, snapshot, strip_server_authorization
from agentic_ai.models import AgentTool, BusinessModule, LoanAccessPolicy, LoanBranch, LoanZone, UserToolAccess
from .test_native_loan_query import SCHEMA, ARGS, QUERY, envelope


class ExplicitToolPermissionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('explicit-access')
        self.user.groups.add(Group.objects.create(name='finance'))
        self.tool = AgentTool.objects.create(name='savings-report', allowed_roles=['finance'])

    def test_explicit_selection_replaces_legacy_roles_and_empty_selection_denies(self):
        self.assertTrue(gateway._allowed(self.tool, self.user))
        access = UserToolAccess.objects.create(user=self.user)
        self.assertFalse(gateway._allowed(self.tool, self.user))
        access.tools.add(self.tool)
        self.user.groups.clear()
        self.assertTrue(gateway._allowed(self.tool, self.user))
        self.assertEqual(gateway._authorized_names(self.user), {'savings-report'})

    def test_expiry_and_inactive_module_override_explicit_grant(self):
        access = UserToolAccess.objects.create(user=self.user)
        access.tools.add(self.tool)
        access.expires_at = timezone.now() - timedelta(seconds=1)
        access.save()
        self.assertFalse(gateway._allowed(self.tool, self.user))
        access.expires_at = None
        access.save()
        self.tool.module = BusinessModule.objects.create(name='Paused', slug='paused', is_active=False)
        self.tool.save()
        self.assertFalse(gateway._allowed(self.tool, self.user))

    def test_app_tool_cannot_replay_without_verified_scope_metadata(self):
        self.tool.loan_access_mode = 'APP'
        self.tool.save()
        self.assertFalse(_can_replay_result(self.user, {'tool': self.tool.name, 'plan': {'intent': 'mcp_tool_call'}}))


class ApplicationScopeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('app-scope')
        self.zone = LoanZone.objects.create(code='07', name='Zone')
        self.branch = LoanBranch.objects.create(code='0253', name='Branch', zone=self.zone)
        self.policy = LoanAccessPolicy.objects.create(user=self.user, mode='SELECTED')
        self.policy.branches.add(self.branch)
        self.record = AgentTool.objects.create(name='loan-due-report', loan_access_mode='APP', allowed_roles=['staff'])
        self.tool = Tool(name=self.record.name, inputSchema=deepcopy(SCHEMA))

    def grant(self, args=None, tool=None):
        return grant(snapshot(self.user), self.record.name, args or ARGS, tool or self.tool)

    def test_single_branch_filter_and_revocation(self):
        access = self.grant()
        self.assertIsInstance(access, ApplicationLoanGrant)
        self.assertEqual(access.query['branch_code'], '0253')
        validate_response({'structuredContent': envelope()}, access)
        self.assertTrue(current_access(self.user, access.access))
        self.policy.mode = 'NONE'
        self.policy.save()
        self.assertFalse(current_access(self.user, access.access))

    def test_model_cannot_expand_branch_or_change_authorized_query(self):
        with self.assertRaises(LoanAccessDenied):
            self.grant({**ARGS, 'branch_code': '9999'})
        with self.assertRaises(LoanAccessDenied):
            grant(snapshot(self.user), self.record.name, ARGS, self.tool, {**ARGS, 'top_n': 100})
        outside = envelope()
        outside['data'][0]['BRANCHCODE'] = '9999'
        with self.assertRaises(LoanAccessDenied):
            validate_response({'structuredContent': outside}, self.grant())

    def test_multi_branch_requires_backend_list_contract_and_receipt(self):
        other = LoanBranch.objects.create(code='0254', name='Second', zone=self.zone)
        self.policy.branches.add(other)
        args = {k: v for k, v in ARGS.items() if k != 'branch_code'}
        with self.assertRaises(LoanAccessDenied):
            self.grant(args)
        schema = deepcopy(SCHEMA)
        schema['properties']['app_branch_codes'] = {'type': 'array', 'items': {'type': 'string'}}
        schema['required'].append('app_branch_codes')
        tool = Tool(name=self.record.name, inputSchema=schema)
        access = self.grant(args, tool)
        self.assertEqual(access.branches, ['0253', '0254'])
        self.assertNotIn('app_branch_codes', strip_server_authorization([tool])[0].inputSchema['properties'])
        response = envelope(args)
        with self.assertRaises(LoanAccessDenied):
            validate_response({'structuredContent': response}, access)
        response['scope'].update(authorization_mode='APPLICATION_SCOPE', authorized_branch_count=2)
        validate_response({'structuredContent': response}, access)


    def run_native_gateway(self, revoke=False):
        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield None, None, None
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(tools=[self.tool], nextCursor=None)
        async def call(name, arguments):
            if revoke:
                await sync_to_async(LoanAccessPolicy.objects.filter(pk=self.policy.pk).update)(mode='NONE')
            return CallToolResult(content=[], structuredContent=envelope(arguments))
        session.call_tool.side_effect = call
        config = {'url': 'https://gateway.example/mcp', 'token': '', 'timeout': 2, 'request_timeout': 10}
        with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
             patch('mcp.ClientSession', return_value=session), \
             patch.object(gateway, 'load_config', return_value=config), \
             patch.object(gateway, '_select_tool', new=AsyncMock(return_value=({}, (self.tool.name, ARGS)))):
            result = gateway.run_mcp_query(self.user, QUERY)
        return result, session

    def test_real_gateway_accepts_app_grant_and_rechecks_revocation(self):
        result, session = self.run_native_gateway()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['loan_access']['mode'], 'APP')
        self.assertEqual(session.call_tool.call_args.kwargs['arguments']['branch_code'], '0253')
        result, _ = self.run_native_gateway(revoke=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'loan_scope_unverified')


class SavingsCatalogSelectionTests(SimpleTestCase):
    def test_savings_report_excludes_delivery_tools_before_model_selection(self):
        from asgiref.sync import async_to_sync
        savings = Tool(name='savings-report', inputSchema={'type': 'object', 'properties': {
            key: {'type': 'string'} for key in ('operation', 'from_date', 'to_date_exclusive')}})
        delivery = Tool(name='send_whatsapp', description='Share savings reports', inputSchema={'type': 'object', 'properties': {}})
        with patch.object(gateway, '_select_tool_once', new=AsyncMock(return_value=({}, None))) as select:
            async_to_sync(gateway._select_tool)({}, 'Savings deposits report for October', [savings, delivery])
        self.assertEqual(select.call_args.args[2], [savings])

    def test_missing_savings_tool_does_not_fall_back_to_delivery(self):
        from asgiref.sync import async_to_sync
        delivery = Tool(name='send_whatsapp', inputSchema={'type': 'object', 'properties': {}})
        with patch.object(gateway, '_select_tool_once', new=AsyncMock()) as select:
            message, selection = async_to_sync(gateway._select_tool)({}, 'Savings deposits report', [delivery])
        self.assertIsNone(selection)
        self.assertEqual(message['selection_mode'], 'savings_catalog')
        select.assert_not_awaited()


class AdoptLivePermissionTablesTests(TransactionTestCase):
    def operation_and_states(self):
        module = import_module('agentic_ai.migrations.0009_reconcile_live_tool_access')
        executor = MigrationExecutor(connection)
        before = executor.loader.project_state([('agentic_ai', '0008_agenttool_fixed_branch_codes_and_more')])
        after = executor.loader.project_state([('agentic_ai', '0009_reconcile_live_tool_access')])
        return module.Migration.operations[-1], before, after

    def test_existing_permission_rows_and_tool_selection_survive_adoption(self):
        user = User.objects.create_user('existing-policy')
        tool = AgentTool.objects.create(name='existing-tool')
        access = UserToolAccess.objects.create(user=user)
        access.tools.add(tool)
        operation, before, after = self.operation_and_states()
        with connection.schema_editor() as editor:
            operation.database_forwards('agentic_ai', editor, before, after)
        self.assertEqual(UserToolAccess.objects.get(user=user).pk, access.pk)
        self.assertTrue(access.tools.filter(pk=tool.pk).exists())
        with connection.schema_editor() as editor, self.assertRaises(IrreversibleError):
            operation.database_backwards('agentic_ai', editor, after, before)

    def test_incompatible_existing_schema_is_rejected(self):
        operation, before, after = self.operation_and_states()
        with connection.schema_editor() as editor, patch.object(connection.introspection, 'get_table_description', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'incompatible'):
                operation.database_forwards('agentic_ai', editor, before, after)


class ReviewedLiveGatewayCompatibilityTests(TestCase):
    def test_only_exact_reviewed_live_gateway_is_accepted(self):
        root = Path(__file__).resolve().parents[2]
        verify = runpy.run_path(str(root / 'deploy/verify_gateway_source.py'))['verify']
        live = (root / 'agentic_ai/tests/fixtures/live_application_scope_gateway.py.txt').read_text()
        baked = (root / 'agentic_ai/mcp_gateway.py').read_text()
        baseline = (root / 'deploy/gateway_baseline.sha256').read_text().strip()
        verify(live, baked, baseline)
        with self.assertRaises(ValueError):
            verify(live + '\nUNREVIEWED_PERMISSION_CHANGE = True\n', baked, baseline)
