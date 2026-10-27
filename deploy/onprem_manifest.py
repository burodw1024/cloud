#!/usr/bin/env python3
"""Manifest/preflight helper for onprem-release.sh. Never print credentials."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def read(path):
    return json.loads(path.read_text())


def prepare(stage):
    root = Path.cwd().resolve()
    source = Path(os.environ['ENV_FILE']).resolve()
    require(not source.is_relative_to(root), 'Keep ENV_FILE outside the Git repository.')
    require(source.stat().st_mode & 0o077 == 0, 'Protect ENV_FILE with chmod 600.')
    values = {}
    for line in source.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        require(sep and re.fullmatch(r'[A-Z][A-Z0-9_]*', key), 'Use unquoted KEY=value lines, without export, in ENV_FILE.')
        require(key not in values, 'Duplicate environment setting: ' + key)
        values[key] = value
    needed = ('DJANGO_SECRET_KEY', 'DJANGO_ALLOWED_HOSTS')
    for key in needed:
        require(values.get(key) and not values[key].startswith('REPLACE_'), 'Configure ' + key + ' in ENV_FILE.')
    require(len(values['DJANGO_SECRET_KEY']) >= 50 and not values['DJANGO_SECRET_KEY'].startswith('django-insecure-'), 'Use a strong production DJANGO_SECRET_KEY.')
    url = urlsplit(os.environ['PUBLIC_URL'])
    require(url.scheme == 'https' and url.hostname and url.path in ('', '/') and not url.query and not url.fragment and not url.username,
            'PUBLIC_URL must be an HTTPS origin without credentials or a path.')
    hosts = [host.strip() for host in values['DJANGO_ALLOWED_HOSTS'].split(',')]
    require('*' not in hosts and url.hostname in hosts, 'Include the exact PUBLIC_URL hostname in DJANGO_ALLOWED_HOSTS; do not use *.')
    require(values.get('DJANGO_SETTINGS_MODULE', 'buro_first.settings_production') == 'buro_first.settings_production', 'Use the production Django settings profile.')
    require(values.get('DJANGO_DATA_DIR', '/data') == '/data', 'Use DJANGO_DATA_DIR=/data for the persistent volume.')
    values['DJANGO_DATA_DIR'] = '/data'
    values['DJANGO_SETTINGS_MODULE'] = 'buro_first.settings_production'
    origins = [x.strip() for x in values.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',') if x.strip()]
    origin = url.scheme + '://' + url.netloc
    if origin not in origins:
        origins.append(origin)
    values['DJANGO_CSRF_TRUSTED_ORIGINS'] = ','.join(origins)
    values.setdefault('OLLAMA_URL', 'http://192.168.1.144:31134')
    values.setdefault('MCP_GATEWAY_URL', 'http://192.168.1.99:30444/mcp')
    values.setdefault('MCP_GATEWAY_ENABLED', 'false')
    values.setdefault('MCP_TIMEOUT', '1500')
    values.setdefault('MCP_REQUEST_TIMEOUT', '1800')
    bootstrap = {k: v for k, v in values.items() if k.startswith('DJANGO_SUPERUSER_')}
    require(not any(value.startswith('REPLACE_') for value in bootstrap.values()), 'Replace bootstrap administrator placeholders in ENV_FILE.')
    app = {k: v for k, v in values.items() if not k.startswith('DJANGO_SUPERUSER_')}
    for name, data in [('app.env', app), ('bootstrap.env', bootstrap)]:
        # An empty optional bootstrap Secret is valid.
        (stage / name).write_text(''.join(f'{k}={v}\n' for k, v in data.items()))
    (stage / 'public.json').write_text(json.dumps({'host': url.netloc}))


def render(stage):
    env = os.environ
    name, ns = env['APP_NAME'], env['NAMESPACE']
    image, nginx = env['APP_IMAGE'], env['NGINX_IMAGE']
    pull_policy = 'Always' if env.get('IMAGE_DELIVERY', 'registry') == 'registry' else 'Never'
    pvc, release, job = env['MEDIA_PVC'], env['RELEASE_ID'], env['JOB']
    data_volume = {'name': 'media', 'hostPath': {'path': '/opt/buro-ai/' + name, 'type': 'DirectoryOrCreate'}} if env.get('STORAGE_MODE') == 'hostpath' else {'name': 'media', 'persistentVolumeClaim': {'claimName': pvc}}
    labels = {'app.kubernetes.io/name': name, 'app.kubernetes.io/managed-by': 'buro-onprem-release'}
    host = read(stage / 'public.json')['host']
    affinity = {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': [
        {'matchFields': [{'key': 'metadata.name', 'operator': 'In', 'values': [env['WORKER_NODE']]}]}]}}}
    pod_security = {'runAsNonRoot': True, 'fsGroup': 10001, 'fsGroupChangePolicy': 'OnRootMismatch', 'seccompProfile': {'type': 'RuntimeDefault'}}
    security = {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}, 'runAsUser': 10001, 'runAsGroup': 10001}
    env_from = [{'secretRef': {'name': name + '-env'}}]
    def obj(kind, object_name, **rest):
        return {'apiVersion': 'v1' if kind in ('Service', 'ConfigMap', 'PersistentVolumeClaim') else 'batch/v1' if kind == 'Job' else 'apps/v1',
                'kind': kind, 'metadata': {'name': object_name, 'namespace': ns, 'labels': labels}, **rest}
    def dump(filename, objects):
        (stage / filename).write_text(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': objects}, indent=2))
    def app_container(container_name, command):
        return {'name': container_name, 'image': image, 'imagePullPolicy': pull_policy, 'command': command,
                'envFrom': env_from, 'securityContext': security,
                'resources': {'requests': {'cpu': '250m', 'memory': '512Mi'}, 'limits': {'cpu': '2', 'memory': '2Gi'}}}
    def pod(containers, volumes, init=None):
        value = {'affinity': affinity, 'securityContext': pod_security, 'automountServiceAccountToken': False,
                 'containers': containers, 'volumes': volumes, 'terminationGracePeriodSeconds': 45}
        if env.get('IMAGE_PULL_SECRET'):
            value['imagePullSecrets'] = [{'name': env['IMAGE_PULL_SECRET']}]
        if init:
            value['initContainers'] = init
        return value
    proxy = '''worker_processes 1;
error_log /dev/stderr warn;
pid /tmp/nginx.pid;
events { worker_connections 1024; }
http {
  include /etc/nginx/mime.types;
  access_log /dev/stdout;
  client_body_temp_path /tmp/client_body;
  proxy_temp_path /tmp/proxy;
  fastcgi_temp_path /tmp/fastcgi;
  uwsgi_temp_path /tmp/uwsgi;
  scgi_temp_path /tmp/scgi;
  server {
    listen 8443 ssl;
    ssl_certificate /tls/tls.crt;
    ssl_certificate_key /tls/tls.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 50m;
    location = /nginx-live/ { access_log off; return 200 "ok"; }
    location /static/ { alias /static/; expires 1h; }
    location /media/ { return 404; }
    location / {
      proxy_pass http://127.0.0.1:8000;
      proxy_http_version 1.1;
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Forwarded-For $remote_addr;
      proxy_connect_timeout 5s;
      proxy_read_timeout 1900s;
      proxy_send_timeout 1900s;
    }
  }
}
'''
    common = [obj('ConfigMap', name + '-nginx', data={'nginx.conf': proxy})]
    if env['CREATE_PVC'] == '1' and env.get('STORAGE_MODE') != 'hostpath':
        spec = {'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': env['STORAGE_SIZE']}}}
        if env.get('STORAGE_CLASS'):
            spec['storageClassName'] = env['STORAGE_CLASS']
        common.append(obj('PersistentVolumeClaim', pvc, spec=spec))
    dump('storage-and-config.json', common)
    backup_code = """import pathlib, sqlite3
