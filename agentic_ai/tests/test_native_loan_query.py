from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from mcp.types import Tool, CallToolResult, TextContent
from agentic_ai import mcp_gateway as gateway
from agentic_ai.loan_report_contract import prepare_tools, ranking_candidates
from agentic_ai.native_loan_query import supports_native_query, validate_native_call, present_native_query

NAME = 'loan-due-report'
SCHEMA = {
    'type': 'object', 'properties': {
        **{k: {'type': 'string'} for k in ('branch_code','zone_code','member_id','product_name','employee_code')},
        **{k: {'type': 'string', 'format': 'date'} for k in ('report_date','disbursed_from','disbursed_to')},
        'level': {'type': 'string', 'enum': ['LOAN','MEMBER','BRANCH','ZONE','AREA','DIVISION','PRODUCT','EMPLOYEE']},
        'sort_by': {'type': 'string', 'enum': ['DUEAMOUNT','OUTSTANDINGP','OUTSTANDING_PI','DUE_DAYS','LOAN_COUNT']},
        'sort_order': {'type': 'string', 'enum': ['ASC','DESC']},
        'top_n': {'type': 'integer', 'minimum': 1, 'maximum': 1000},
        'min_due': {'type': 'number', 'minimum': 0}, 'min_days': {'type': 'number', 'minimum': 0}},
    'required': ['report_date'], 'additionalProperties': False}
TOOL = Tool(name=NAME, inputSchema=SCHEMA, description='Loan query')
QUERY = ('Rank members in branch 0253 by total overdue for loans disbursed between '
         'January 1 and December 31, 2025, measured as of September 30, 2026.')
ARGS = {'report_date': '2026-09-30', 'branch_code': '0253', 'level': 'MEMBER',
        'sort_by': 'DUEAMOUNT', 'sort_order': 'DESC', 'top_n': 10,
        'disbursed_from': '2025-01-01', 'disbursed_to': '2025-12-31'}


def envelope(arguments=None):
    a = arguments or ARGS
    return {'success': True, 'report_date': a['report_date'], 'level': a.get('level','LOAN'),
            'sort_by': a.get('sort_by','DUEAMOUNT'), 'sort_order': a.get('sort_order','DESC'),
            'top_n': a.get('top_n',10), 'scope': {'branch_code': a.get('branch_code'),
                'zone_code': a.get('zone_code'), 'authorized_branch_count': 1},
            'row_count': 2, 'data': [
                {'RANK_NO': rank, 'BRANCHCODE': '0253', 'ENTITY_CODE': member,
                 'ENTITY_NAME': 'PRIVATE_NAME', 'DUEAMOUNT': due, 'OUTSTANDINGP': 100,
                 'OUTSTANDING_PI': 125, 'DUE_DAYS': 30.25, 'LOAN_COUNT': 2,
                 'PHONENO': 'PRIVATE_PHONE'}
                for rank, member, due in [(1,'0001',200.5),(2,'0002',100)]]}


