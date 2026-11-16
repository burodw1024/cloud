"""Initialize the confirmed empty Superset DB using the node's imported image."""
import argparse
import copy
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import re
import subprocess
import time


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def build_job(deployment, name, script):
    pod = copy.deepcopy(deployment['spec']['template']['spec'])
    require(pod.get('nodeSelector', {}).get('kubernetes.io/hostname') == 'superset', 'Wrong deployment node')
    require(not pod.get('initContainers'), 'Unexpected init containers; use the prepared driver image')
    require(len(pod['containers']) == 1 and pod['containers'][0]['name'] == 'superset', 'Unexpected containers')
    container = pod['containers'][0]
    require(container['image'].startswith('192.168.1.224:80/superset:'), 'Expected the imported local-registry image')
    require(any(source.get('secretRef', {}).get('name') == 'superset-external-db-secrets'
                for source in container.get('envFrom', [])), 'Expected the existing external database Secret')
    container['name'] = 'initialize'
    container['imagePullPolicy'] = 'Never'
    container['command'] = ['python', '-u', '-c', script]
    for field in ('args', 'startupProbe', 'readinessProbe', 'livenessProbe', 'ports', 'lifecycle'):
        container.pop(field, None)
    container['envFrom'].append({'secretRef': {'name': name}})
    pod['restartPolicy'] = 'Never'
    return {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name, 'namespace': 'superset-onprem'},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 1200,
                     'template': {'metadata': {'labels': {'app': 'superset-metadata-init'}}, 'spec': pod}}}


