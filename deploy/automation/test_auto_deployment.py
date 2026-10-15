"""Run with python3; all Docker, Kubernetes, and SSH operations are simulated."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2]
TEMPLATE = {
    'metadata': {'labels': {'app': 'cloud-live-test'}},
    'spec': {
        'nodeSelector': {'kubernetes.io/hostname': 'worker-99'},
        'initContainers': [{'name': 'dashboard-mcp-dependencies', 'image': 'old-init-image'}],
        'containers': [{'name': 'django', 'image': 'registry/cloud:previous', 'imagePullPolicy': 'Never',
                        'env': [{'name': 'MCP_BEARER_TOKEN', 'valueFrom': {'secretKeyRef': {'name': 'contextforge-auth', 'key': 'MCP_BEARER_TOKEN'}}}],
                        'volumeMounts': [
                            {'name': 'dashboard-mcp-code', 'mountPath': '/app/agentic_ai/orchestrator.py', 'subPath': 'orchestrator.py'},
                            {'name': 'agentic-live-code', 'mountPath': '/app/agentic_ai/planner.py', 'subPath': 'planner.py'},
                            {'name': 'dashboard-nifi-fix', 'mountPath': '/app/agentic_ai/tools/nifi_tool.py', 'subPath': 'nifi_tool.py'},
                            {'name': 'test-data', 'mountPath': '/data'},
                        ]}],
        'volumes': [
            {'name': 'dashboard-mcp-code', 'configMap': {'name': 'dashboard-mcp-display-20261013120053'}},
            {'name': 'agentic-live-code', 'configMap': {'name': 'cloud-live-test-agentic-20261013073130'}},
            {'name': 'dashboard-nifi-fix', 'configMap': {'name': 'dashboard-nifi-url-20261013075216'}},
            {'name': 'test-data', 'persistentVolumeClaim': {'claimName': 'test-data'}},
        ],
    },
}
STUB = r'''#!/usr/bin/env python3
import hashlib,json,os,shutil,subprocess,sys,tempfile
from pathlib import Path
root=Path(os.environ['CHECK_ROOT']); args=sys.argv[1:]; tool=Path(sys.argv[0]).name
with (root/'commands.jsonl').open('a') as out: out.write(json.dumps({'tool':tool,'args':args})+'\n')
mode=os.environ.get('CHECK_MODE','success')
if tool=='git':
 if args[:2]==['status','--porcelain']: pass
 elif args[0]=='pull': print('Already up to date.')
 elif args[:2]==['rev-parse','HEAD']: print('a'*40)
 elif args[:2]==['ls-files','-z']: sys.stdout.buffer.write((root/'tracked.txt').read_bytes())
 else: raise SystemExit('Unexpected git operation')
elif tool=='docker':
 if args[0]=='build':
  context=Path(args[-1])
  assert not (context/'.zypl.env').exists()
  assert not (context/'chat/.env').exists()
  assert not (context/'chat/private.key').exists()
  assert not (context/'.manager-state').exists()
  assert (context/'new_app/views.py').is_file()
  assert (context/'chat/templates/chat/index.html').is_file()
  if mode in {'full','replace-settings'}: assert (context/'buro_first/settings.py').is_file()
  else: assert not (context/'buro_first/settings.py').exists()
  if mode!='full': assert 'COPY --chown=' in (context/'Dockerfile').read_text()
  (root/'built').touch()
 elif args[0]=='push' and mode=='push-failure': sys.exit(1)
 elif args[0]=='save': Path(args[args.index('-o')+1]).write_bytes(b'test-image-archive')
elif tool=='kubectl':
 if 'get' in args:
  if 'node' in args:
   print(json.dumps({'metadata':{'labels':{'kubernetes.io/hostname':'worker-99'}},'status':{'nodeInfo':{'architecture':'amd64'},'conditions':[{'type':'Ready','status':'True'}]}}))
  elif any(a.startswith('jsonpath=') for a in args): print(json.loads((root/'deployment.json').read_text())['spec']['template']['spec']['containers'][0]['image'])
  else: print((root/'deployment.json').read_text())
 elif 'patch' in args:
  patches=json.loads(Path(args[args.index('--patch-file')+1]).read_text())
  d=json.loads((root/'deployment.json').read_text())
  if mode=='concurrent-change' and not (root/'patched').exists():
   d['spec']['template']['metadata']['labels']['concurrent']='change'
  assert patches[0]['op']=='test'
  if patches[0]['value']!=d['spec']['template']: sys.exit(1)
  d['spec']['template']=patches[1]['value']
  (root/'deployment.json').write_text(json.dumps(d));(root/'patched').touch()
  with (root/'patches.jsonl').open('a') as out: out.write(json.dumps(patches)+'\n')
 elif 'rollout' in args:
  p=root/'rollout.count';n=int(p.read_text()) if p.exists() else 0;p.write_text(str(n+1))
  if mode=='rollout-failure' and n==0: sys.exit(1)
 else: raise SystemExit('Unexpected kubectl operation')
elif tool=='sshpass':
 assert args[0]=='-f' and Path(args[1]).read_text().strip()=='test-secret'
 sys.exit(subprocess.call(args[2:]))
elif tool=='scp':
 target=args[-1].split(':',1)[1];shutil.copyfile(args[-2],target)
elif tool=='ssh':
 command=args[-1]
 password_mode=mode=='password'
 if command=='true': sys.exit(1 if password_mode else 0)
 elif command=='sudo -n true': sys.exit(1 if password_mode else 0)
 elif command.startswith('mktemp -d'):
  p=tempfile.mkdtemp(prefix='buro-auto.');(root/'remote-dir').write_text(p);print(p)
 elif command.startswith('sha256sum'):
  p=Path(command.split("'")[1]);print(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p))
 elif command.startswith('sudo '):
  if '-S' in command: assert sys.stdin.readline().strip()=='test-secret'
  if 'images import' in command:
   if mode=='import-failure':sys.exit(1)
   (root/'imported').touch()
 elif command.startswith('rm -f '):
  p=root/'remote-dir'
  if p.exists():shutil.rmtree(p.read_text(),ignore_errors=True)
 else: raise SystemExit('Unexpected SSH command: '+command)
'''


class AutoDeploymentTests(unittest.TestCase):
    def run_script(self, mode='success', unknown_mount=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root/'repo'; bins = root/'bin'
            repo.mkdir(); bins.mkdir()
            shutil.copyfile(SOURCE/'auto-deployment.sh', repo/'auto-deployment.sh')
            paths = ['Dockerfile', 'requirements.docker.txt', 'manage.py',
                     'chat/templates/chat/index.html', 'chat/.env', 'chat/private.key', '.zypl.env',
                     'buro_first/settings.py', 'agentic_ai/orchestrator.py', 'agentic_ai/planner.py',
                     'agentic_ai/tools/nifi_tool.py', 'new_app/views.py',
                     '.manager-state/should-not-be-packaged.json']
            for name in paths:
                p = repo/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('fixture')
            (root/'tracked.txt').write_bytes(('\0'.join(paths)+'\0').encode())
            original = json.loads(json.dumps(TEMPLATE))
            if unknown_mount:
                original['spec']['containers'][0]['volumeMounts'].append({'name': 'unknown', 'mountPath': '/app/chat'})
            (root/'deployment.json').write_text(json.dumps({'spec': {'template': original}}))
            for name in ['git', 'docker', 'kubectl', 'ssh', 'scp', 'sshpass']:
                p=bins/name;p.write_text(STUB);p.chmod(0o755)
            env={**os.environ,'PATH':str(bins)+':'+os.environ['PATH'],'CHECK_ROOT':str(root),
                 'CHECK_MODE':mode,'AUTO_DEPLOY_PASSWORD':'test-secret'}
            env.pop('AUTO_DEPLOY_SOURCE_READY', None)
            env['AUTO_DEPLOY_FULL_BUILD']='1' if mode=='full' else '0'
            env['AUTO_DEPLOY_REPLACE_SETTINGS']='1' if mode=='replace-settings' else '0'
            result=subprocess.run(['sh', str(repo/'auto-deployment.sh')],env=env,capture_output=True,text=True)
            final=json.loads((root/'deployment.json').read_text())['spec']['template']
            commands=[json.loads(line) for line in (root/'commands.jsonl').read_text().splitlines()]
            self.assertNotIn('test-secret', result.stdout+result.stderr)
            self.assertNotIn('test-secret', json.dumps(commands))
            if (root/'remote-dir').exists(): self.assertFalse(Path((root/'remote-dir').read_text()).exists())
            return result, original, final, commands, (root/'built').exists(), (root/'patched').exists()

    def test_success_preserves_configuration_and_imports_before_patch(self):
        result, original, final, commands, built, patched = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(built and patched)
        container=final['spec']['containers'][0]
        self.assertIn('/cloud:auto-', container['image'])
        self.assertEqual(container['imagePullPolicy'],'IfNotPresent')
        self.assertEqual(container['env'], original['spec']['containers'][0]['env'])
        self.assertEqual(final['spec']['volumes'], original['spec']['volumes'])
        self.assertEqual(final['spec']['initContainers'], original['spec']['initContainers'])
        self.assertEqual(len(container['volumeMounts']),3)
        imported=next(i for i,c in enumerate(commands) if c['tool']=='ssh' and 'images import' in c['args'][-1])
        patched=next(i for i,c in enumerate(commands) if c['tool']=='kubectl' and 'patch' in c['args'])
        self.assertLess(imported,patched)

    def test_full_build_explicitly_includes_repository_settings(self):
        result, _, _, commands, built, _ = self.run_script('full')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(built)
        self.assertFalse(any(c['tool']=='docker' and c['args'][:2]==['image','inspect'] for c in commands))

    def test_settings_replacement_is_explicit(self):
        result, _, _, _, built, _ = self.run_script('replace-settings')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(built)

    def test_password_mode_uses_private_file_and_sudo_stdin(self):
        result, _, _, commands, _, _ = self.run_script('password')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(any(c['tool']=='sshpass' for c in commands))

    def test_rollout_failure_restores_entire_previous_template(self):
        result, original, final, _, _, patched = self.run_script('rollout-failure')
        self.assertNotEqual(result.returncode,0)
        self.assertTrue(patched,result.stdout+result.stderr)
        self.assertEqual(final,original)

    def test_push_failure_does_not_change_workload(self):
        result, original, final, _, _, patched = self.run_script('push-failure')
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(patched)
        self.assertEqual(final,original)

    def test_import_failure_does_not_change_workload(self):
        result, original, final, _, _, patched = self.run_script('import-failure')
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(patched)
        self.assertEqual(final,original)

    def test_unknown_source_mount_stops_before_build(self):
        result, _, _, _, built, patched = self.run_script(unknown_mount=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(built or patched)
        self.assertIn('Source mount would hide this release',result.stderr)

    def test_concurrent_change_refuses_deployment(self):
        result, _, _, _, _, patched = self.run_script('concurrent-change')
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(patched)


if __name__=='__main__':
    unittest.main()
