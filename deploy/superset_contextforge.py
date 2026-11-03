"""Provision isolated Superset databases using ContextForge's in-cluster reference.

Credential values are resolved by Kubernetes inside a short-lived Job. The
    administration client reads only the Deployment's reference names, never
    Secret or ConfigMap data. Optional candidate keys are resolved in the Job.
"""
import json
import secrets
import time


WORKER = r'''import json
import os
import sys
from urllib.parse import urlsplit, parse_qsl, unquote
import psycopg2
from psycopg2 import sql

def finish(ok, code):
    message = json.dumps({'ok': ok, 'code': code})
    with open('/dev/termination-log', 'w') as output:
        output.write(message)
    print(message, flush=True)
    sys.exit(0 if ok else 1)

try:
    candidates = json.loads(os.environ['CONTEXTFORGE_URI_CANDIDATES'])
    raw = next((os.environ[key] for key in candidates if key in os.environ), None)
    if raw is None:
        finish(False, 'database_uri_reference_missing')
    parsed = urlsplit(raw)
    if parsed.scheme.split('+')[0] not in ('postgres', 'postgresql'):
        finish(False, 'connection_is_not_postgresql')
    if not parsed.username or parsed.password is None:
        finish(False, 'connection_needs_username_and_password')
    query = dict(parse_qsl(parsed.query))
    parameters = {key: value for key, value in query.items() if key in
        ('sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'application_name')}
    if 'ssl' in query and 'sslmode' not in parameters:
        parameters['sslmode'] = 'require' if query['ssl'].lower() in ('true', '1', 'require') else query['ssl']
    parameters.setdefault('sslmode', 'require')
    parameters.update(host=os.environ['TARGET_PG_HOST'], port=5432,
        user=unquote(parsed.username), password=unquote(parsed.password),
        dbname=unquote(parsed.path.lstrip('/')) or 'postgres', connect_timeout=5,
        options='-c statement_timeout=60000 -c lock_timeout=5000', target_session_attrs='read-write')
    conn = psycopg2.connect(**parameters)
    conn.autocommit = True
    cursor = conn.cursor()
    cursor.execute('SELECT pg_is_in_recovery()')
    if cursor.fetchone()[0]:
        finish(False, 'destination_is_standby')
    cursor.execute('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')
    if not cursor.fetchone()[0]:
        finish(False, 'provisioning_requires_postgres_admin_credential')
    roles = ('agentic_superset', 'agentic_snapshot_writer', 'agentic_snapshot_reader')
    databases = ('agentic_superset', 'agentic_analytics')
    cursor.execute('SELECT rolname FROM pg_roles WHERE rolname=ANY(%s)', (list(roles),))
    if cursor.fetchall():
        finish(False, 'superset_account_names_already_exist')
    cursor.execute('SELECT datname FROM pg_database WHERE datname=ANY(%s)', (list(databases),))
    if cursor.fetchall():
        finish(False, 'superset_database_names_already_exist')
    if os.environ['SETUP_MODE'] == 'check':
        cursor.close(); conn.close()
        finish(True, 'database_preflight_passed')
    with open('/etc/db-input/credentials.json') as source:
        passwords = json.load(source)
    if set(passwords) != set(roles):
        finish(False, 'invalid_generated_account_set')
    for role in roles:
        cursor.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}').format(
            sql.Identifier(role), sql.Literal(passwords[role])))
    for name, owner in [('agentic_superset', 'agentic_superset'), ('agentic_analytics', 'agentic_snapshot_writer')]:
        cursor.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(name), sql.Identifier(owner)))
        cursor.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
    cursor.execute('GRANT CONNECT ON DATABASE agentic_analytics TO agentic_snapshot_reader')
    cursor.close(); conn.close()
    parameters['dbname'] = 'agentic_analytics'
    conn = psycopg2.connect(**parameters)
    with conn:
        with conn.cursor() as cursor:
            cursor.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
            cursor.execute('CREATE SCHEMA agentic AUTHORIZATION agentic_snapshot_writer')
            cursor.execute('GRANT USAGE ON SCHEMA agentic TO agentic_snapshot_reader')
            cursor.execute('ALTER DEFAULT PRIVILEGES FOR ROLE agentic_snapshot_writer IN SCHEMA agentic GRANT SELECT ON TABLES TO agentic_snapshot_reader')
    conn.close()
    finish(True, 'superset_databases_created')
except psycopg2.Error as error:
    finish(False, 'postgres_error_' + (error.pgcode or 'connection_failed'))
except Exception:
    finish(False, 'database_setup_failed')
'''

MESSAGES = {
    'database_uri_reference_missing': 'ContextForge has no supported database URI key in its referenced settings. Expected DATABASE_URL, SQLALCHEMY_DATABASE_URI, POSTGRES_DSN or DB_URI. Credential values were not read by the installer.',
    'provisioning_requires_postgres_admin_credential': 'ContextForge uses a non-admin PostgreSQL account. A PostgreSQL administrator credential reference is required to create the separate Superset databases. No account privileges were changed.',
    'connection_is_not_postgresql': 'The selected ContextForge database reference is not a PostgreSQL URI.',
    'superset_account_names_already_exist': 'Superset account names already exist. Review partial state rather than recreating accounts.',
    'superset_database_names_already_exist': 'Superset database names already exist. Review partial state rather than overwriting data.',
    'destination_is_standby': 'The selected PostgreSQL primary service currently routes to a standby.',
    'postgres_error_connection_failed': 'The setup Job could not connect to PostgreSQL. Check account access, network policy and TLS configuration.',
}


