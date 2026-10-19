import json
from unittest.mock import AsyncMock
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from agentic_ai.report_presentation import present_report, report_rows


class ReportPresentationTests(SimpleTestCase):
    config={"provider":"ollama","model":"project-selected-model"}

    def test_model_selects_columns_and_private_data_never_returned(self):
        raw={"content":[{"type":"text","text":json.dumps({"rows":[{
            "OUTSTANDINGP":"125000.50","DUEAMOUNT":"50.25","MEMBERNAME":"Private Name","PHONENO":"Private Phone"}]})}]}
        model=AsyncMock(return_value={"message":{"content":json.dumps({"columns":["OUTSTANDINGP"],"clarification":""})}})
        result=async_to_sync(present_report)(self.config,"Show outstanding",raw,model)
        self.assertIn("125000.50",result["answer"])
        self.assertNotIn("Private",str(result))
        self.assertNotIn("DUEAMOUNT",str(result))
        payload=model.call_args.args[1]
        self.assertEqual(payload['model'],'project-selected-model')
        self.assertFalse(payload['think'])
        self.assertNotIn('Private',json.dumps(payload))
        self.assertNotIn('125000.50',json.dumps(payload))

    def test_model_cannot_choose_unapproved_columns(self):
        raw={"structuredContent":{"rows":[{"OUTSTANDINGP":10,"PHONENO":"private"}]}}
        model=AsyncMock(return_value={"message":{"content":json.dumps({"columns":["PHONENO"],"clarification":""})}})
        with self.assertRaises(ValueError):
            async_to_sync(present_report)(self.config,'show phone',raw,model)

    def test_unknown_shapes_never_return_raw(self):
        for raw in ({"content":[{"type":"text","text":"upstream error"}]},
                    {"structuredContent":{"error":"failed","rows":[]}}):
            with self.assertRaises(ValueError): report_rows(raw)

    def test_unsupported_calculation_clarifies_without_financial_data(self):
        raw={"structuredContent":{"rows":[{"OUTSTANDINGP":10}]}}
        model=AsyncMock(return_value={"message":{"content":json.dumps({"columns":[],"clarification":"Ranking is not supported by this tool."})}})
        result=async_to_sync(present_report)(self.config,'highest outstanding',raw,model)
        self.assertTrue(result['clarification_required'])
        self.assertNotIn('OUTSTANDINGP',str(result))

    def test_empty_rows_skip_model_and_truncation_is_explicit(self):
        model=AsyncMock()
        result=async_to_sync(present_report)(self.config,'report',{"structuredContent":{"rows":[]}},model)
        model.assert_not_called()
        model.return_value={"message":{"content":json.dumps({"columns":["DUEAMOUNT"],"clarification":""})}}
        result=async_to_sync(present_report)(self.config,'report',{"structuredContent":{"rows":[{"DUEAMOUNT":1}]*101}},model)
        self.assertTrue(result['raw']['structuredContent']['truncated'])
        self.assertEqual(len(result['raw']['structuredContent']['rows']),100)

    def test_live_gateway_path_returns_only_projected_data(self):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from unittest.mock import patch
        from mcp.types import Tool, CallToolResult, TextContent
        from agentic_ai import mcp_gateway as gateway
        name='loan-due-report'
        tool=Tool(name=name,inputSchema={'type':'object','properties':{}})
        session=AsyncMock()
        session.__aenter__.return_value=session
        session.list_tools.return_value=SimpleNamespace(tools=[tool],nextCursor=None)
        session.call_tool.return_value=CallToolResult(content=[TextContent(type='text',text=json.dumps({
            'rows':[{'OUTSTANDINGP':'10.50','DUEAMOUNT':'2.00','PHONENO':'private'}]}))])
        @asynccontextmanager
        async def transport(*args,**kwargs): yield (None,None,None)
        config={**self.config,'url':'https://mcp.example/mcp','token':'','timeout':1}
        model=AsyncMock(return_value={'message':{'content':json.dumps({'columns':['OUTSTANDINGP'],'clarification':''})}})
        with patch('mcp.client.streamable_http.streamablehttp_client',transport), \
             patch('mcp.ClientSession',return_value=session), \
             patch.object(gateway,'_select_tool',new=AsyncMock(return_value=({},(name,{})))), \
             patch.object(gateway,'_decision',new=lambda *a: None), \
             patch.object(gateway,'get_setting',return_value=name), \
             patch.object(gateway,'_model_response',new=model):
            result=async_to_sync(gateway._run)(config,None,'show outstanding',{name})
        self.assertTrue(result['ok'])
        self.assertIn('10.50',result['answer'])
        self.assertNotIn('private',str(result))
        self.assertNotIn('DUEAMOUNT',str(result))
        session.call_tool.assert_awaited_once()