class NativeLoanQueryTests(SimpleTestCase):
    def test_capability_requires_declared_schema_and_enabled_name(self):
        self.assertTrue(supports_native_query(TOOL,{NAME}))
        self.assertFalse(supports_native_query(TOOL,set()))
        old = Tool(name=NAME,inputSchema={'type':'object','properties':{
            'branchCode':{'type':'string'},'reportDate':{'type':'string'}}})
        self.assertFalse(supports_native_query(old,{NAME}))
        self.assertEqual(ranking_candidates([old,TOOL],{NAME}),[TOOL])
        self.assertIn('disbursed_from',prepare_tools([TOOL],{NAME})[0].description)

    def test_natural_calendar_dates_and_exact_scope(self):
        self.assertEqual(validate_native_call(TOOL,ARGS,QUERY),ARGS)
        for key, value in [('branch_code','0254'), ('report_date','2025-12-31'),
                           ('disbursed_from','2024-01-01'), ('level','LOAN')]:
            with self.subTest(key=key),self.assertRaises(Exception):
                validate_native_call(TOOL,{**ARGS,key:value},QUERY)

    def test_identifier_leading_zero_and_unsupported_inputs(self):
        for extra in [{'operation':'rank_overdue_members'},{'branchCode':'0253'},
                      {'authorized_branches':['0254']},{'top_n':1001}, {'top_n':True}]:
            with self.subTest(extra=extra),self.assertRaises(Exception):
                validate_native_call(TOOL,{**ARGS,**extra},QUERY)

    def test_backend_ranks_presented_without_local_aggregation_or_private_data(self):
        result=present_native_query({'content':[{'type':'text','text':json.dumps(envelope())}]},ARGS)
        data=result['raw']['structuredContent']
        self.assertEqual(data['rows'][0]['DUEAMOUNT'],'200.5')
        self.assertEqual(data['rows'][0]['ENTITY_CODE'],'0001')
        self.assertEqual(data['rows'][0]['DUE_DAYS'],'30.25')
        self.assertNotIn('PRIVATE_',json.dumps(result))
        self.assertIn('does not establish complete zone coverage',result['answer'])
        self.assertEqual(data['filters']['disbursed_from'],'2025-01-01')

    def test_response_metadata_scope_count_and_order_fail_closed(self):
        cases=[]
        for key,value in [('success',False),('report_date','2026-09-29'),('level','LOAN'),
                          ('row_count',3),('top_n',1)]:
            d=envelope();d[key]=value;cases.append(d)
        d=envelope();d['scope']['branch_code']='0254';cases.append(d)
        d=envelope();d['scope']['authorized_branch_count']=0;cases.append(d)
        d=envelope();d['data'][0]['BRANCHCODE']='0254';cases.append(d)
        d=envelope();d['data'][0]['DUEAMOUNT']='NaN';cases.append(d)
        d=envelope();d['data'][1]['DUEAMOUNT']=300;cases.append(d)
        d=envelope();d['data'][1]['RANK_NO']=1;cases.append(d)
        for d in cases:
            with self.subTest(d=d),self.assertRaises(ValueError):
                present_native_query({'structuredContent':d},ARGS)

    def test_empty_results_do_not_invent_a_balance(self):
        d=envelope();d.update(data=[],row_count=0)
        result=present_native_query({'structuredContent':d},ARGS)
        self.assertEqual(result['raw']['structuredContent']['rows'],[])

    def test_zone_branch_ranking_is_native_candidate(self):
        a={'report_date':'2026-09-30','zone_code':'Z015','level':'BRANCH','top_n':10}
        validate_native_call(TOOL,a,'Rank the top 10 branches in zone Z015 by overdue as of September 30, 2026.')

    def test_gateway_routes_native_query_and_keeps_role_approval_checks(self):
        @asynccontextmanager
        async def transport(*args,**kwargs): yield None,None,None
        session=AsyncMock();session.__aenter__.return_value=session
        session.list_tools.return_value=SimpleNamespace(tools=[TOOL],nextCursor=None)
        session.call_tool.return_value=CallToolResult(content=[TextContent(type='text',text=json.dumps(envelope()))])
        config={'provider':'ollama','model':'test','url':'http://gateway.example/mcp','token':'','timeout':1}
        for blocked in [None,{'ok':False,'approval_required':True},{'ok':False,'code':'permission_denied'}]:
            session.call_tool.reset_mock()
            with patch('mcp.client.streamable_http.streamablehttp_client',transport), \
                 patch('mcp.ClientSession',return_value=session), \
                 patch.object(gateway,'_select_tool',new=AsyncMock(return_value=({},(NAME,ARGS)))), \
                 patch.object(gateway,'_decision',new=lambda *args:blocked), \
                 patch.object(gateway,'get_setting',return_value=NAME):
                result=async_to_sync(gateway._run)(dict(config),None,QUERY,{NAME})
            if blocked:
                session.call_tool.assert_not_awaited()
            else:
                self.assertTrue(result['ok'])
                session.call_tool.assert_awaited_once_with(NAME,arguments=ARGS)
                self.assertEqual(result['raw']['structuredContent']['ranking_source'],'oracle_native_query')

    def test_zone_branch_ranking_passes_gateway_guard(self):
        args={'report_date':'2026-09-30','zone_code':'Z015','level':'BRANCH','top_n':10}
        query='Rank the top 10 branches in zone Z015 by overdue as of September 30, 2026.'
        data=envelope(args)
        for row,branch in zip(data['data'],['0253','0254']):
            row.update(BRANCHCODE=branch,ENTITY_CODE=branch,ENTITY_NAME='Synthetic Branch')
        @asynccontextmanager
        async def transport(*a,**kw): yield None,None,None
        session=AsyncMock();session.__aenter__.return_value=session
        session.list_tools.return_value=SimpleNamespace(tools=[TOOL],nextCursor=None)
        session.call_tool.return_value=CallToolResult(content=[TextContent(type='text',text=json.dumps(data))])
        config={'provider':'ollama','model':'test','url':'http://gateway.example/mcp','token':'','timeout':1}
        with patch('mcp.client.streamable_http.streamablehttp_client',transport), \
             patch('mcp.ClientSession',return_value=session), \
             patch.object(gateway,'_select_tool',new=AsyncMock(return_value=({},(NAME,args)))), \
             patch.object(gateway,'_decision',new=lambda *a:None), \
             patch.object(gateway,'get_setting',return_value=NAME):
            result=async_to_sync(gateway._run)(config,None,query,{NAME})
        self.assertTrue(result['ok'])
        session.call_tool.assert_awaited_once_with(NAME,arguments=args)

    def test_product_totals_with_branch_filter_use_confirmed_envelope_scope(self):
        args={'report_date':'2026-09-30','branch_code':'0253','level':'PRODUCT'}
        data=envelope(args)
        for row in data['data']:
            row['BRANCHCODE']=None
            row['ENTITY_NAME']='Synthetic Product'
        result=present_native_query({'structuredContent':data},args)
        self.assertEqual(result['raw']['structuredContent']['scope']['branch_code'],'0253')
        self.assertEqual(len(result['raw']['structuredContent']['rows']),2)

    def test_previous_gateway_source_is_recognized_exactly_for_deployment(self):
        from deploy.verify_gateway_source import verify
        root=Path(__file__).resolve().parents[2]
        previous=(root/'agentic_ai/tests/fixtures/native_query_previous_gateway.py.txt').read_text()
        baked=(root/'agentic_ai/mcp_gateway.py').read_text()
        baseline=(root/'deploy/gateway_baseline.sha256').read_text().strip()
        verify(previous,baked,baseline)
        with self.assertRaises(ValueError): verify(previous+'\nUNREVIEWED=True\n',baked,baseline)