def discover(cluster, namespace, deployment='contextforge'):
    resource = cluster.get('deployment', deployment, namespace=namespace)
    pod = resource['spec']['template']['spec']
    container = next((c for c in pod['containers'] if c['name'] == 'gateway'), None)
    if not container:
        raise RuntimeError('ContextForge gateway container was not found.')
    variables = ('DATABASE_URL', 'SQLALCHEMY_DATABASE_URI', 'POSTGRES_DSN', 'DB_URI')
    references = {key: [] for key in variables}
    for source in container.get('envFrom', []):
        for kind, key, reference in [('configmap', 'configMapRef', 'configMapKeyRef'), ('secret', 'secretRef', 'secretKeyRef')]:
            if key not in source:
                continue
            name = source[key]['name']
            prefix = source.get('prefix', '')
            for variable in variables:
                if variable.startswith(prefix):
                    references[variable].append({'valueFrom': {reference: {
                        'name': name, 'key': variable[len(prefix):], 'optional': True}}})
    for variable in container.get('env', []):
        if variable['name'] not in references:
            continue
        if 'valueFrom' not in variable:
            raise RuntimeError('ContextForge database URI is a literal Deployment setting. A ConfigMap/Secret reference is required for this provisioning mode.')
        references[variable['name']] = [{'valueFrom': variable['valueFrom']}]
    environments = []
    for variable in variables:
        for reference in reversed(references[variable]):
            environments.append({'name': 'CONTEXTFORGE_DB_REF_' + str(len(environments)), **reference})
    if not environments:
        raise RuntimeError('No database setting references were found on the ContextForge gateway container.')
    return {'namespace': namespace, 'environments': environments,
            'pull_secrets': pod.get('imagePullSecrets', [])}


def documents(source, image, host, mode, passwords=None, pull_secret=None):
    identifier = 'agentic-superset-db-' + mode + '-' + secrets.token_hex(4)
    namespace = source['namespace']
    def metadata(name):
        return {'name': name, 'namespace': namespace, 'labels': {'app.kubernetes.io/managed-by': 'agentic-superset-installer'}}
    config = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': metadata(identifier), 'data': {'setup.py': WORKER}}
    pod = {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
        'affinity': {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': [
            {'matchFields': [{'key': 'metadata.name', 'operator': 'In', 'values': ['etlreg-virtual-machine']}]}]}}},
        'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000, 'seccompProfile': {'type': 'RuntimeDefault'}},
        'volumes': [{'name': 'script', 'configMap': {'name': identifier}}],
        'containers': [{'name': 'database-setup', 'image': image, 'imagePullPolicy': 'IfNotPresent',
            'command': ['python', '/etc/db-setup/setup.py'],
            'env': source['environments'] + [
                {'name': 'CONTEXTFORGE_URI_CANDIDATES', 'value': json.dumps([e['name'] for e in source['environments']])},
                {'name': 'TARGET_PG_HOST', 'value': host}, {'name': 'SETUP_MODE', 'value': mode}],
            'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'cpu': '500m', 'memory': '512Mi'}},
            'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
            'volumeMounts': [{'name': 'script', 'mountPath': '/etc/db-setup', 'readOnly': True}],
            'terminationMessagePolicy': 'File'}]}
    pull = [{'name': pull_secret}] if pull_secret else source['pull_secrets']
    if pull:
        pod['imagePullSecrets'] = pull
    inputs = None
    if mode == 'provision':
        inputs = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': metadata(identifier),
                  'type': 'Opaque', 'stringData': {'credentials.json': json.dumps(passwords)}}
        pod['volumes'].append({'name': 'input', 'secret': {'secretName': identifier}})
        pod['containers'][0]['volumeMounts'].append({'name': 'input', 'mountPath': '/etc/db-input', 'readOnly': True})
    job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': metadata(identifier),
        'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 600,
                 'template': {'spec': pod}}}
    return config, inputs, job


def run_job(cluster, source, image, host, mode, passwords=None, pull_secret=None):
    if pull_secret:
        cluster.run('-n', source['namespace'], 'get', 'secret', pull_secret, '-o', 'name')
    config, inputs, job = documents(source, image, host, mode, passwords, pull_secret)
    name, namespace = job['metadata']['name'], source['namespace']
    cluster.create(config)
    if inputs:
        cluster.create(inputs)
    cluster.create(job)
    print(f'Database {mode} Job: {namespace}/{name}', flush=True)
    deadline = time.monotonic() + 660
    while time.monotonic() < deadline:
        state = cluster.get('job', name, namespace=namespace)
        conditions = {c['type']: c.get('status') == 'True' for c in state.get('status', {}).get('conditions', [])}
        if conditions.get('Complete'):
            # No logs/exec transport is needed; success and failure are read from the Kubernetes API.
            for kind in ['job', 'configmap'] + (['secret'] if inputs else []):
                cluster.run('-n', namespace, 'delete', kind, name, '--wait=false')
            return
        if conditions.get('Failed'):
            pods = cluster.get('pods', namespace=namespace, extra=('-l', 'job-name=' + name))['items']
            codes = []
            for pod in pods:
                for container in pod.get('status', {}).get('containerStatuses', []):
                    message = container.get('state', {}).get('terminated', {}).get('message', '')
                    try:
                        codes.append(json.loads(message).get('code', 'database_setup_failed'))
                    except (ValueError, AttributeError):
                        continue
            code = codes[0] if codes else 'database_setup_failed'
            message = MESSAGES.get(code, 'Database Job failed; inspect the retained pod status. Diagnostic code: ' + code)
            raise RuntimeError(message + f' Job retained: {namespace}/{name}')
        time.sleep(2)
    raise RuntimeError(f'Database Job did not finish within 660 seconds: {namespace}/{name}. Inspect retained Job and pod events before retrying.')
