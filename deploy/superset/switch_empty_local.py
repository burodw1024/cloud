"""Retain, but stop, the first install's PostgreSQL after verifying it is empty."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy.pod_ownership import deployment_pods

EMPTY_SQL = """SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind IN ('r','p','m','f') AND n.nspname NOT IN ('pg_catalog','information_schema')
AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%';"""


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--switch-empty-local', action='store_true')
    args = parser.parse_args()
    base = ['kubectl', '--context', args.context, '-n', 'superset-onprem']
    def get(kind, name=None):
        raw = subprocess.check_output(base+['get', kind]+([name] if name else [])+
                                      ['--ignore-not-found', '-o', 'json'], text=True).strip()
        return json.loads(raw) if raw else None
    def run(*command):
        subprocess.run(base+list(command), check=True)
    database = get('deployment', 'superset-db')
    if not database:
        return
    uid = database['metadata']['uid']
    marker = get('configmap', 'superset-local-db-retained')
    if marker:
        require(marker.get('data', {}).get('database_uid') == uid,
                'Retained database identity changed; inspect before switching')
        require(database['spec'].get('replicas') == 0,
                'Retained local database has been restarted; inspect it before proceeding')
        print('Previously verified empty local database remains retained at zero replicas.')
        return
    require(args.switch_empty_local,
            'Local database exists. Use --switch-empty-local only to verify emptiness and retain it stopped.')
    require(database['spec'].get('replicas') == 1,
            'Local database must be available for the emptiness check; no resources were deleted')
    def owned(deployment):
        return deployment_pods(deployment, get('replicasets'), get('pods'), include_terminating=True)
    def empty():
        pods = owned(database)
        require(len(pods) == 1 and pods[0].get('status', {}).get('phase') == 'Running',
                'Expected one running local PostgreSQL pod')
        command = ['sh', '-ec', 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"',
                   'empty-check', EMPTY_SQL]
        count = subprocess.check_output(base+['exec', pods[0]['metadata']['name'], '-c', 'postgres', '--']+command,
                                        text=True).strip()
        require(count == '0', 'Local database has user tables; refusing to switch. Export/migrate metadata first.')
    def drain(deployment):
        for pod in owned(deployment):
            run('wait', '--for=delete', 'pod/'+pod['metadata']['name'], '--timeout=180s')
    empty()  # Refuse nonempty databases before stopping anything.
    application = get('deployment', 'superset')
    if application:
        run('scale', 'deployment/superset', '--replicas=0')
        drain(application)
    empty()  # Recheck after the app can no longer initialize/write metadata.
    secret = get('secret', 'superset-secrets')
    if secret:
        backup = get('secret', 'superset-local-credentials-retained')
        if backup:
            require(backup.get('data') == secret.get('data'), 'Credential backup differs; inspect before proceeding')
        else:
            backup = {'apiVersion': 'v1', 'kind': 'Secret', 'type': secret.get('type', 'Opaque'),
                      'metadata': {'name': 'superset-local-credentials-retained', 'namespace': 'superset-onprem'},
                      'data': secret['data']}
            # Send directly to kubectl stdin; never print secret contents or save to disk.
            subprocess.run(base+['create', '-f', '-'], input=json.dumps(backup), text=True, check=True)
    run('scale', 'deployment/superset-db', '--replicas=0')
    drain(database)
    require(get('deployment', 'superset-db')['metadata']['uid'] == uid, 'Database identity changed during switch')
    run('create', 'configmap', 'superset-local-db-retained', '--from-literal=database_uid='+uid)
    print('Verified empty local PostgreSQL stopped. Deployment, hostPath data and credentials retained; nothing deleted.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
