import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stage=Path(self.tmp.name)
        self.script=Path(__file__).resolve().parents[2]/'deploy/release_manifest.py'
        self.project=self.script.parents[1]
        self.env={**os.environ,'RELEASE_ID':'20261019000000-test','APP_IMAGE':'registry/app:test',
                  'NGINX_IMAGE':'registry/nginx:test','MIGRATION_JOB':'release-test'}
        mounts=[{'name':'data','mountPath':'/data'},{'name':'gateway','mountPath':'/app/agentic_ai/mcp_gateway.py','subPath':'mcp_gateway.py'}]
        django={'name':'django','image':'old','envFrom':[{'secretRef':{'name':'existing-env'}}],
                'env':[{'name':'KEEP_ME','valueFrom':{'secretKeyRef':{'name':'existing','key':'token'}}}],
                'volumeMounts':mounts,'readinessProbe':{'exec':{'command':['true']}}}
        web={'name':'https','image':'old-nginx','volumeMounts':[{'name':'nginx','mountPath':'/etc/nginx/nginx.conf','subPath':'nginx.conf'}]}
        spec={'containers':[django,web],'initContainers':[{'name':'collect-static','image':'old','volumeMounts':[{'name':'data','mountPath':'/data'}]}],
              'volumes':[{'name':'data','hostPath':{'path':'/opt/buro-ai/buro-django','type':'Directory'}},
                         {'name':'gateway','configMap':{'name':'buro-django-router-fix'}},
                         {'name':'nginx','configMap':{'name':'existing-nginx'}}]}
        self.dep={'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'buro-django','namespace':'rag','resourceVersion':'99','uid':'deployment-uid'},
                  'spec':{'replicas':1,'selector':{'matchLabels':{'app':'django'}},'template':{'metadata':{'labels':{'app':'django'}},'spec':spec}}}
        self.write('deployment.before.json',self.dep)
        self.write('node.json',{'metadata':{'name':'worker-99'},'status':{'nodeInfo':{'architecture':'amd64'},'conditions':[{'type':'Ready','status':'True'}]}})
        self.write('replicasets.before.json',{'items':[{'metadata':{'uid':'replica-uid','ownerReferences':[{'kind':'Deployment','uid':'deployment-uid','controller':True}]}}]})
        self.write('pods.before.json',{'items':[{'metadata':{'name':'existing-pod','ownerReferences':[{'kind':'ReplicaSet','uid':'replica-uid','controller':True}]},'spec':{'nodeName':'worker-99'},'status':{'phase':'Running','containerStatuses':[{'ready':True},{'ready':True}]}}]})
        self.write('nginx.before.json',{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'existing-nginx','namespace':'rag'},'data':{
            'nginx.conf':'location / { proxy_pass http://localhost; proxy_read_timeout 130s; proxy_send_timeout 130s; }'}})

    def write(self,name,value): (self.stage/name).write_text(json.dumps(value))
    def run_helper(self,action):
        return subprocess.run([sys.executable,str(self.script),action,str(self.stage)],cwd=self.project,env=self.env,capture_output=True,text=True)

    def test_preserves_storage_and_secret_refs_and_removes_overlay(self):
        self.assertEqual(self.run_helper('validate').returncode,0)
        result=self.run_helper('render')
        self.assertEqual(result.returncode,0,result.stderr)
        after=json.loads((self.stage/'deployment.after.json').read_text())
        spec=after['spec']['template']['spec']
        self.assertEqual(after['spec']['strategy'],{'type':'Recreate'})
        self.assertEqual(after['spec']['replicas'],1)
        self.assertEqual(spec['nodeName'],'worker-99')
        self.assertNotIn('resourceVersion',after['metadata'])
        app=next(c for c in spec['containers'] if c['name']=='django')
        self.assertEqual(app['envFrom'],self.dep['spec']['template']['spec']['containers'][0]['envFrom'])
        self.assertIn(self.dep['spec']['template']['spec']['containers'][0]['env'][0],app['env'])
        self.assertEqual(app['imagePullPolicy'],'Never')
        self.assertIn('gthread',app['command'])
        self.assertNotIn('gateway',{v['name'] for v in spec['volumes']})
        self.assertEqual(next(v for v in spec['volumes'] if v['name']=='data')['hostPath']['path'],'/opt/buro-ai/buro-django')
        job=json.loads((self.stage/'migration.json').read_text())
        self.assertEqual(job['spec']['backoffLimit'],0)
        self.assertNotIn('initContainers',job['spec']['template']['spec'])
        self.assertEqual(job['spec']['template']['spec']['containers'][0]['imagePullPolicy'],'Never')

    def test_unexpected_init_image_fails_before_rollout(self):
        self.dep['spec']['template']['spec']['initContainers'].append({'name':'unexpected','image':'unknown'})
        self.write('deployment.before.json',self.dep)
        self.assertNotEqual(self.run_helper('validate').returncode,0)

    def test_missing_proxy_directive_fails_before_rollout(self):
        self.assertEqual(self.run_helper('validate').returncode,0)
        self.write('nginx.before.json',{'metadata':{'name':'existing-nginx'},'data':{'nginx.conf':'proxy_pass http://localhost;'}})
        self.assertNotEqual(self.run_helper('render').returncode,0)

    def test_completed_job_pods_do_not_count_as_application_pods(self):
        pods=json.loads((self.stage/'pods.before.json').read_text())
        for phase in ('Succeeded','Failed','Running'):
            pods['items'].append({'metadata':{'name':'job-'+phase,'ownerReferences':[{'kind':'Job','uid':'job-uid','controller':True}]},
                                  'spec':{'nodeName':'worker-99'},'status':{'phase':phase}})
        self.write('pods.before.json',pods)
        result=self.run_helper('validate')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.stage/'pod-name').read_text(),'existing-pod')

    def test_wrong_node_is_reported_without_automatically_moving_data(self):
        pods=json.loads((self.stage/'pods.before.json').read_text())
        pods['items'][0]['spec']['nodeName']='worker-other'
        self.write('pods.before.json',pods)
        result=self.run_helper('validate')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('different node',result.stderr)
        self.assertIn('worker-other',result.stderr)

    def test_drain_counts_terminating_app_but_ignores_terminal_jobs(self):
        pods=json.loads((self.stage/'pods.before.json').read_text())
        pods['items'][0]['metadata']['deletionTimestamp']='2026-10-19T00:00:00Z'
        pods['items'].append({'metadata':{'name':'completed-job','ownerReferences':[{'kind':'Job','uid':'job-uid','controller':True}]},
                              'spec':{'nodeName':'worker-99'},'status':{'phase':'Succeeded'}})
        self.write('draining-pods.json',pods)
        helper=self.project/'deploy/pod_ownership.py'
        result=subprocess.run([sys.executable,str(helper),'drain-count',str(self.stage)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'1')
        pods['items']=pods['items'][1:]
        self.write('draining-pods.json',pods)
        result=subprocess.run([sys.executable,str(helper),'drain-count',str(self.stage)],capture_output=True,text=True)
        self.assertEqual(result.stdout.strip(),'0')

    def test_replica_owned_by_another_deployment_is_excluded(self):
        replicas=json.loads((self.stage/'replicasets.before.json').read_text())
        replicas['items'][0]['metadata']['ownerReferences'][0]['uid']='different-deployment'
        self.write('replicasets.before.json',replicas)
        result=self.run_helper('validate')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('found: none',result.stderr)
