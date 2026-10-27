"""Render a replacement from the live spec; never regenerate production Secrets."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from pod_ownership import staged_pods, describe

stage = Path(sys.argv[2])
release = os.environ['RELEASE_ID']

def require(ok, message):
    if not ok:
        raise SystemExit(message)

def read(name):
    return json.loads((stage/name).read_text())

def write(name, value):
    (stage/name).write_text(json.dumps(value, indent=2))

def environment(container, updates):
    container['env'] = [x for x in container.get('env', []) if x['name'] not in updates]
    container['env'].extend({'name': k, 'value': v} for k, v in updates.items())

def clean_resource(resource):
    result = copy.deepcopy(resource)
    result.pop('status', None)
    result['metadata'] = {k:v for k,v in result['metadata'].items() if k in ('name','namespace','labels','annotations')}
    result['metadata'].get('annotations', {}).pop('kubectl.kubernetes.io/last-applied-configuration', None)
    return result

if sys.argv[1] == 'validate':
    dep = read('deployment.before.json')
    spec = dep['spec']['template']['spec']
    require(dep['spec'].get('replicas',1) == 1, 'Expected exactly one existing replica. Resolve scaling before release.')
    containers = {c['name']:c for c in spec['containers']}
    require(set(containers) == {'django','https'}, 'Unexpected application containers; refusing to replace an unknown spec.')
    require({c['name'] for c in spec.get('initContainers',[])}=={'collect-static'}, 'Unexpected init containers; every runtime image must be explicitly imported.')
    node = read('node.json')
    require(node['metadata']['name']=='worker-99' and node['status']['nodeInfo']['architecture']=='amd64', 'Expected worker-99/linux-amd64.')
    require(any(c['type']=='Ready' and c['status']=='True' for c in node['status']['conditions']), 'Worker is not Ready.')
    require(not node.get('spec',{}).get('unschedulable'), 'Worker is not schedulable.')
    pods = staged_pods(stage)
    require(len(pods)==1, 'Expected one active Deployment-owned Django pod; found: ' + describe(pods))
    require(pods[0]['spec'].get('nodeName')=='worker-99', 'Django is on a different node. Refusing to move HostPath data automatically: ' + describe(pods))
    require(len(pods[0]['status'].get('containerStatuses',[]))==2 and all(c.get('ready') for c in pods[0]['status'].get('containerStatuses',[])), 'Existing deployment is not healthy; investigate before release.')
    host = [v for v in spec['volumes'] if v.get('hostPath',{}).get('path')=='/opt/buro-ai/buro-django']
    require(len(host)==1, 'Existing persistent HostPath does not match the confirmed data directory.')
    require(any(m['name']==host[0]['name'] and m['mountPath']=='/data' for m in containers['django'].get('volumeMounts',[])), 'Django /data is not mounted from the confirmed HostPath.')
    for c in spec['containers'] + spec.get('initContainers',[]):
        for mount in c.get('volumeMounts',[]):
            path = mount['mountPath'].rstrip('/')
            require(not path in ('/app','/app/agentic_ai','/app/chat'), 'A directory overlay would hide the new code; review it before release.')
    nginx_mount = [m for m in containers['https']['volumeMounts'] if m['mountPath']=='/etc/nginx/nginx.conf']
    require(len(nginx_mount)==1, 'Cannot identify the existing Nginx config mount.')
    volume = next(v for v in spec['volumes'] if v['name']==nginx_mount[0]['name'])
    require('configMap' in volume, 'Nginx must use its existing ConfigMap.')
    (stage/'nginx-name').write_text(volume['configMap']['name'])
    (stage/'pod-name').write_text(pods[0]['metadata']['name'])
    selector = dep['spec']['selector'].get('matchLabels',{})
    require(selector and not dep['spec']['selector'].get('matchExpressions'), 'Unsupported deployment selector.')
    (stage/'selector').write_text(','.join(k+'='+v for k,v in selector.items()))
    print('Live node, persistent storage, containers and deployment validated.')

elif sys.argv[1] == 'render':
    dep = clean_resource(read('deployment.before.json'))
    dep['spec'].update(replicas=1, strategy={'type':'Recreate'}, progressDeadlineSeconds=900)
    spec=dep['spec']['template']['spec']
    spec['nodeName']='worker-99'
    spec['terminationGracePeriodSeconds']=1920
    containers={c['name']:c for c in spec['containers']}
    django=containers['django']; https=containers['https']
    updates={'DJANGO_SETTINGS_MODULE':'buro_first.settings_production','DJANGO_DATA_DIR':'/data',
             'POLICY_WORK_DIR':'/data/policy_rag','MCP_TIMEOUT':'1500','MCP_REQUEST_TIMEOUT':'1800',
             'AGENT_RELEASE_ID':release}
    for c in spec['containers']+spec.get('initContainers',[]):
        if c['name'] in ('django','collect-static'):
            c['image']=os.environ['APP_IMAGE'];c['imagePullPolicy']='Never'
            environment(c,updates)
        mounts=c.get('volumeMounts',[])
        c['volumeMounts']=[m for m in mounts if m['mountPath']!='/app/agentic_ai/mcp_gateway.py']
    used={m['name'] for c in spec['containers']+spec.get('initContainers',[]) for m in c.get('volumeMounts',[])}
    # Remove only unused volumes that provided the old gateway overlay.
    old_overlay_names={m['name'] for c in read('deployment.before.json')['spec']['template']['spec']['containers']
                       for m in c.get('volumeMounts',[]) if m['mountPath']=='/app/agentic_ai/mcp_gateway.py'}
    spec['volumes']=[v for v in spec['volumes'] if v['name'] not in old_overlay_names or v['name'] in used]
    django['command']=['gunicorn','buro_first.wsgi:application','--bind','127.0.0.1:8000',
                       '--workers','1','--worker-class','gthread','--threads','4','--timeout','1860',
                       '--graceful-timeout','1860','--access-logfile','-','--error-logfile','-']
    django.pop('args',None)
    https['image']=os.environ['NGINX_IMAGE'];https['imagePullPolicy']='Never'
    # Preserve probe commands while allowing enough response time under load.
    for name in ('startupProbe','readinessProbe','livenessProbe'):
        if name in django:
            django[name]['timeoutSeconds']=10
    dep['spec']['template']['metadata'].setdefault('annotations',{})['agentic.buro/release']=release
    dep['spec']['template']['metadata']['annotations']['agentic.buro/gateway-sha256']=hashlib.sha256(Path('agentic_ai/mcp_gateway.py').read_bytes()).hexdigest()
    cm=clean_resource(read('nginx.before.json'))
    cm['metadata']['name']='buro-nginx-'+release
    cm.pop('immutable',None)
    found=False
    for key,text in cm.get('data',{}).items():
        if 'proxy_pass' in text:
            for directive in ('proxy_read_timeout','proxy_send_timeout'):
                text,count=re.subn(r'\b'+directive+r'\s+[^;]+;',directive+' 1900s;',text)
                require(count>0,'Missing '+directive+'; cannot safely update this Nginx configuration.')
            cm['data'][key]=text;found=True
    require(found,'No proxy configuration found in the existing Nginx ConfigMap.')
    old_name=(stage/'nginx-name').read_text()
    for v in spec['volumes']:
        if v.get('configMap',{}).get('name')==old_name:
            v['configMap']['name']=cm['metadata']['name']
    write('nginx.after.json',cm)
    write('deployment.after.json',dep)
    migration=copy.deepcopy(django)
    for key in ('ports','startupProbe','readinessProbe','livenessProbe','lifecycle'):
        migration.pop(key,None)
    migration['name']='migrate'
    migration['command']=['python','/app/deploy/migrate_release.py']
    pod=copy.deepcopy(spec)
    pod['containers']=[migration];pod.pop('initContainers',None)
    pod['restartPolicy']='Never'
    pod['terminationGracePeriodSeconds']=60
    pod['volumes']=[v for v in pod['volumes'] if v['name'] in {m['name'] for m in migration.get('volumeMounts',[])}]
    job={'apiVersion':'batch/v1','kind':'Job','metadata':{'name':os.environ['MIGRATION_JOB'],'namespace':'rag'},
         'spec':{'backoffLimit':0,'activeDeadlineSeconds':2100,'ttlSecondsAfterFinished':604800,
                 'template':{'metadata':{'labels':{'agentic-release-job':os.environ['MIGRATION_JOB']}},'spec':pod}}}
    write('migration.json',job)
    print('Rendered one-replica Recreate deployment and stopped-writer migration Job.')
else:
    raise SystemExit('Unknown action')
