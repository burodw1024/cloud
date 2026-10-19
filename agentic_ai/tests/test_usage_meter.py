import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase
from agentic_ai.usage_meter import begin_call, finish_call, summarize_usage, validate_price
from agentic_ai import mcp_gateway as gateway
from agentic_ai.models import AgentRunLog
from . import test_mcp_gateway
from . import test_setup

PRICE={'currency':'USD','input_per_million':'2','output_per_million':'8','cached_input_per_million':'0.5'}


class UsageTests(SimpleTestCase):
    def test_openai_cached_input_counts_once_with_decimal_pricing(self):
        meter=[]
        entry=begin_call({'provider':'openai','model':'configured-model','usage_meter':meter})
        finish_call(entry,{'usage':{'prompt_tokens':1000,'completion_tokens':200,'prompt_tokens_details':{'cached_tokens':400}}})
        summary=summarize_usage(meter,{'openai:configured-model':PRICE})
        self.assertEqual(summary['total_tokens'],1200)
        self.assertEqual(Decimal(summary['estimated_cost']),Decimal('0.003'))
        self.assertEqual(summary['cost_status'],'estimated')

    def test_ollama_totals_include_all_model_calls_but_unpriced_is_not_free(self):
        meter=[]
        for i in range(3):
            entry=begin_call({'provider':'ollama','model':'local-model','usage_meter':meter})
            finish_call(entry,{'prompt_eval_count':100,'eval_count':20})
        summary=summarize_usage(meter,{})
        self.assertEqual(summary['model_calls'],3)
        self.assertEqual(summary['total_tokens'],360)
        self.assertIsNone(summary['estimated_cost'])
        self.assertEqual(summary['cost_status'],'unpriced')

    def test_timeout_and_missing_counts_are_explicitly_incomplete(self):
        meter=[]
        entry=begin_call({'provider':'openai','model':'configured-model','usage_meter':meter})
        finish_call(entry,{'usage':{'prompt_tokens':100,'completion_tokens':10}})
        begin_call({'provider':'openai','model':'configured-model','usage_meter':meter})
        result=summarize_usage(meter,{'openai:configured-model':PRICE})
        self.assertFalse(result['complete'])
        self.assertEqual(result['reported_calls'],1)
        self.assertEqual(result['cost_status'],'partial_estimate')

    def test_invalid_rates_or_provider_counts_are_never_fabricated(self):
        for value in ('NaN','Infinity','-1',True,None):
            with self.assertRaises(ValueError):
                validate_price({**PRICE,'input_per_million':value})
        for usage in ({'prompt_tokens':True,'completion_tokens':2},
                      {'prompt_tokens':1,'completion_tokens':2,'prompt_tokens_details':{'cached_tokens':3}},
                      {'prompt_tokens':10},{}):
            meter=[];entry=begin_call({'provider':'openai','model':'m','usage_meter':meter})
            finish_call(entry,{'usage':usage})
            self.assertFalse(summarize_usage(meter,{})['complete'])

    def test_real_http_response_usage_is_captured_without_response_data(self):
        real=httpx.AsyncClient
        def respond(request):return httpx.Response(200,json={'message':{'content':'PRIVATE RESULT'},'prompt_eval_count':45,'eval_count':7})
        def client(**kwargs):return real(transport=httpx.MockTransport(respond),**kwargs)
        config={'provider':'ollama','model':'local-model','model_key':'','model_url':'http://model.example','timeout':1,'usage_meter':[]}
        with patch.object(gateway.httpx,'AsyncClient',side_effect=client):
            async_to_sync(gateway._model_response)(config,{'model':'local-model','messages':[]})
        self.assertEqual(config['usage_meter'][0]['input_tokens'],45)
        self.assertNotIn('PRIVATE RESULT',str(config['usage_meter']))


class UsageAuditTests(TestCase):
    setUp=test_mcp_gateway.RoleTests.setUp

    def test_usage_is_saved_with_original_request(self):
        async def run(config,*args):
            entry=begin_call(config)
            finish_call(entry,{'prompt_eval_count':100,'eval_count':20})
            return {'ok':True,'answer':'Test answer'}
        with patch.object(gateway,'load_config',return_value={'provider':'ollama','model':'local-model','request_timeout':1}), \
             patch.object(gateway,'_run',new=run),patch.object(gateway,'get_setting',return_value='{}'):
            result=gateway.run_mcp_query(self.user,'test question')
        self.assertEqual(result['usage']['total_tokens'],120)
        self.assertEqual(AgentRunLog.objects.get().usage_summary,result['usage'])


class PricingSetupTests(TestCase):
    setUp=test_setup.GuidedSetupTests.setUp
    post=test_setup.GuidedSetupTests.post

    @patch('agentic_ai.setup_views._pricing_context',return_value={'pricing_provider':'openai','pricing_model':'configured-model'})
    @patch('agentic_ai.setup_views.get_setting',return_value='{}')
    @patch('agentic_ai.setup_views._save_settings')
    def test_price_save_is_model_specific_and_does_not_enable_tools(self,save,setting,identity):
        response=self.post('save_pricing',provider='openai',model='configured-model',input_rate='2',output_rate='8',cached_rate='0.5',currency='USD')
        self.assertEqual(response.status_code,200)
        saved=save.call_args.args[0]
        self.assertEqual(set(saved),{'MCP_MODEL_PRICING'})
        self.assertEqual(json.loads(saved['MCP_MODEL_PRICING'])['openai:configured-model'],PRICE)

    @patch('agentic_ai.setup_views._pricing_context',return_value={'pricing_provider':'ollama','pricing_model':'local-model'})
    def test_stale_model_identity_does_not_change_pricing(self,identity):
        response=self.post('save_pricing',provider='openai',model='old-model',input_rate='2',output_rate='8')
        self.assertEqual(response.status_code,409)

    @patch('agentic_ai.setup_views._pricing_context',return_value={'pricing_provider':'openai','pricing_model':'configured-model'})
    @patch('agentic_ai.setup_views.get_setting',return_value='{}')
    @patch('agentic_ai.setup_views._save_settings')
    def test_invalid_rates_and_unprivileged_requests_do_not_write(self,save,setting,identity):
        response=self.post('save_pricing',provider='openai',model='configured-model',input_rate='-2',output_rate='8',currency='USD')
        self.assertEqual(response.status_code,400)
        save.assert_not_called()
        self.user.is_superuser=False
        self.user.save()
        self.assertEqual(self.post('save_pricing',provider='openai',model='configured-model',input_rate='2',output_rate='8').status_code,403)
        save.assert_not_called()

    @patch('agentic_ai.setup_views._pricing_context',return_value={'pricing_provider':'openai','pricing_model':'configured-model'})
    @patch('agentic_ai.setup_views.get_setting',return_value=json.dumps({'openai:configured-model':PRICE,'ollama:other-model':PRICE}))
    @patch('agentic_ai.setup_views._save_settings')
    def test_removing_one_models_price_preserves_other_prices(self,save,setting,identity):
        response=self.post('save_pricing',provider='openai',model='configured-model',input_rate='',output_rate='')
        self.assertEqual(response.status_code,200)
        self.assertEqual(json.loads(save.call_args.args[0]['MCP_MODEL_PRICING']),{'ollama:other-model':PRICE})
