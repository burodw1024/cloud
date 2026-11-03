#!/usr/bin/env python3
"""Install fresh Superset on the selected node using existing PostgreSQL HA.

Run on the on-premises Kubernetes administration machine. Dry-run is the default.
No PostgreSQL workload or volume is created, relocated or deleted.
"""
import argparse
from decimal import Decimal
import getpass
import json
import re
import secrets
import subprocess
import sys
from urllib.parse import quote

NAMESPACE = 'superset'
NODE = 'etlreg-virtual-machine'
PG_NAMESPACE = 'postgres-ha-test'
PG_SERVICE = 'acid-postgres-primary-nodeport'
PG_CLUSTER = 'acid-postgres-test'
APP = 'agentic-superset'
MANAGER = 'agentic-superset-installer'
META_USER = META_DB = 'agentic_superset'
ANALYTICS_DB = 'agentic_analytics'
WRITER = 'agentic_snapshot_writer'
READER = 'agentic_snapshot_reader'
LABELS = {'app.kubernetes.io/name': APP, 'app.kubernetes.io/managed-by': MANAGER}

CONFIG = '''import os
SECRET_KEY = os.environ['SUPERSET_SECRET_KEY']
SQLALCHEMY_DATABASE_URI = os.environ['SUPERSET_METADATA_URI']
SQLALCHEMY_TRACK_MODIFICATIONS = False
AUTH_USER_REGISTRATION = False
FEATURE_FLAGS = {'DASHBOARD_RBAC': True}
WTF_CSRF_ENABLED = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
DATA_CACHE_CONFIG = {'CACHE_TYPE': 'NullCache'}
CACHE_CONFIG = {'CACHE_TYPE': 'SimpleCache', 'CACHE_DEFAULT_TIMEOUT': 300}
ENABLE_PROXY_FIX = False
'''

BOOTSTRAP = '''import os
import subprocess
import psycopg2
from superset.app import create_app
subprocess.run(['superset', 'db', 'upgrade'], check=True)
subprocess.run(['superset', 'init'], check=True)
app = create_app()
with app.app_context():
    from superset import db, security_manager
    from superset.models.core import Database
    username = os.environ['SUPERSET_ADMIN_USERNAME']
    user = security_manager.find_user(username=username)
    if not user:
        user = security_manager.add_user(username=username,
            first_name='Platform', last_name='Administrator',
            email=os.environ['SUPERSET_ADMIN_EMAIL'],
            role=security_manager.find_role('Admin'),
            password=os.environ['SUPERSET_ADMIN_PASSWORD'])
        if not user:
            raise RuntimeError('Administrator creation failed')
    uri = os.environ['AGENTIC_ANALYTICS_READ_URI']
    with psycopg2.connect(uri.replace('postgresql+psycopg2:', 'postgresql:')) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT current_user, pg_is_in_recovery()')
            _, recovery = cur.fetchone()
            if recovery:
                raise RuntimeError('Primary service routes to a standby')
    dataset_db = db.session.query(Database).filter_by(database_name='Agentic snapshots').one_or_none()
    if not dataset_db:
        dataset_db = Database(database_name='Agentic snapshots', sqlalchemy_uri=uri,
            expose_in_sqllab=False, allow_run_async=False, allow_dml=False,
            allow_ctas=False, allow_cvas=False, allow_file_upload=False)
        db.session.add(dataset_db)
        db.session.commit()
    print('Analytics database ID:', dataset_db.id)
    print('Administrator Superset user ID:', user.id)
    print('Superset initialization complete')
'''


def quantity(value):
    value = str(value)
    for suffix, factor in [('Ki', 1024), ('Mi', 1024**2), ('Gi', 1024**3),
                           ('Ti', 1024**4), ('m', Decimal('.001')),
                           ('u', Decimal('.000001')), ('n', Decimal('.000000001')),
                           ('K', 1000), ('M', 1000**2), ('G', 1000**3)]:
        if value.endswith(suffix):
            return Decimal(value[:-len(suffix)]) * factor
    return Decimal(value)


