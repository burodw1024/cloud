import json
from unittest.mock import AsyncMock
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from agentic_ai.report_presentation import present_report, report_rows


class ReportPresentationTests(SimpleTestCase):
    config={"provider":"ollama","model":"project-selected-model", "summarize_reports": False}

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


class MemberListTests(SimpleTestCase):
    config = {"provider": "ollama", "model": "project-selected-model", "summarize_reports": False}

    def present(self, rows, **decision):
        choice = {"columns": ["MEMBERID"], "clarification": "",
                  "operation": "list_member_ids", "limit": 10, **decision}
        model = AsyncMock(return_value={"message": {"content": json.dumps(choice)}})
        result = async_to_sync(present_report)(self.config,
            'List the first 10 member IDs in branch 0253 as of 2026-09-30. Show member ID only.',
            {"structuredContent": {"rows": rows}}, model)
        return result, model

    def test_unique_ordered_bounded_ids_preserve_zeros_and_exclude_other_data(self):
        rows = [{"MEMBERID": value, "MEMBERNAME": "PRIVATE NAME", "PHONENO": "PRIVATE PHONE",
                 "DUEAMOUNT": "777.25"} for value in ["002", "001", "002", "010"]]
        result, model = self.present(rows, limit=2)
        data = result['raw']['structuredContent']
        self.assertEqual(data['rows'], [{'MEMBERID': '001'}, {'MEMBERID': '002'}])
        self.assertEqual(data['row_count'], 3)
        self.assertEqual(data['source_report_rows'], 4)
        self.assertTrue(data['truncated'])
        self.assertIn('not a complete branch member directory', result['answer'])
        for value in ('PRIVATE', '777.25', 'DUEAMOUNT'):
            self.assertNotIn(value, str(result))
        payload = str(model.call_args.args[1])
        for value in ('PRIVATE', '777.25', '001', '002', '010'):
            self.assertNotIn(value, payload)

    def test_ten_ids_not_ten_loan_rows(self):
        rows = [{'MEMBERID': f'{n:04d}'} for n in range(15, 0, -1)] * 2
        result, _ = self.present(rows)
        data = result['raw']['structuredContent']
        self.assertEqual(len(data['rows']), 10)
        self.assertEqual(data['rows'][0], {'MEMBERID': '0001'})
        self.assertEqual(data['rows'][-1], {'MEMBERID': '0010'})
        self.assertEqual(data['row_count'], 15)

    def test_invalid_limits_and_identifiers_fail_closed(self):
        for limit in (0, 101, True, '10', 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.present([{'MEMBERID': '001'}], limit=limit)
        for value in (None, 123, '', ' 001', '001\n', ['001']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.present([{'MEMBERID': value}])

    def test_member_listing_cannot_leak_additional_columns_or_invent_missing_ids(self):
        for rows, choice in [([{'DUEAMOUNT': 1}], {}),
                             ([{'MEMBERID': '001', 'DUEAMOUNT': 1}], {'columns': ['MEMBERID', 'DUEAMOUNT']}),
                             ([{'MEMBERID': '001'}], {'operation': 'project_columns'})]:
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                self.present(rows, **choice)

    def test_default_limit_and_single_id_only_row(self):
        model = AsyncMock(return_value={'message': {'content': json.dumps({
            'columns': ['MEMBERID'], 'clarification': '', 'operation': 'list_member_ids'})}})
        result = async_to_sync(present_report)(self.config, 'list member IDs',
            {'structuredContent': {'MEMBERID': '0001'}}, model)
        self.assertEqual(result['raw']['structuredContent']['limit'], 10)
        self.assertEqual(result['raw']['structuredContent']['rows'], [{'MEMBERID': '0001'}])


class ReportSummaryTests(SimpleTestCase):
    def test_natural_intro_receives_metadata_only_and_keeps_exact_report(self):
        from agentic_ai.report_presentation import summarize_report
        report = {'answer': 'MEMBERID: SECRET-ID-001', 'raw': {'structuredContent': {
            'columns': ['MEMBERID'], 'rows': [{'MEMBERID': 'SECRET-ID-001'}],
            'row_count': 1, 'scope': 'members present in the returned loan report'}}}
        model = AsyncMock(return_value={'message': {'content': json.dumps({
            'summary': 'Here is 1 member ID from the returned loan report. You can find it below.'})}})
        result = async_to_sync(summarize_report)({'provider': 'ollama', 'model': 'project-model'}, report, model)
        self.assertIn('Here is 1', result['answer'])
        self.assertIn('SECRET-ID-001', result['answer'])
        self.assertEqual(result['raw'], report['raw'])
        self.assertNotIn('SECRET-ID-001', str(model.call_args))
        self.assertEqual(model.call_args.args[0]['timeout'], 15)

    def test_optional_intro_failure_never_loses_report(self):
        from agentic_ai.report_presentation import summarize_report
        report = {'answer': 'DUEAMOUNT: 50', 'raw': {'structuredContent': {
            'columns': ['DUEAMOUNT'], 'rows': [{'DUEAMOUNT': 50}], 'row_count': 1}}}
        for model in (AsyncMock(side_effect=TimeoutError()), AsyncMock(return_value={
            'message': {'content': json.dumps({'summary': 'Your outstanding total is 9999.'})}})):
            result = async_to_sync(summarize_report)({'provider': 'ollama', 'model': 'project-model'}, report, model)
            self.assertEqual(result, report)
            self.assertNotIn('"DUEAMOUNT": 50', str(model.call_args))
