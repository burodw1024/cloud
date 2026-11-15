from unittest.mock import AsyncMock, patch
from mcp.types import Tool
from django.test import TestCase
from agentic_ai.models import AgentTool, UserToolAccess
from agentic_ai.mcp_gateway import _authorized_names, _decision
from django.contrib.auth.models import User
from .test_setup import GuidedSetupTests
from .test_native_loan_query import SCHEMA

WHATSAPP = Tool(name='send-whatsapp-template', inputSchema={
    'type':'object','properties':{'to':{'type':'string'},'text':{'type':'object'}},'required':['to','text']})
LOAN = Tool(name='loan-due-report', inputSchema=SCHEMA)


class StandardToolSetupTests(TestCase):
    setUp = GuidedSetupTests.setUp
    post = GuidedSetupTests.post

    def discover(self):
        self.post('discover');self.post('check_model')

    def configuration(self, name, mode, scoped):
        return dict(name=name,enabled=True,roles=['manager'],requires_approval=False,
                    requires_loan_scope=scoped,loan_access_mode=mode,fixed_branch_codes=[])

    @patch('agentic_ai.setup_views._save_settings')
    def test_loan_and_whatsapp_can_enable_gateway_together_without_loan_contract_on_whatsapp(self,save):
        with patch('agentic_ai.setup_views.discover_gateway',new=AsyncMock(return_value=[LOAN,WHATSAPP])):
            self.discover()
            result=self.post('save_tools',tools=[self.configuration(LOAN.name,'UI',True),self.configuration(WHATSAPP.name,'SIGNED',False)])
        self.assertEqual(result.status_code,200,result.content)
        save.assert_called_once_with({'MCP_GATEWAY_ENABLED':'true'})
        ordinary=AgentTool.objects.get(name=WHATSAPP.name)
        self.assertFalse(ordinary.requires_loan_scope)
        user=User.objects.create_user('24123')
        access=UserToolAccess.objects.create(user=user)
        access.tools.add(ordinary)
        self.assertEqual(_authorized_names(user),{WHATSAPP.name})
        self.assertIsNone(_decision(user,WHATSAPP.name,{'to':'example','text':{}},WHATSAPP))

    @patch('agentic_ai.setup_views._save_settings')
    def test_wrong_whatsapp_mode_explains_correction_and_saves_nothing(self,save):
        with patch('agentic_ai.setup_views.discover_gateway',new=AsyncMock(return_value=[LOAN,WHATSAPP])):
            self.discover()
            result=self.post('save_tools',tools=[self.configuration(LOAN.name,'UI',True),self.configuration(WHATSAPP.name,'UI',True)])
        self.assertEqual(result.status_code,400)
        self.assertIn('turn off loan branch/zone enforcement',result.json()['error'])
        self.assertFalse(AgentTool.objects.exists())
        save.assert_not_called()