source = pathlib.Path('/data/db.sqlite3')
target = pathlib.Path('/data/deployment-backups')
target.mkdir(parents=True, exist_ok=True)
if source.exists():
    with sqlite3.connect(str(source), timeout=20) as src, sqlite3.connect(str(target / 'pre-RELEASE.sqlite3')) as dst:
        src.backup(dst)
    print('SQLite backup completed.')
else:
    print('First deployment: no existing SQLite database to back up.')
""".replace('RELEASE', release)
    backup = app_container('database-backup', ['python', '-c', backup_code])
    backup['volumeMounts'] = [{'name': 'media', 'mountPath': '/data'}, {'name': 'backup-tmp', 'mountPath': '/tmp'}]
    bootstrap_code = '''import os
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
User=get_user_model()
if not User.objects.filter(is_superuser=True,is_active=True).exists():
    name=os.environ.get('DJANGO_SUPERUSER_USERNAME','')
    password=os.environ.get('DJANGO_SUPERUSER_PASSWORD','')
    if not name or not password:
        raise SystemExit('First deployment requires DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD in the private env file.')
    user=User(username=name,email=os.environ.get('DJANGO_SUPERUSER_EMAIL',''),is_superuser=True,is_staff=True)
    validate_password(password,user)
    user.set_password(password)
    user.full_clean(exclude=['password'])
    user.save()
    print('Initial administrator created.')
else:
    print('Existing administrator preserved.')
'''
    # A single release lock serializes this pipeline. Do not retry a failed migration job.
    command = 'set -eu\npython manage.py check --deploy --fail-level ERROR\npython manage.py migrate --noinput\npython manage.py shell -c ' + __import__('shlex').quote(bootstrap_code)
    if env.get('CHECK_MCP') == '1':
        command += '\npython manage.py list_mcp_tools --check-model'
    migration = app_container('migrate', ['/bin/sh', '-ec', command])
    migration['envFrom'] = env_from + [{'secretRef': {'name': env['BOOTSTRAP_SECRET']}}]
    migration['volumeMounts'] = [{'name': 'media', 'mountPath': '/data'}, {'name': 'app-tmp', 'mountPath': '/tmp'}]
    job_pod = pod([migration], [data_volume,
                               {'name': 'app-tmp', 'emptyDir': {}}, {'name': 'backup-tmp', 'emptyDir': {}}], [backup])
    if env.get('STORAGE_MODE') == 'hostpath':
        prepare_data = app_container('prepare-django-data', ['python', '-c',
            "import os; os.chmod('/data',0o770); os.chown('/data',10001,10001)"])
        prepare_data.pop('envFrom')
        prepare_data['securityContext'] = {**security, 'runAsNonRoot': False, 'runAsUser': 0, 'runAsGroup': 0,
            'capabilities': {'drop': ['ALL'], 'add': ['CHOWN', 'FOWNER']}}
        prepare_data['volumeMounts'] = [{'name': 'media', 'mountPath': '/data'}]
        job_pod['initContainers'].insert(0, prepare_data)
    job_pod['restartPolicy'] = 'Never'
    dump('migration-job.json', [obj('Job', job, spec={'backoffLimit': 0, 'activeDeadlineSeconds': 900, 'ttlSecondsAfterFinished': 86400,
                            'template': {'metadata': {'labels': labels}, 'spec': job_pod}})])
    collect = app_container('collect-static', ['python', 'manage.py', 'collectstatic', '--noinput'])
    collect['volumeMounts'] = [{'name': 'media', 'mountPath': '/data'}, {'name': 'static', 'mountPath': '/app/staticfiles'}, {'name': 'app-tmp', 'mountPath': '/tmp'}]
    app = app_container('django', ['gunicorn', 'buro_first.wsgi:application', '--bind', '127.0.0.1:8000', '--workers', '1',
                                    '--timeout', '1860', '--access-logfile', '-', '--error-logfile', '-'])
    app['volumeMounts'] = [{'name': 'media', 'mountPath': '/data'}, {'name': 'app-tmp', 'mountPath': '/tmp'}]
    def app_probe(path, period, failures):
        code = ("import urllib.request; "
                f"r=urllib.request.Request('http://127.0.0.1:8000{path}', headers={{'Host': {host!r}, 'X-Forwarded-Proto': 'https'}}); "
                "response=urllib.request.urlopen(r, timeout=8); assert response.status == 200")
        return {'exec': {'command': ['python', '-c', code]}, 'periodSeconds': period,
                'failureThreshold': failures, 'timeoutSeconds': 10}
    app['startupProbe'] = app_probe('/healthz/live/', 5, 60)
    app['readinessProbe'] = app_probe('/healthz/ready/', 10, 3)
    app['livenessProbe'] = app_probe('/healthz/live/', 20, 3)

    def probe(path, period, failures):
        return {'httpGet': {'path': path, 'port': 8443, 'scheme': 'HTTPS', 'httpHeaders': [{'name': 'Host', 'value': host}]},
                'periodSeconds': period, 'failureThreshold': failures, 'timeoutSeconds': 10}
    web = {'name': 'https', 'image': nginx, 'imagePullPolicy': pull_policy, 'command': ['nginx', '-g', 'daemon off;'],
           'ports': [{'name': 'https', 'containerPort': 8443}],
           'securityContext': {**security, 'runAsUser': 101, 'runAsGroup': 101},
           'resources': {'requests': {'cpu': '50m', 'memory': '32Mi'}, 'limits': {'cpu': '500m', 'memory': '128Mi'}},
           'volumeMounts': [{'name': 'static', 'mountPath': '/static', 'readOnly': True}, {'name': 'tls', 'mountPath': '/tls', 'readOnly': True},
                            {'name': 'nginx-config', 'mountPath': '/etc/nginx/nginx.conf', 'subPath': 'nginx.conf', 'readOnly': True},
                            {'name': 'nginx-tmp', 'mountPath': '/tmp'}],
           'startupProbe': probe('/nginx-live/', 5, 60), 'readinessProbe': probe('/nginx-live/', 10, 3),
           'livenessProbe': probe('/nginx-live/', 20, 3)}
    volumes = [data_volume, {'name': 'static', 'emptyDir': {}},
               {'name': 'app-tmp', 'emptyDir': {}}, {'name': 'nginx-tmp', 'emptyDir': {}},
               {'name': 'tls', 'secret': {'secretName': env['TLS_SECRET'], 'defaultMode': 0o440}},
               {'name': 'nginx-config', 'configMap': {'name': name + '-nginx'}}]
    deployment = obj('Deployment', name, spec={'replicas': 1, 'revisionHistoryLimit': 5, 'progressDeadlineSeconds': 600,
        'selector': {'matchLabels': {'app.kubernetes.io/name': name}},
        'strategy': {'type': 'Recreate'},
        'template': {'metadata': {'labels': labels}, 'spec': pod([app, web], volumes, [collect])}})
    service = obj('Service', name, spec={'type': 'NodePort', 'externalTrafficPolicy': 'Cluster',
        'selector': {'app.kubernetes.io/name': name}, 'ports': [{'name': 'https', 'port': 443, 'targetPort': 8443, 'nodePort': int(env['APP_NODE_PORT'])}]})
    dump('application.json', [deployment, service])


def discover_registry(stage):
    """Resolve exactly one published registry Service, never its temporary pod IP."""
    namespace = os.environ.get('REGISTRY_NAMESPACE', 'registry')
    pods = [pod for pod in read(stage / 'registry-pods.json')['items']
            if 'registry' in pod['metadata'].get('name', '').lower()
            and pod.get('status', {}).get('phase') == 'Running'
            and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in pod.get('status', {}).get('conditions', []))]
    nodes = {node['metadata']['name']: node for node in read(stage / 'nodes.json')['items']}
    candidates = {}
    for service in read(stage / 'services.json')['items']:
        if service['metadata'].get('namespace') != namespace:
            continue
        selector = service.get('spec', {}).get('selector', {})
        if not selector:
            continue
        matches = [pod for pod in pods if all(pod['metadata'].get('labels', {}).get(key) == value for key, value in selector.items())]
        if not matches:
            continue
        ports = service['spec'].get('ports', [])
        usable = [port for port in ports if port.get('protocol', 'TCP') == 'TCP'
                  and (len(ports) == 1 or port.get('port') == 5000 or 'registry' in port.get('name', '').lower())]
        for port in usable:
            ingress = service.get('status', {}).get('loadBalancer', {}).get('ingress', [])
            published = sorted({entry.get('ip') or entry.get('hostname') for entry in ingress
                                if (entry.get('ip') or entry.get('hostname'))
                                and ':' not in (entry.get('ip') or entry.get('hostname'))})
            if published:
                candidates[(service['metadata']['name'], port['port'])] = f"{published[0]}:{port['port']}"
                continue
            if not port.get('nodePort'):
                continue
            # An endpoint node works with both Cluster and Local external traffic policy.
            addresses = sorted({address['address'] for pod in matches
                                for address in nodes.get(pod.get('spec', {}).get('nodeName'), {}).get('status', {}).get('addresses', [])
                                if address['type'] == 'InternalIP' and ':' not in address['address']})
            if addresses:
                candidates[(service['metadata']['name'], port['nodePort'])] = f"{addresses[0]}:{port['nodePort']}"
    require(len(candidates) == 1,
            'Cannot identify exactly one published registry Service. Run kubectl -n registry get svc -o wide and set REGISTRY=HOST:PORT explicitly; do not use a pod IP or Ollama port.')
    return next(iter(candidates.values()))


def validate_registry(address):
    require(bool(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*(?::[0-9]{1,5})?', address)),
            'REGISTRY must be HOST[:PORT], without http(s), paths or credentials.')
    require(not address.endswith(':31134'), '31134 is reserved for the confirmed Ollama endpoint; use the registry Service port.')
    if ':' in address:
        require(1 <= int(address.rsplit(':', 1)[1]) <= 65535, 'Invalid registry port.')


def main():
    action = sys.argv[1]
    if action == 'label':
        item = json.load(sys.stdin)
        item['metadata'].setdefault('labels', {})['app.kubernetes.io/managed-by'] = 'buro-onprem-release'
        json.dump(item, sys.stdout)
        return
    stage = Path(sys.argv[2])
    if action == 'prepare':
        prepare(stage)
    elif action == 'render':
        render(stage)
    elif action == 'node':
        nodes = [n for n in read(stage / 'nodes.json')['items'] if any(a['type'] == 'InternalIP' and a['address'] == os.environ['WORKER_IP'] for a in n['status']['addresses'])]
        require(len(nodes) == 1, 'WORKER_IP must match exactly one Kubernetes node.')
        n = nodes[0]
        require(any(c['type'] == 'Ready' and c['status'] == 'True' for c in n['status']['conditions']) and not n['spec'].get('unschedulable'), 'Worker must be Ready and schedulable.')
        info = n['status']['nodeInfo']
        require(info['operatingSystem'] == 'linux' and info['architecture'] in ('amd64', 'arm64'), 'Worker must be Linux amd64/arm64.')
        (stage / 'node.txt').write_text(n['metadata']['name'] + ' ' + info['architecture'] + '\n')
    elif action == 'registry':
        address = discover_registry(stage)
        validate_registry(address)
        (stage / 'registry.txt').write_text(address + '\n')
    elif action == 'registry-address':
        validate_registry(os.environ['REGISTRY'])
    elif action == 'ports':
        for service in read(stage / 'services.json')['items']:
            for port in service['spec'].get('ports', []):
                if port.get('nodePort') == int(os.environ['APP_NODE_PORT']):
                    require(service['metadata']['name'] == os.environ['APP_NAME'] and service['metadata']['namespace'] == os.environ['NAMESPACE'], 'NodePort is occupied by another Service; choose a free APP_NODE_PORT.')
    elif action == 'ownership':
        content = (stage / 'resource.json').read_text().strip()
        if content:
            require(json.loads(content)['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') == 'buro-onprem-release', 'Existing resource is not managed by this script. Choose a new APP_NAME or review an explicit adoption plan.')
    elif action == 'storage':
        require(any(s['metadata'].get('annotations', {}).get('storageclass.kubernetes.io/is-default-class') == 'true' for s in read(stage / 'storage.json')['items']), 'No default StorageClass. Set STORAGE_CLASS or MEDIA_PVC to an existing claim.')
    elif action == 'pvc':
        require('ReadWriteOncePod' not in read(stage / 'pvc.json')['spec']['accessModes'], 'The backup Job and application require a PVC that permits multiple pods on the same worker; ReadWriteOncePod cannot be shared.')
    elif action == 'staged':
        paths = subprocess.check_output(['git', 'diff', '--cached', '--name-only', '-z']).decode().split('\0')
        for name in filter(None, paths):
            filename = Path(name).name.lower()
            require(filename.endswith('.example') or not (filename.startswith('.env') or filename.endswith(('.pem', '.key', '.crt', '.sqlite3')) or filename in ('production.env', 'app.env', 'bootstrap.env')),
                    'Potential private configuration staged: ' + name + '. Remove it from the Git index before release.')
    else:
        raise SystemExit('Unknown action')


if __name__ == '__main__':
    main()