def pod_request(pod, resource):
    spec = pod['spec']
    def request(container):
        return quantity(container.get('resources', {}).get('requests', {}).get(resource, 0))
    app = sum((request(c) for c in spec.get('containers', [])), Decimal(0))
    sidecars = peak = Decimal(0)
    for container in spec.get('initContainers', []):
        if container.get('restartPolicy') == 'Always':
            sidecars += request(container)
            peak = max(peak, sidecars)
        else:
            peak = max(peak, sidecars + request(container))
    return max(app + sidecars, peak) + quantity(spec.get('overhead', {}).get(resource, 0))


def metadata(name):
    return {'name': name, 'namespace': NAMESPACE, 'labels': LABELS.copy()}


def selects_new_pods(resource):
    """Prevent older services/controllers from adopting or exposing the new pod."""
    selector = resource.get('spec', {}).get('selector', {})
    if not selector:
        return False
    if resource.get('kind') == 'Service':
        return all(LABELS.get(key) == value for key, value in selector.items())
    labels = selector.get('matchLabels', {})
    expressions = selector.get('matchExpressions', [])
    if not labels and not expressions:
        return False
    if any(LABELS.get(key) != value for key, value in labels.items()):
        return False
    for expression in expressions:
        key, operator = expression['key'], expression['operator']
        values = expression.get('values', [])
        if operator == 'In' and LABELS.get(key) not in values:
            return False
        if operator == 'NotIn' and LABELS.get(key) in values:
            return False
        if operator == 'Exists' and key not in LABELS:
            return False
        if operator == 'DoesNotExist' and key in LABELS:
            return False
    return True


def manifests(image, pull_secret=None):
    config = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': metadata(APP + '-config'),
              'data': {'superset_config.py': CONFIG, 'bootstrap.py': BOOTSTRAP}}
    pod = {'automountServiceAccountToken': False,
        'affinity': {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {
            'nodeSelectorTerms': [{'matchFields': [{'key': 'metadata.name', 'operator': 'In', 'values': [NODE]}]}]}}},
        'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000,
                            'seccompProfile': {'type': 'RuntimeDefault'}},
        'volumes': [{'name': 'config', 'configMap': {'name': APP + '-config'}}],
        'containers': [{'name': 'superset', 'image': image, 'imagePullPolicy': 'IfNotPresent',
            'env': [{'name': 'SUPERSET_CONFIG_PATH', 'value': '/etc/superset/superset_config.py'}],
            'envFrom': [{'secretRef': {'name': APP + '-runtime'}}],
            'volumeMounts': [{'name': 'config', 'mountPath': '/etc/superset', 'readOnly': True}],
            'resources': {'requests': {'cpu': '1', 'memory': '2Gi'}, 'limits': {'cpu': '2', 'memory': '4Gi'}},
            'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}}}]}
    if pull_secret:
        pod['imagePullSecrets'] = [{'name': pull_secret}]
    job_pod = json.loads(json.dumps(pod))
    job_pod['restartPolicy'] = 'Never'
    job_pod['containers'][0]['command'] = ['python', '/etc/superset/bootstrap.py']
    job_pod['containers'][0]['envFrom'].append({'secretRef': {'name': APP + '-bootstrap'}})
    job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': metadata(APP + '-init'),
           'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 900,
                    'template': {'metadata': {'labels': LABELS}, 'spec': job_pod}}}
    web = pod['containers'][0]
    web['command'] = ['gunicorn', '--bind', '0.0.0.0:8088', '--workers', '1',
                      '--worker-class', 'gthread', '--threads', '4', '--timeout', '180',
                      'superset.app:create_app()']
    web['ports'] = [{'name': 'http', 'containerPort': 8088}]
    for key, period, failures in [('startupProbe', 10, 60), ('readinessProbe', 10, 3), ('livenessProbe', 30, 3)]:
        web[key] = {'httpGet': {'path': '/health', 'port': 'http'},
                    'periodSeconds': period, 'timeoutSeconds': 5, 'failureThreshold': failures}
    deployment = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': metadata(APP),
        'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'},
            'selector': {'matchLabels': {'app.kubernetes.io/name': APP}},
            'template': {'metadata': {'labels': LABELS}, 'spec': pod}}}
    service = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': metadata(APP),
        'spec': {'type': 'ClusterIP', 'selector': {'app.kubernetes.io/name': APP},
                 'ports': [{'name': 'http', 'port': 8088, 'targetPort': 'http'}]}}
    return config, job, deployment, service


