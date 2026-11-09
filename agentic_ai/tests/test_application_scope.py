from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone
from mcp.types import Tool, CallToolResult, TextContent

from agentic_ai import mcp_gateway as gateway
from agentic_ai.api import _can_replay_result
from agentic_ai.application_scope import ApplicationLoanGrant, SCOPE_FIELD
from agentic_ai.loan_access import authorize, current_access, validate_response, strip_server_authorization, LoanAccessDenied, LoanAccessUnavailable
from agentic_ai.models import AgentTool, LoanAccessPolicy, LoanBranch, LoanZone
from .test_native_loan_query import SCHEMA, envelope

NAME='loan-due-report'
ARGS={'report_date':'2026-09-30','level':'LOAN','sort_by':'DUEAMOUNT','sort_order':'DESC','top_n':10}
QUERY='As of 2026-09-30 show top 10 loans by overdue descending across my permitted branches.'


def app_tool():
    schema=deepcopy(SCHEMA)
    schema['properties'][SCOPE_FIELD]={'type':'array','items':{'type':'string'},'minItems':1,'maxItems':5000}
    schema['required'].append(SCOPE_FIELD)
    return Tool(name=NAME,inputSchema=schema)


def result(args=ARGS, branches=None):
    branches=branches or ['0003']
    data=envelope(args)
    data['scope'].update(authorization_mode='APPLICATION_SCOPE',authorized_branch_count=len(branches))
    data['data']=[];data['row_count']=0
    return data


class ApplicationScopeTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('24123')
        self.user.groups.add(Group.objects.create(name='loan_users'))
        self.record=AgentTool.objects.create(name=NAME,allowed_roles=['loan_users'],loan_access_mode='APP',requires_loan_scope=True)
        self.zone=LoanZone.objects.create(code='01',name='Zone one')
        self.branch=LoanBranch.objects.create(code='0003',name='Branch 3',zone=self.zone)
        self.other=LoanBranch.objects.create(code='0250',name='Branch 250',zone=self.zone)
        self.policy=LoanAccessPolicy.objects.create(user=self.user,mode='SELECTED')
        self.policy.branches.add(self.branch)

    def grant(self,args=None):return authorize(self.user,NAME,args if args is not None else ARGS,app_tool())

    def test_user_access_default_for_any_module(self):
        for name in ('knowledge-search', 'whatsapp', 'custom-module-tool'):
            tool=AgentTool.objects.create(name=name)
            self.assertEqual(tool.loan_access_mode,'UI')
            self.assertFalse(tool.requires_loan_scope)

    def test_ui_financial_mode_uses_no_signing_key(self):
        self.record.loan_access_mode='UI';self.record.save()
        with patch.dict(os.environ,{'LOAN_ACCESS_SIGNING_KEY':''}):
            g=self.grant({'report_date':'2026-09-30','branch_code':'0003'})
        self.assertIsInstance(g,ApplicationLoanGrant)
        self.assertEqual(g.branches,['0003'])
        self.assertTrue(current_access(self.user,g.access))

    def test_configuration_migrations_preserve_all_user_grants(self):
        import importlib
        from django.apps import apps
        from django.db import connection
        from agentic_ai.models import UserToolAccess
        explicit=UserToolAccess.objects.create(user=self.user)
        explicit.tools.add(self.record)
        self.record.loan_access_mode='SIGNED';self.record.save()
        ordinary=AgentTool.objects.create(name='knowledge-search',loan_access_mode='SIGNED')
        fixed=AgentTool.objects.create(name='savings-report',loan_access_mode='FIXED',fixed_branch_codes=['0250'])
        editor=SimpleNamespace(connection=connection)
        for _ in range(2):
            importlib.import_module('agentic_ai.migrations.0011_loan_due_application_scope').configure_loan_due(apps,editor)
            importlib.import_module('agentic_ai.migrations.0012_user_access_default').configure_user_access(apps,editor)
        self.record.refresh_from_db();ordinary.refresh_from_db();fixed.refresh_from_db();self.policy.refresh_from_db()
        self.assertEqual(self.record.loan_access_mode,'APP')
        self.assertEqual(ordinary.loan_access_mode,'UI')
        self.assertFalse(ordinary.requires_loan_scope)
        self.assertEqual(fixed.loan_access_mode,'UI')
        self.assertTrue(fixed.requires_loan_scope)
        self.assertEqual(fixed.fixed_branch_codes,[])
        self.assertEqual(self.policy.mode,'SELECTED')
        self.assertEqual(list(self.policy.branches.values_list('code',flat=True)),['0003'])
        self.assertEqual(list(explicit.tools.values_list('name',flat=True)),[NAME])

    def test_app_readiness_does_not_require_a_signing_key(self):
        from agentic_ai.access_readiness import report_readiness
        status=report_readiness([self.record],[app_tool()],False,{NAME})
        self.assertEqual(status[0]['code'],'application_scope_detected')

    def test_no_single_branch_required_and_no_signing_key(self):
        with patch.dict(os.environ,{'LOAN_ACCESS_SIGNING_KEY':''}):g=self.grant()
        self.assertIsInstance(g,ApplicationLoanGrant)
        self.assertEqual(g.branches,['0003'])
        self.assertNotIn('branch_code',g.query)
        self.assertEqual(g.access['mode'],'APP')

    def test_all_1414_branches_are_resolved_by_application(self):
        self.policy.mode='ALL';self.policy.save()
        LoanBranch.objects.bulk_create([LoanBranch(code=f'X{i:04}',name=f'Branch {i}',zone=self.zone) for i in range(1412)])
        self.assertEqual(len(self.grant().branches),1414)
        self.assertEqual(self.grant({**ARGS,'branch_code':'0003'}).branches,['0003'])

    def test_unauthorized_branch_and_zone_are_denied_before_dispatch(self):
        for args in [{**ARGS,'branch_code':'0250'},{**ARGS,'zone_code':'outside'}]:
            with self.assertRaises(LoanAccessDenied):self.grant(args)

    def test_caller_cannot_supply_scope_or_signed_credentials(self):
        for key in [SCOPE_FIELD,'authorized_branches','auth.branch_codes','loan_access_token']:
            with self.assertRaises(LoanAccessDenied):self.grant({**ARGS,key:['0250']})

    def test_scope_field_is_hidden_from_model_schema(self):
        visible=strip_server_authorization([app_tool()])[0]
        self.assertNotIn(SCOPE_FIELD,visible.inputSchema['properties'])
        self.assertNotIn(SCOPE_FIELD,visible.inputSchema['required'])
        self.assertIn('report_date',visible.inputSchema['required'])
        self.assertNotIn('branch_code',visible.inputSchema['required'])

    def test_legacy_or_signed_schema_does_not_silently_become_app_mode(self):
        with self.assertRaises(LoanAccessUnavailable):authorize(self.user,NAME,ARGS,Tool(name=NAME,inputSchema=SCHEMA))
        tool=app_tool();tool.inputSchema['required'].append('loan_access_token')
        with self.assertRaises(LoanAccessUnavailable):authorize(self.user,NAME,ARGS,tool)

    def test_scope_revocation_expiry_and_tool_mode_change_invalidate_result(self):
        meta=self.grant().access
        self.assertTrue(current_access(self.user,meta))
        self.record.loan_access_mode='SIGNED';self.record.save()
        self.assertFalse(current_access(self.user,meta))
        self.record.loan_access_mode='APP';self.record.save()
        self.policy.expires_at=timezone.now()-timedelta(seconds=1);self.policy.save()
        self.assertFalse(current_access(self.user,meta))

    def test_response_must_confirm_application_mode_and_exact_scope_count(self):
        grant=self.grant();validate_response({'structuredContent':result()},grant)
        for field,value in [('authorization_mode','PROJECT_BRANCH'),('authorized_branch_count',2),('authorized_branch_count',True),('branch_code','0250')]:
            body=result();body['scope'][field]=value
            with self.assertRaises(LoanAccessDenied):validate_response({'structuredContent':body},grant)

    def test_response_outside_scope_or_missing_loan_branch_is_rejected(self):
        grant=self.grant()
        for row in [{'BRANCHCODE':'0250'},{'BRANCHCODE':3},{'DUEAMOUNT':10}]:
            body=result();body['data']=[row];body['row_count']=1
            with self.assertRaises(LoanAccessDenied):validate_response({'structuredContent':body},grant)

    def test_cached_report_rechecks_grants(self):
        source={'tool':NAME,'plan':{'intent':'mcp_tool_call'},'loan_access':self.grant().access}
        self.assertTrue(_can_replay_result(self.user,source))
        self.policy.mode='NONE';self.policy.save()
        self.assertFalse(_can_replay_result(self.user,source))

    def run_gateway(self,revoke=False):
        @asynccontextmanager
        async def transport(*a,**kw):yield None,None,None
        session=AsyncMock();session.__aenter__.return_value=session
        session.list_tools.return_value=SimpleNamespace(tools=[app_tool()],nextCursor=None)
        async def call(name,arguments):
            self.assertEqual(arguments[SCOPE_FIELD],['0003'])
            self.assertNotIn('loan_access_token',arguments)
            self.assertNotIn('branch_code',arguments)
            if revoke:await sync_to_async(LoanAccessPolicy.objects.filter(pk=self.policy.pk).update)(mode='NONE')
            return CallToolResult(content=[],structuredContent=result())
        session.call_tool.side_effect=call
        config={'url':'https://gateway.example/mcp','token':'','timeout':2,'request_timeout':3}
        with patch('mcp.client.streamable_http.streamablehttp_client',transport),patch('mcp.ClientSession',return_value=session),patch.object(gateway,'load_config',return_value=config),patch.object(gateway,'_select_tool',return_value=({},(NAME,dict(ARGS)))):
            response=gateway.run_mcp_query(self.user,QUERY)
        return response,session

    def test_gateway_injects_scope_after_user_validation_and_accepts_native_response(self):
        response,session=self.run_gateway()
        self.assertTrue(response['ok'],response)
        self.assertEqual(response['loan_access']['branches'],['0003'])
        session.call_tool.assert_awaited_once()

    def test_revocation_during_call_withholds_report(self):
        response,session=self.run_gateway(revoke=True)
        self.assertFalse(response['ok'])
        self.assertEqual(response['code'],'loan_scope_unverified')
