from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
def load(name):
    path = ROOT/'deploy/nifi/signed-loan'/f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class NiFiFlowPreparationTests(unittest.TestCase):
    def fixture(self):
        def p(identifier, kind, version='2.4.0', properties=None):
            return dict(identifier=identifier, instanceIdentifier=identifier, name=kind, type='com.example.nifi.QueryLoanDue' if kind=='QueryLoanDue' else 'org.apache.nifi.'+kind,
                bundle={'version': version}, properties=properties or {}, groupIdentifier='group',position={'x':0,'y':0})
        def c(a,b,rel='success'):
            return dict(identifier=a+b+rel,source={'id':a},destination={'id':b},selectedRelationships=[rel])
        return {'flowContents': {'processors': [p('request','HandleHttpRequest',properties={'HTTP Context Map':'context'}),
            p('attr','UpdateAttribute',properties={'auth.branch_codes':'[]'}),p('query','QueryLoanDue','1.1.0',{'Authorized Branches JSON':'${auth.branch_codes}'}),
            p('response','HandleHttpResponse',properties={'HTTP Context Map':'context'}),p('log','LogMessage'),p('unrelated','Other',properties={'keep':'unchanged'})],
            'connections':[c('request','attr'),c('attr','query'),c('query','response'),c('query','log','failure')],
            'controllerServices':[{'identifier':'context','properties':{'untouched':True}}]}}

    def test_draft_preserves_unrelated_components_and_routes_failures_to_http(self):
        module=load('prepare_flow');source=self.fixture();before=deepcopy(source)
        result, summary=module.prepare(source)
        self.assertEqual(source,before)
        processors={p['identifier']:p for p in result['flowContents']['processors']}
        self.assertEqual(processors['unrelated'],source['flowContents']['processors'][-1])
        self.assertEqual(result['flowContents']['controllerServices'],source['flowContents']['controllerServices'])
        self.assertEqual(processors['response']['properties']['HTTP Status Code'],'${http.status.code}')
        self.assertNotIn('auth.branch_codes',processors['attr']['properties'])
        self.assertEqual(len(summary['new_processors']),2)
        self.assertFalse(summary['sql_enforcement_verified'])
        for identifier in summary['new_processors']+summary['changed_processors']:
            self.assertEqual(processors[identifier]['scheduledState'],'DISABLED')
        self.assertTrue(any(c['source']['id']=='attr' and c['selectedRelationships']==['failure'] and c['destination']['id']=='response'
                            for c in result['flowContents']['connections']))

    def test_shared_response_is_rejected(self):
        source=self.fixture();source['flowContents']['connections'].append({'source':{'id':'unrelated'},'destination':{'id':'response'}})
        with self.assertRaises(ValueError):load('prepare_flow').prepare(source)

    def test_schema_patch_preserves_backend_parameters_and_refuses_complex_contracts(self):
        schema={'type':'object','properties':{'branchCode':{'type':'string'}},'required':['branchCode'],'additionalProperties':False}
        result=load('patch_schema').patch(schema)
        self.assertEqual(result['required'],['branchCode','loan_access_token'])
        self.assertEqual(schema['required'],['branchCode'])
        with self.assertRaises(ValueError):load('patch_schema').patch({**schema,'allOf':[]})