def monitoring_recovery(lock, deployment, original, logs):
    data = lock.get('data', {})
    name = data.get('job', '')
    require(re.fullmatch(r'superset-metadata-init-[0-9]{14}', name), 'Unexpected original initialization Job name')
    require(not data.get('previous_job'), 'A recovery was already dispatched; inspect its Job')
    require(data.get('deployment_uid') == deployment['metadata']['uid'], 'Initialization deployment identity changed')
    require(deployment['spec'].get('replicas') == 0, 'Frontend must still be stopped')
    require(original['metadata']['name'] == name, 'Original Job identity mismatch')
    status = original.get('status', {})
    require(status.get('failed', 0) > 0 and not status.get('active'), 'Original Job must be failed/inactive')
    expected = 'RuntimeError: Database is not empty. No migrations or account resets were run.'
    require(expected in logs.splitlines() and 'Verified empty metadata database;' not in logs,
            'Recovery requires the confirmed pre-migration object-guard failure')
    old = original['spec']['template']['spec']['containers'][0]
    current = deployment['spec']['template']['spec']['containers'][0]
    require(old['image'] == current['image'], 'Prepared image changed; inspect before recovery')
    require(old.get('envFrom') and old['envFrom'][-1].get('secretRef', {}).get('name') == name,
            'Original bootstrap credentials are not identified')
    return name+'-views', name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', default='mamun')
    parser.add_argument('--resume-monitoring-views', action='store_true', help='Recover only the confirmed object-guard failure before any migrations.')
    args = parser.parse_args()
    command = ['kubectl', '--context', args.context, '-n', 'superset-onprem']
    def get(kind, name):
        return json.loads(subprocess.check_output(command+['get', kind, name, '-o', 'json'], text=True))
    def run(*argv):
        subprocess.run(command+list(argv), check=True)
    def create(resource):
        subprocess.run(command+['create', '-f', '-'], input=json.dumps(resource), text=True, check=True)
    deployment = get('deployment', 'superset')
    require(not deployment.get('status', {}).get('readyReplicas'), 'Superset is already Ready; do not initialize its database')
    name = 'superset-metadata-init-'+datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    script = Path(__file__).with_name('bootstrap_metadata.py').read_text()
    job = build_job(deployment, name, script)
    prior = subprocess.check_output(command+['get', 'configmap', 'superset-metadata-bootstrap-lock',
                                             '--ignore-not-found', '-o', 'name'], text=True).strip()
    if args.resume_monitoring_views:
        require(prior, 'No failed initialization lock was found')
        lock = get('configmap', 'superset-metadata-bootstrap-lock')
        original_name = lock['data']['job']
        original = get('job', original_name)
        logs = subprocess.check_output(command+['logs', 'job/'+original_name], text=True)
        name, admin_secret = monitoring_recovery(lock, deployment, original, logs)
        credentials = get('secret', admin_secret)
        require(all(credentials.get('data', {}).get(key) for key in
                    ('BOOTSTRAP_ADMIN_USERNAME', 'BOOTSTRAP_ADMIN_PASSWORD', 'BOOTSTRAP_ADMIN_EMAIL')),
                'Original web-admin credentials are unavailable')
        username = 'the username entered for the original initialization'
        active = json.loads(subprocess.check_output(command+['get', 'pods', '-l', 'app=superset', '-o', 'json'], text=True))
        require(not any(p.get('status', {}).get('phase') not in ('Succeeded', 'Failed') for p in active['items']),
                'Frontend pods must be fully stopped before recovery')
        job = build_job(deployment, name, script)
        job['spec']['template']['spec']['containers'][0]['envFrom'][-1] = {'secretRef': {'name': admin_secret}}
        existing = subprocess.check_output(command+['get', 'job', name, '--ignore-not-found', '-o', 'name'], text=True).strip()
        require(not existing, 'Recovery Job already exists; inspect its logs')
        run('patch', 'configmap', 'superset-metadata-bootstrap-lock', '--type=merge', '-p',
            json.dumps({'data': {'job': name, 'previous_job': original_name}}))
    else:
        require(not prior, 'Initialization lock exists. Inspect the recorded Job instead of repeating initialization.')
        username = input('New Superset web-admin username [admin]: ').strip() or 'admin'
        require(re.fullmatch(r'[A-Za-z0-9_.@-]{1,150}', username), 'Invalid username')
        password = getpass.getpass('New Superset web-admin password: ')
        require(password and password == getpass.getpass('Confirm web-admin password: '), 'Passwords are empty or do not match')
        admin_secret = name
        create({'apiVersion': 'v1', 'kind': 'ConfigMap',
                'metadata': {'name': 'superset-metadata-bootstrap-lock', 'namespace': 'superset-onprem'},
                'data': {'job': name, 'deployment_uid': deployment['metadata']['uid']}})
        # A failed attempt retains this lock, Job and temporary Secret for inspection.
        create({'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                'metadata': {'name': name, 'namespace': 'superset-onprem'}, 'stringData': {
                    'BOOTSTRAP_ADMIN_USERNAME': username, 'BOOTSTRAP_ADMIN_PASSWORD': password,
                    'BOOTSTRAP_ADMIN_EMAIL': 'admin@buro.local'}})
        password = None
        run('scale', 'deployment/superset', '--replicas=0')
        pods = json.loads(subprocess.check_output(command+['get', 'pods', '-l', 'app=superset', '-o', 'json'], text=True))
        for pod in pods['items']:
            if pod.get('status', {}).get('phase') not in ('Succeeded', 'Failed'):
                run('wait', '--for=delete', 'pod/'+pod['metadata']['name'], '--timeout=180s')
    require(get('deployment', 'superset')['metadata']['uid'] == deployment['metadata']['uid'], 'Deployment identity changed')
    create(job)
    print('Initialization Job:', name, flush=True)
    deadline = time.monotonic()+1250
    while True:
        status = get('job', name).get('status', {})
        if status.get('succeeded'):
            break
        if status.get('failed') or any(c['type'] == 'Failed' and c['status'] == 'True' for c in status.get('conditions', [])):
            run('logs', 'job/'+name, '--tail=100')
            raise RuntimeError('Initialization failed. Lock retained; inspect logs before retrying or running migrations.')
        require(time.monotonic() < deadline, 'Initialization timed out; lock retained. Inspect the existing Job.')
        time.sleep(5)
    run('logs', 'job/'+name, '--tail=30')
    current = get('deployment', 'superset')
    require(current['metadata']['uid'] == deployment['metadata']['uid'] and current['spec'].get('replicas') == 0,
            'Deployment changed during initialization; inspect before starting it')
    run('scale', 'deployment/superset', '--replicas=1')
    run('rollout', 'status', 'deployment/superset', '--timeout=1200s')
    lock = get('configmap', 'superset-metadata-bootstrap-lock')
    require(lock['data'].get('job') == name, 'Initialization lock ownership changed')
    run('delete', 'secret', admin_secret)
    run('delete', 'configmap', 'superset-metadata-bootstrap-lock')
    print('Superset initialized and Ready: http://192.168.1.176:32086')
    print('Web username:', username, '(use the web-admin password you entered, not the PostgreSQL password)')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
