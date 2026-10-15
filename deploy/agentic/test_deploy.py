import json,os,shutil,subprocess,tempfile
from pathlib import Path
source=Path(__file__).resolve().parents[2]
fixture={'spec':{'template':{'metadata':{'labels':{'app':'cloud-live-test'}},'spec':{
 'containers':[{'name':'django','image':'192.168.1.224:80/cloud:ui-existing','imagePullPolicy':'IfNotPresent','volumeMounts':[
 {'name':'dashboard-mcp-code','mountPath':'/app/agentic_ai/mcp_runtime.py','subPath':'mcp_runtime.py'},
 {'name':'dashboard-nifi-fix','mountPath':'/app/agentic_ai/tools/nifi_tool.py','subPath':'nifi_tool.py'},
 {'name':'agentic-live-code','mountPath':'/app/agentic_ai/planner.py','subPath':'planner.py'},
 {'name':'dashboard-mcp-code','mountPath':'/app/agentic_ai/orchestrator.py','subPath':'orchestrator.py'},
 {'name':'dashboard-mcp-deps','mountPath':'/opt/dashboard-mcp-deps'},
 {'name':'test-data','mountPath':'/data'}]}],
 'volumes':[{'name':'dashboard-mcp-code','configMap':{'name':'dashboard-mcp-display-20261013120053'}},
 {'name':'dashboard-nifi-fix','configMap':{'name':'dashboard-nifi-url-20261013075216'}},
 {'name':'agentic-live-code','configMap':{'name':'cloud-live-test-agentic-20261013073130'}},
 {'name':'test-data','persistentVolumeClaim':{'claimName':'test-data'}}]
}}}}
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp); repo=root/'repo'; bins=root/'bin'; bins.mkdir(); (repo/'deploy/agentic').mkdir(parents=True)
 for f in ['deploy/agentic/deploy.sh','deploy/agentic/files.txt',*(source/'deploy/agentic/files.txt').read_text().splitlines()]:
  target=repo/f;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source/f,target)
 stub='''#!/usr/bin/env python3
import sys,os,json
from pathlib import Path
args=sys.argv[1:]; root=Path(os.environ['CHECK_ROOT'])
if Path(sys.argv[0]).name=='docker':
 with (root/'docker.log').open('a') as out: out.write(json.dumps(args)+'\\n')
 sys.exit(0)
if 'get' in args:
 d=json.loads((root/'fixture.json').read_text())
 if any(a.startswith('jsonpath=') for a in args): print(d['spec']['template']['spec']['containers'][0]['image'])
 else: print(json.dumps(d))
elif 'patch' in args:
 p=Path(args[args.index('--patch-file')+1])
 with (root/'patches.jsonl').open('a') as out: out.write(p.read_text()+'\\n')
elif 'rollout' in args:
 count=root/'rollout.count'; n=int(count.read_text()) if count.exists() else 0;count.write_text(str(n+1))
 if os.environ.get('FAIL_FIRST')=='1' and n==0: sys.exit(1)
else: raise SystemExit('Unexpected command: '+str(args))
'''
 for name in ['kubectl','docker']:
  p=bins/name;p.write_text(stub);p.chmod(0o755)
 env={**os.environ,'PATH':str(bins)+':'+os.environ['PATH'],'CHECK_ROOT':str(root)}
 for fail in [False,True]:
  (root/'fixture.json').write_text(json.dumps(fixture))
  for name in ['patches.jsonl','rollout.count','docker.log']:(root/name).unlink(missing_ok=True)
  result=subprocess.run(['bash',str(repo/'deploy/agentic/deploy.sh')],env={**env,'FAIL_FIRST':'1' if fail else '0'},capture_output=True,text=True)
  assert result.returncode==(1 if fail else 0),result.stderr
  patches=[json.loads(l) for l in (root/'patches.jsonl').read_text().splitlines()]
  c=patches[0]['spec']['template']['spec']['containers'][0]
  assert c['volumeMounts']==[{'mountPath':'/app/agentic_ai/orchestrator.py','$patch':'delete'}],c
  assert ':agentic-' in c['image']
  if fail:
   rollback=patches[1]['spec']['template']['spec']['containers'][0]
   assert rollback['image']=='192.168.1.224:80/cloud:ui-existing'
   assert rollback['volumeMounts']==[fixture['spec']['template']['spec']['containers'][0]['volumeMounts'][3]]
   assert (root/'rollout.count').read_text()=='2'
  else: assert len(patches)==1
 # Unknown source mount must still fail before any docker call or deployment patch.
 fixture['spec']['template']['spec']['containers'][0]['volumeMounts'].append({'name':'unexpected','mountPath':'/app'})
 (root/'fixture.json').write_text(json.dumps(fixture));(root/'docker.log').unlink();(root/'patches.jsonl').unlink()
 result=subprocess.run(['bash',str(repo/'deploy/agentic/deploy.sh')],env=env,capture_output=True,text=True)
 assert result.returncode!=0 and 'hide this release: /app' in result.stderr,result.stderr
 assert not (root/'docker.log').exists() and not (root/'patches.jsonl').exists()
print('PASS: atomic image+orchestrator replacement, rollback restores mount/image, all other mounts untouched, unknown source mounts blocked before build.')
