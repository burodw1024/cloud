import copy, json, os, subprocess, time, uuid, tempfile, unittest
from pathlib import Path
import jwt


ROOT = Path(__file__).resolve().parents[2]

@unittest.skipUnless(os.environ.get('NIFI_GROOVY_CLASSPATH'), 'Set NIFI_GROOVY_CLASSPATH for the Groovy/JVM integration suite')
class NiFiSignedScopeJVMTests(unittest.TestCase):
    def test_python_tokens_and_nifi_callbacks(self):
        KEY='test-only-nifi-signing-key-at-least-32-bytes'
        now=int(time.time())
        params={'branchCode':'0003','reportDate':'2026-09-30','operation':'rank_overdue_members','limit':10}
        claims={'v':1,'iss':'buro-insight-ai','aud':'buro-loan-query','sub':'24123','jti':uuid.uuid4().hex,
                'iat':now,'nbf':now,'exp':now+120,'tool':'loan-due-report','branches':['0003'],'query':params,
                'scope_version':'a'*64}
        
        def payload(c=None,key=KEY):return {**params,'loan_access_token':jwt.encode(c or claims,key,algorithm='HS256')}
        cases=[]
        def core(name,p=None,**kw):cases.append(dict(kind='core',name=name,payload=p or payload(),key=KEY,tool='loan-due-report',now=now,**kw))
        core('valid')
        for name,key,value in [('expired','exp',now-1),('wrong_tool','tool','savings-report'),('wrong_issuer','iss','other'),
                               ('future','iat',now+60),('too_long','exp',now+181),('empty_scope','branches',[]),
                               ('unsorted_scope','branches',['0250','0003']),('numeric_scope','branches',[3])]:
            c=copy.deepcopy(claims);c[key]=value;core(name,payload(c))
        core('bad_signature',payload(key=KEY+'wrong'))
        p=payload();p['branchCode']='0250';core('query_tampering',p)
        p=payload();p['authorized_branches']=['0250'];core('unsigned_scope',p)
        c=copy.deepcopy(claims);c['branches']=['0250'];core('branch_not_in_scope',payload(c))
        c=copy.deepcopy(claims);c['query']={**params,'label':'বাংলা'}
        p={**c['query'],'loan_access_token':jwt.encode(c,KEY,algorithm='HS256')};core('unicode',p)
        c=copy.deepcopy(claims);c['query']={**params,'limit':10.0};p={**c['query'],'loan_access_token':jwt.encode(c,KEY,algorithm='HS256')};core('float',p)
        attrs={'http.method':'POST','http.context.identifier':'request-123','http.query.string':'',
               'auth.branch_codes':'["9999"]','loan.access.verified':'forged'}
        cases.append(dict(kind='wrapper',name='request_wrapper',tool='loan-due-report',script='verify-loan-access.groovy',payload=payload(),attributes=attrs))
        p=payload();p['branchCode']='0250';cases.append(dict(kind='wrapper',name='request_rejected',tool='loan-due-report',script='verify-loan-access.groovy',payload=p,attributes=attrs))
        receipt={'version':1,'request_id':claims['jti'],'subject':claims['sub'],'scope_version':claims['scope_version'],'branch_codes':['0003']}
        postattrs={'http.context.identifier':'request-123','auth.branch_codes':'["0003"]','loan.access.verified':'true','loan.access.receipt':json.dumps(receipt)}
        for name,body in [('response_wrapper',{'success':True,'rows':[{'BRANCHCODE':'0003','DUEAMOUNT':'100'}]}),
                          ('response_outside_scope',{'rows':[{'BRANCHCODE':'0250'}]}),('response_numeric_scope',{'rows':[{'BRANCHCODE':3}]}),('response_encoded_scope',{'rows':'[{"BRANCHCODE":"0250"}]'}),
                          ('response_echoes_token',{'loan_access_token':'secret'}),('backend_failure',{'success':False})]:
            cases.append(dict(kind='wrapper',name=name,tool='loan-due-report',script='attach-loan-receipt.groovy',payload=body,attributes=postattrs))
        cases.append(dict(kind='wrapper',name='missing_scope',tool='loan-due-report',script='attach-loan-receipt.groovy',payload={'rows':[]},attributes={}))
        cases.append(dict(kind='wrapper',name='backend_error',tool='loan-due-report',script='backend-error.groovy',payload={'internal':'sql detail'},attributes=postattrs))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        case_path = Path(temporary.name)/'cases.json'
        case_path.write_text(json.dumps(cases))
        r=subprocess.run(['java','-cp',os.environ['NIFI_GROOVY_CLASSPATH'],'groovy.ui.GroovyMain',str(ROOT/'agentic_ai/tests/nifi/run-fixtures.groovy'),
                          str(ROOT/'deploy/nifi/signed-loan'),str(case_path)],env={**os.environ,'LOAN_ACCESS_SIGNING_KEY':KEY},capture_output=True,text=True)
        assert r.returncode==0,r.stderr
        results={v['name']:v for v in json.loads(r.stdout)}
        for name in ['valid','unicode','float']:assert results[name]['ok'],results[name]
        for c in cases:
            if c['kind']=='core' and c['name'] not in ['valid','unicode','float']:assert not results[c['name']]['ok'],c['name']
        pre=results['request_wrapper'];assert pre['relation']=='success',pre
        assert pre['attributes']['auth.branch_codes']=='["0003"]'
        assert pre['attributes']['http.context.identifier']=='request-123'
        assert 'loan_access_token' not in pre['body']
        assert results['request_rejected']['relation']=='failure'
        assert 'auth.branch_codes' not in results['request_rejected']['attributes']
        assert results['response_wrapper']['relation']=='success',results['response_wrapper']
        assert results['response_wrapper']['body']['authorization']==receipt
        from agentic_ai.loan_access import LoanGrant, validate_response
        validate_response({'structuredContent':results['response_wrapper']['body']}, LoanGrant(payload()['loan_access_token'], receipt, {}))
        for name in ['response_outside_scope','response_numeric_scope','response_encoded_scope','response_echoes_token','backend_failure','missing_scope']:
            assert results[name]['relation']=='failure',name
            assert 'rows' not in results[name]['body'],name
        assert results['backend_error']['attributes']['http.status.code']=='503'
        assert 'internal' not in results['backend_error']['body']
        self.assertEqual(len(results), len(cases))
