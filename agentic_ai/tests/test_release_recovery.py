import copy
import hashlib
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import unittest


class PostMigrationRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        cls.helper = runpy.run_path(str(root/'deploy/resume_release.py'))

    def fixture(self, base, pending=False):
        project = base/'app'
        (project/'deploy').mkdir(parents=True)
        for directory in ('django', 'django/db', 'django/db/migrations'):
            (project/directory).mkdir(exist_ok=True)
            (project/directory/'__init__.py').write_text('')
        (project/'django/__init__.py').write_text('def setup(): pass\n')
        (project/'django/db/__init__.py').write_text('connection = object()\n')
        (project/'django/db/migrations/executor.py').write_text('''
from types import SimpleNamespace
class MigrationExecutor:
    def __init__(self, connection):
        self.loader = SimpleNamespace(graph=SimpleNamespace(leaf_nodes=lambda: []))
    def migration_plan(self, targets):
        return PLAN
'''.replace('PLAN', '[1]' if pending else '[]'))
        data = base/'data'
        backups = data/'deployment-backups/recovery-test'
        backups.mkdir(parents=True)
        with sqlite3.connect(backups/'db.sqlite3') as database:
            database.execute('CREATE TABLE original_backup (id INTEGER)')
        checkpoint = base/'post-checkpoint'
        script = ("raise RuntimeError('backup/migrate phase must never run twice')\n"
                  "\nimport django\ndjango.setup()\n"
                  + f"Path({str(checkpoint)!r}).write_text('post-migration only')\n")
        (project/'deploy/migrate_release.py').write_text(script)
        code = self.helper['recovery_code'](hashlib.sha256(script.encode()).hexdigest())
        code = code.replace("'/app/deploy/migrate_release.py'", repr(str(project/'deploy/migrate_release.py')))
        code = code.replace("'/app'", repr(str(project))).replace("'/data'", repr(str(data)))
        return code, checkpoint, backups

    def execute(self, code, base):
        env = dict(os.environ, AGENT_RELEASE_ID='recovery-test')
        env.pop('PYTHONPATH', None)
        return subprocess.run([sys.executable, '-c', code], cwd=base, env=env,
                              capture_output=True, text=True)

    def test_recovery_runs_only_post_migration_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            code, checkpoint, backups = self.fixture(base)
            before = (backups/'db.sqlite3').read_bytes()
            result = self.execute(code, base)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(checkpoint.read_text(), 'post-migration only')
            self.assertEqual((backups/'db.sqlite3').read_bytes(), before)

    def test_pending_migrations_block_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            code, checkpoint, backups = self.fixture(base, pending=True)
            result = self.execute(code, base)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Pending migrations found', result.stderr)
            self.assertFalse(checkpoint.exists())

    def test_missing_backup_blocks_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            code, checkpoint, backups = self.fixture(base)
            (backups/'db.sqlite3').unlink()
            result = self.execute(code, base)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('backup is missing', result.stderr)
            self.assertFalse(checkpoint.exists())

    def test_recovery_job_preserves_existing_image_and_credentials(self):
        saved = {'metadata': {'name': 'original-release', 'namespace': 'test'}, 'spec': {
            'template': {'spec': {'containers': [{'name': 'migrate', 'image': 'registry/existing:immutable',
                'imagePullPolicy': 'Never', 'command': ['python', '/app/deploy/migrate_release.py'],
                'envFrom': [{'secretRef': {'name': 'existing-secret'}}]}]}}}}
        original = copy.deepcopy(saved)
        recovery = self.helper['build_recovery'](saved, 'a'*64)
        self.assertEqual(saved, original)
        container = recovery['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['image'], 'registry/existing:immutable')
        self.assertEqual(container['envFrom'], saved['spec']['template']['spec']['containers'][0]['envFrom'])
        self.assertEqual(container['command'][:2], ['python', '-c'])
        self.assertEqual(recovery['spec']['backoffLimit'], 0)

    def test_cluster_recovery_orders_checks_before_rollout_and_lock_removal(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            stage = base/'private'
            stage.mkdir()
            (base/'project/deploy').mkdir(parents=True)
            (base/'project/deploy/migrate_release.py').write_text('saved image source')
            cert = base/'cert.crt'
            cert.write_text('public test certificate')
            release = 'test-release'
            job_name = 'test-job'
            image = 'registry/app:existing'
            deployment = {'metadata': {'name':'test-app','uid':'deployment-uid'},'spec':{'replicas':0}}
            lock = {'data':{'release':release,'job':job_name}}
            saved = {'metadata':{'name':job_name,'namespace':'test'},'spec':{'template':{'spec':{'containers':[{
                'name':'migrate','image':image,'imagePullPolicy':'Never',
                'command':['python','/app/deploy/migrate_release.py'],
                'env':[{'name':'AGENT_RELEASE_ID','value':release}]}]}}}}
            after = {'metadata':{'name':'test-app'},'spec':{'template':{'metadata':{
                'annotations':{'agentic.buro/gateway-sha256':'a'*64}}}}}
            for name,data in {'migration.json':saved,'deployment.before.json':deployment,'deployment.after.json':after,
                              'migration.status.json':{'metadata':{'uid':'original-job-uid'}},'nginx.after.json':{}}.items():
                (stage/name).write_text(json.dumps(data))
            calls=[]
            state={'rolled_out':False,'checks_complete':False}
            def output(command, **kwargs):
                if 'exec' in command:
                    return 'a'*64 if 'django' in command else 'proxy_read_timeout 670s; proxy_send_timeout 670s;'
                kind=command[command.index('get')+1]
                if kind=='deployment': return json.dumps(deployment)
                if kind=='configmap': return json.dumps(lock)
                if kind=='job':
                    if '--ignore-not-found' in command: return ''
                    if job_name in command:
                        return json.dumps({'metadata':{'uid':'original-job-uid'},'status':{'failed':1}})
                    state['checks_complete']=True
                    return json.dumps({'status':{'succeeded':1}})
                if kind=='replicasets':
                    return json.dumps({'items':[{'metadata':{'uid':'replica-uid','ownerReferences':[
                        {'kind':'Deployment','uid':'deployment-uid','controller':True}]}}]})
                if kind=='pods':
                    pods=[]
                    if state['rolled_out']:
                        pods=[{'metadata':{'name':'new-pod','ownerReferences':[{'kind':'ReplicaSet','uid':'replica-uid','controller':True}]},
                               'spec':{'containers':[{'name':'django','image':image}]},
                               'status':{'phase':'Running','containerStatuses':[{'ready':True,'restartCount':0}]*2}}]
                    return json.dumps({'items':pods})
                raise AssertionError(command)
            def run(command, **kwargs):
                calls.append(command)
                if 'patch' in command:
                    lock['data'].update(json.loads(command[command.index('-p')+1])['data'])
                if 'apply' in command and str(stage/'deployment.after.json') in command:
                    self.assertTrue(state['checks_complete'])
                    state['rolled_out']=True
                if 'delete' in command:
                    self.assertTrue(state['rolled_out'])
                    self.assertTrue(any(c[0]=='curl' for c in calls))
                return SimpleNamespace(returncode=0)
            root=Path(__file__).resolve().parents[2]
            argv=['resume_release.py',str(stage),'--context','test-context','--url','https://app.example','--cert',str(cert)]
            with patch.object(sys,'argv',argv), patch.object(sys,'path',[str(root/'deploy'),*sys.path]), \
                 patch('subprocess.check_output',side_effect=output), patch('subprocess.run',side_effect=run):
                self.helper['main']()
            self.assertTrue(state['rolled_out'])
            self.assertEqual(sum('create' in c for c in calls),1)
            self.assertFalse(any('migrate' in c for c in calls if 'logs' not in c))
