"""Resume the verified post-migration phase of a stopped, failed release.

Uses existing imported images and saved manifests. Never rebuilds, reruns migrate,
replaces a backup, deletes failed Jobs, or removes a lock before readiness passes.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def recovery_code(expected_sha):
    return '''import hashlib, json, os, sqlite3, subprocess, sys
from pathlib import Path
os.chdir('/app')
sys.path.insert(0, '/app')
root = Path('/data')
release = os.environ['AGENT_RELEASE_ID']
backups = root/'deployment-backups'/release
backup = backups/'db.sqlite3'
assert backup.is_file(), 'Verified pre-migration backup is missing'
with sqlite3.connect('file:'+str(backup)+'?mode=ro', uri=True) as database:
    assert database.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
script_text = Path('/app/deploy/migrate_release.py').read_text()
assert hashlib.sha256(script_text.encode()).hexdigest() == EXPECTED_SHA, 'Imported image does not match the saved source'
marker = '\\nimport django\\ndjango.setup()\\n'
assert script_text.count(marker) == 1, 'Unrecognized post-migration entrypoint'
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
executor = MigrationExecutor(connection)
assert not executor.migration_plan(executor.loader.graph.leaf_nodes()), 'Pending migrations found; recovery will not run them'
assert not (backups/'migration-complete.json').exists(), 'Post-migration phase already marked complete; inspect before retrying'
print('Existing backup verified; no pending migrations. Resuming post-migration checks only.', flush=True)
post_migration = 'import django\\ndjango.setup()\\n' + script_text.split(marker, 1)[1]
exec(compile(post_migration, '/app/deploy/migrate_release.py:post-migration', 'exec'), globals())
'''.replace('EXPECTED_SHA', repr(expected_sha))


def build_recovery(saved, expected_sha):
    job = copy.deepcopy(saved)
    original = job['metadata']['name']
    job['metadata'] = {'name': original+'-resume', 'namespace': saved['metadata']['namespace']}
    job.pop('status', None)
    spec = job['spec']
    spec.update(backoffLimit=0, activeDeadlineSeconds=1200)
    pod = spec['template']['spec']
    require(len(pod['containers']) == 1, 'Unexpected migration container count')
    container = pod['containers'][0]
    require(container.get('imagePullPolicy') == 'Never', 'Recovery must use the imported image')
    require(container['command'] == ['python', '/app/deploy/migrate_release.py'], 'This helper only supports the reported absolute-script failure')
    container['command'] = ['python', '-c', recovery_code(expected_sha)]
    container.pop('args', None)
    container['workingDir'] = '/app'
    spec['template']['metadata'] = {'labels': {'agentic-release-job': job['metadata']['name']}}
    return job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', type=Path)
    parser.add_argument('--context', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--cert', type=Path, default=Path.home()/'.config/agentic/tls/tls.crt')
    args = parser.parse_args()
    stage = args.stage.resolve()
    def read(name): return json.loads((stage/name).read_text())
    saved = read('migration.json')
    before = read('deployment.before.json')
    after = read('deployment.after.json')
    namespace = saved['metadata']['namespace']
    name = after['metadata']['name']
    original_job = saved['metadata']['name']
    env = saved['spec']['template']['spec']['containers'][0].get('env', [])
    release = next(value['value'] for value in env if value['name'] == 'AGENT_RELEASE_ID')
    lock_name = name+'-release-lock'
    command = ['kubectl', '--context', args.context, '-n', namespace]
    def get(kind, resource=None, selector=None):
        argv = command+['get', kind]+([resource] if resource else [])
        if selector: argv += ['-l', selector]
        return json.loads(subprocess.check_output(argv+['-o', 'json'], text=True))
    def run(*argv): subprocess.run(command+list(argv), check=True)
    current = get('deployment', name)
    require(current['metadata']['uid'] == before['metadata']['uid'], 'Deployment identity changed')
    require(current['spec'].get('replicas') == 0, 'Application must still be stopped; this helper will not stop an active deployment')
    original = get('job', original_job)
    require(original.get('status', {}).get('failed', 0) > 0 and not original.get('status', {}).get('active', 0),
            'Original migration Job is not in the expected failed/inactive state')
    status_path = stage/'migration.status.json'
    if status_path.exists():
        require(original['metadata']['uid'] == read('migration.status.json')['metadata']['uid'], 'Original Job identity changed')
    lock = get('configmap', lock_name)
    require(lock.get('data', {}).get('release') == release, 'Release lock belongs to another release')
    require(lock['data'].get('job') == original_job, 'Lock does not match the failed Job')
    all_pods = get('pods')
    for pod in all_pods['items']:
        if pod.get('status', {}).get('phase') in ('Succeeded', 'Failed'):
            continue
        volumes = pod['spec'].get('volumes', [])
        if any(v.get('hostPath', {}).get('path') == '/opt/buro-ai/'+name for v in volumes):
            raise RuntimeError('An active pod still mounts the database: '+pod['metadata']['name'])
    require(args.cert.is_file(), 'Public TLS certificate is missing')
    require(args.url.startswith('https://'), 'HTTPS URL required')
    source = stage.parent/'project/deploy/migrate_release.py'
    require(source.is_file(), 'Saved build source is missing')
    recovery = build_recovery(saved, hashlib.sha256(source.read_bytes()).hexdigest())
    recovery_name = recovery['metadata']['name']
    require(len(recovery_name) <= 63, 'Recovery Job name is too long')
    prior = subprocess.check_output(command+['get','job',recovery_name,'--ignore-not-found','-o','json'], text=True).strip()
    require(not prior, 'A recovery Job already exists; inspect its logs instead of rerunning it')
    path = stage/'post-migration-recovery.json'
    path.write_text(json.dumps(recovery, indent=2))
    run('apply', '--dry-run=server', '-f', str(path))
    run('patch', 'configmap', lock_name, '--type=merge', '-p', json.dumps({'data': {'recovery_job': recovery_name}}))
    run('create', '-f', str(path))
    deadline = time.monotonic()+1250
    while True:
        status = get('job', recovery_name).get('status', {})
        if status.get('succeeded', 0):
            break
        if status.get('failed', 0) or any(c['type']=='Failed' and c['status']=='True' for c in status.get('conditions', [])):
            run('logs', 'job/'+recovery_name, '-c', 'migrate', '--tail=100')
            raise RuntimeError('Recovery checks failed. Lock and backups retained; do not rerun blindly.')
        require(time.monotonic() < deadline, 'Recovery timed out; inspect the existing Job and retain the lock')
        time.sleep(5)
    run('logs', 'job/'+recovery_name, '-c', 'migrate', '--tail=30')
    require(get('deployment', name)['spec'].get('replicas') == 0, 'Deployment changed during recovery')
    run('apply', '-f', str(stage/'nginx.after.json'))
    run('apply', '-f', str(stage/'deployment.after.json'))
    run('rollout', 'status', 'deployment/'+name, '--timeout=900s')
    from pod_ownership import deployment_pods
    pods = deployment_pods(current, get('replicasets'), get('pods'))
    require(len(pods) == 1, 'Expected exactly one application pod after recovery')
    pod = pods[0]
    statuses = pod.get('status', {}).get('containerStatuses', [])
    require(len(statuses)==2 and all(c.get('ready') and c.get('restartCount')==0 for c in statuses), 'Recovered pod is not 2/2 ready with zero restarts')
    require(next(c['image'] for c in pod['spec']['containers'] if c['name']=='django') ==
            saved['spec']['template']['spec']['containers'][0]['image'], 'Unexpected running image')
    pod_name = pod['metadata']['name']
    # Module execution fixes import lookup in the already imported image as well.
    run('exec', pod_name, '-c', 'django', '--', 'python', '-c',
        "import runpy,sys; sys.path.insert(0,'/app'); runpy.run_module('deploy.runtime_verify',run_name='__main__')")
    expected_gateway = after['spec']['template']['metadata']['annotations']['agentic.buro/gateway-sha256']
    actual_gateway = subprocess.check_output(command+['exec',pod_name,'-c','django','--','python','-c',
        "import hashlib; print(hashlib.sha256(open('/app/agentic_ai/mcp_gateway.py','rb').read()).hexdigest())"], text=True).strip()
    require(actual_gateway == expected_gateway, 'Running gateway code does not match the saved image source')
    run('exec', pod_name, '-c', 'https', '--', 'nginx', '-t')
    import re
    nginx = subprocess.check_output(command+['exec',pod_name,'-c','https','--','cat','/etc/nginx/nginx.conf'], text=True)
    for directive in ('proxy_read_timeout','proxy_send_timeout'):
        values = re.findall(r'\b'+directive+r'\s+([^;]+);', nginx)
        require(values and all(value.strip()=='670s' for value in values), 'Running Nginx timeout mismatch')
    subprocess.run(['curl','--fail','--silent','--show-error','--cacert',str(args.cert),
                    '--connect-timeout','5','--max-time','30',args.url.rstrip('/')+'/healthz/ready/'], check=True)
    lock = get('configmap', lock_name)
    require(lock['data'].get('release')==release and lock['data'].get('recovery_job')==recovery_name, 'Lock ownership changed; not removing it')
    run('delete', 'configmap', lock_name)
    run('get', 'pod', pod_name, '-o', 'wide')
    print('\nRecovery deployment verified:', args.url)
    print('Original backup, failed Job and recovery Job were retained.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