class Cluster:
    def __init__(self, context):
        self.command = ['kubectl'] + (['--context', context] if context else [])

    def run(self, *args, data=None, timeout=60):
        try:
            result = subprocess.run(self.command + list(args), input=data, text=True,
                                    capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            if 'psql' in args:
                phase = 'database provisioning' if data else 'read-only PostgreSQL preflight'
                raise RuntimeError(f'Timed out during {phase} after {timeout} seconds. '
                    'Check Kubernetes exec connectivity and PostgreSQL local administrator access. '
                    'No interactive database password prompt is used.') from None
            raise RuntimeError('Kubernetes operation timed out: ' + ' '.join(args[:5])) from None
        if result.returncode:
            # Never print raw SQL/Secret data or provider error response bodies.
            if 'psql' in args:
                error = (result.stderr or '').lower()
                if any(message in error for message in ('no password supplied', 'password authentication failed', 'peer authentication failed')):
                    raise RuntimeError('PostgreSQL administrator authentication was rejected. '
                        'Check local access for postgres in the primary pod; no interactive password was requested.')
                if any(message in error for message in ('unable to upgrade connection', 'error dialing backend', 'tls handshake timeout', 'i/o timeout')):
                    raise RuntimeError('Kubernetes exec could not reach the PostgreSQL pod. '
                        'Check the API server connection to the primary node kubelet.')
            raise RuntimeError('Kubernetes operation failed: ' + ' '.join(args[:5]) + '. Check cluster logs locally.')
        return result.stdout

    def get(self, kind, name=None, namespace=None, extra=()):
        args = (['-n', namespace] if namespace else []) + ['get', kind]
        args += [name] if name else []
        args += list(extra) + ['-o', 'json', '--request-timeout=20s']
        return json.loads(self.run(*args))

    def create(self, document):
        self.run('create', '-f', '-', data=json.dumps(document))

    def apply(self, document):
        self.run('apply', '-f', '-', data=json.dumps(document))


def secret_document(name, values):
    return {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': metadata(name),
            'type': 'Opaque', 'stringData': values}


def sql_for_accounts(passwords):
    # All identifiers are fixed in source; passwords are generated hexadecimal.
    for password in passwords.values():
        if not re.fullmatch('[0-9a-f]{64}', password):
            raise ValueError('Invalid generated database credential')
    statements = []
    for role in (META_USER, WRITER, READER):
        statements.append(f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '{passwords[role]}';")
    statements += [f'CREATE DATABASE {META_DB} OWNER {META_USER};',
                   f'CREATE DATABASE {ANALYTICS_DB} OWNER {WRITER};',
                   f'REVOKE ALL ON DATABASE {META_DB} FROM PUBLIC;',
                   f'REVOKE ALL ON DATABASE {ANALYTICS_DB} FROM PUBLIC;',
                   f'GRANT CONNECT ON DATABASE {ANALYTICS_DB} TO {READER};',
                   f'\\connect {ANALYTICS_DB}',
                   f'REVOKE CREATE ON SCHEMA public FROM PUBLIC;',
                   f'CREATE SCHEMA agentic AUTHORIZATION {WRITER};',
                   f'GRANT USAGE ON SCHEMA agentic TO {READER};',
                   f'ALTER DEFAULT PRIVILEGES FOR ROLE {WRITER} IN SCHEMA agentic GRANT SELECT ON TABLES TO {READER};']
    # Explicitly exit psql rather than relying on exec stdin EOF propagation.
    return '\n'.join(statements) + '\n\\quit\n'


def psql_command(pod, container, sql=None):
    """Read-only SQL uses argv; credential-bearing provisioning uses stdin only."""
    provisioning = sql is None
    command = ['-n', PG_NAMESPACE, 'exec', '--pod-running-timeout=10s']
    if provisioning:
        command.append('-i')
    command += [pod, '-c', container, '--', 'env', 'PGCONNECT_TIMEOUT=5',
        'PGOPTIONS=-c statement_timeout=' + ('60000' if provisioning else '10000') + ' -c lock_timeout=5000',
        'psql', '-X', '-w', '-U', 'postgres', '-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-At']
    if not provisioning:
        command += ['--command', sql]
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Built PostgreSQL-enabled Superset image pinned by @sha256 digest')
    parser.add_argument('--context')
    parser.add_argument('--pull-secret')
    parser.add_argument('--cluster-domain', default='cluster.local')
    parser.add_argument('--allow-existing-superset', action='store_true',
                        help='Create an isolated installation alongside a differently named Superset workload; never overwrite installer resources.')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[^\s@]+@sha256:[a-f0-9]{64}', args.image):
        parser.error('Use an immutable image reference with a sha256 digest.')
    if not re.fullmatch(r'[a-z0-9.-]+', args.cluster_domain):
        parser.error('Invalid cluster DNS domain.')
    cluster = Cluster(args.context)
    node = cluster.get('node', NODE)
    if node.get('status', {}).get('nodeInfo', {}).get('architecture') != 'amd64':
        raise RuntimeError('This deployment package expects an amd64 node and amd64 image.')
    conditions = {c['type']: c['status'] for c in node['status']['conditions']}
    if (node['spec'].get('unschedulable') or conditions.get('Ready') != 'True'
            or any(conditions.get(k) == 'True' for k in ('MemoryPressure', 'DiskPressure', 'PIDPressure'))
            or any(t['effect'] in {'NoSchedule', 'NoExecute'} for t in node['spec'].get('taints', []))):
        raise RuntimeError('Selected node is not currently eligible for this deployment.')
    pods = cluster.get('pods', extra=('--all-namespaces', '--field-selector', 'spec.nodeName=' + NODE))['items']
    active = [p for p in pods if p['status'].get('phase') not in ('Succeeded', 'Failed')]
    for resource, required in [('cpu', '1'), ('memory', '2Gi')]:
        remaining = quantity(node['status']['allocatable'][resource]) - sum(pod_request(p, resource) for p in active)
        if remaining < quantity(required):
            raise RuntimeError('Selected node no longer has sufficient unreserved ' + resource)
    if len(active) >= int(node['status']['allocatable']['pods']):
        raise RuntimeError('Selected node has no free pod slots.')
    # Independently named installations can coexist with separate credentials,
    # databases and services. Actual name/selector collisions always stop.
    resources = cluster.get('deployments,statefulsets,jobs,services,configmaps', namespace=NAMESPACE)['items']
    other_superset = []
    for resource in resources:
        name = resource['metadata']['name']
        identity = resource['kind'] + '/' + name
        images = [c.get('image', '') for c in resource.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])]
        if name.startswith(APP) or selects_new_pods(resource):
            raise RuntimeError('Installer resource or pod-selector collision: ' + identity
                               + '. Inspect partial state; this resource will not be overwritten.')
        if resource['kind'] in {'Deployment', 'StatefulSet'} and any('superset' in image.lower() for image in images):
            other_superset.append(identity)
    if other_superset:
        names = ', '.join(other_superset)
        if not args.allow_existing_superset:
            raise RuntimeError('Other Superset workloads detected: ' + names
                + '. To create a separate installation alongside them, pass --allow-existing-superset. '
                'Database, credential and selector collision checks remain enforced.')
        print('Separate Superset workloads found: ' + names + '. Creating the isolated ' + APP + ' installation.')
    secret_names = cluster.run('-n', NAMESPACE, 'get', 'secrets', '-o',
        'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}').splitlines()
    collisions = [name for name in secret_names if name.startswith(APP)]
    if collisions:
        raise RuntimeError('Installer credential references already exist: ' + ', '.join(collisions)
                           + '. Review partial state rather than replacing them.')
    endpoints = cluster.get('endpoints', PG_SERVICE, namespace=PG_NAMESPACE)
    if not any(s.get('addresses') and any(p['port'] == 5432 for p in s.get('ports', []))
               for s in endpoints.get('subsets', [])):
        raise RuntimeError('PostgreSQL primary service has no ready 5432 endpoint.')
    primaries = cluster.get('pods', namespace=PG_NAMESPACE,
        extra=('-l', f'cluster-name={PG_CLUSTER},spilo-role=master'))['items']
    if len(primaries) != 1:
        raise RuntimeError('Cannot identify exactly one PostgreSQL primary pod.')
    primary = primaries[0]
    containers = primary['spec']['containers']
    pg_container = next((c['name'] for c in containers if 'spilo' in c.get('image', '')), None)
    if not pg_container:
        raise RuntimeError('Cannot identify the existing PostgreSQL container.')
    check_sql = ("SELECT pg_is_in_recovery();\n"
        f"SELECT rolname FROM pg_roles WHERE rolname IN ('{META_USER}','{WRITER}','{READER}');\n"
        f"SELECT datname FROM pg_database WHERE datname IN ('{META_DB}','{ANALYTICS_DB}');\n")
    existing = cluster.run(*psql_command(primary['metadata']['name'], pg_container, check_sql), timeout=30).strip().splitlines()
    if existing != ['f']:
        raise RuntimeError('Primary verification failed or destination database/account names already exist. Review rather than overwrite.')
    if args.pull_secret:
        # Validate only existence; never print the Secret contents.
        cluster.run('-n', NAMESPACE, 'get', 'secret', args.pull_secret, '-o', 'name')
    print(f'Preflight passed: {NODE}; existing HA primary; fresh database names.')
    if not args.apply:
        print('Dry run only. Re-run with --apply to install. No cluster changes were made.')
        return
    password = getpass.getpass('Choose a NEW Superset administrator password (minimum 16 characters): ')
    if len(password) < 16:
        raise RuntimeError('Administrator password must contain at least 16 characters.')
    if getpass.getpass('Confirm administrator password: ') != password:
        raise RuntimeError('Passwords do not match.')
    credentials = {role: secrets.token_hex(32) for role in (META_USER, WRITER, READER)}
    host = f'{PG_SERVICE}.{PG_NAMESPACE}.svc.{args.cluster_domain}'
    def uri(role, database):
        return f'postgresql+psycopg2://{role}:{quote(credentials[role], safe="")}@{host}:5432/{database}?sslmode=require'
    # Persist credentials before provisioning; retain them on partial failure.
    cluster.create(secret_document(APP + '-runtime', {
        'SUPERSET_SECRET_KEY': secrets.token_urlsafe(64),
        'SUPERSET_METADATA_URI': uri(META_USER, META_DB),
        'AGENTIC_ANALYTICS_READ_URI': uri(READER, ANALYTICS_DB)}))
    cluster.create(secret_document(APP + '-bootstrap', {'SUPERSET_ADMIN_USERNAME': 'admin',
        'SUPERSET_ADMIN_EMAIL': 'superset-admin@localhost.invalid', 'SUPERSET_ADMIN_PASSWORD': password}))
    cluster.create(secret_document(APP + '-analytics-writer', {'HOST': host, 'PORT': '5432',
        'NAME': ANALYTICS_DB, 'USER': WRITER, 'PASSWORD': credentials[WRITER], 'SSLMODE': 'require'}))
    cluster.run(*psql_command(primary['metadata']['name'], pg_container),
                data=sql_for_accounts(credentials), timeout=120)
    config, job, deployment, service = manifests(args.image, args.pull_secret)
    cluster.apply(config)
    cluster.create(job)
    cluster.run('-n', NAMESPACE, 'wait', '--for=condition=complete', 'job/' + APP + '-init', '--timeout=900s', timeout=930)
    cluster.apply(service)
    cluster.apply(deployment)
    cluster.run('-n', NAMESPACE, 'rollout', 'status', 'deployment/' + APP, '--timeout=600s', timeout=630)
    print('Superset web deployment is ready. Initialization verified the database connection.')
    print('Read database/user IDs with: kubectl -n superset logs job/agentic-superset-init')
    print('Access: kubectl -n superset port-forward service/agentic-superset 8088:8088')
    print('Open http://127.0.0.1:8088 on that machine. Username: admin; password: the one you entered.')
    print('The analytics writer credential is retained in Secret agentic-superset-analytics-writer in namespace superset.')
    print('Agent publishing remains disabled until Django configuration and per-user Superset permissions are verified.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        print('Stopped. Existing objects and credentials are retained. Inspect partial state before retrying.', file=sys.stderr)
        sys.exit(1)
