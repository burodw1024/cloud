"""ContextForge credential references stay in-cluster; no live infrastructure used."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

spec = importlib.util.spec_from_file_location('superset_contextforge', Path(__file__).resolve().parents[2] / 'deploy/superset_contextforge.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
IMAGE = 'registry/superset@sha256:' + 'a' * 64
HOST = 'acid-postgres-primary-nodeport.postgres-ha-test.svc.cluster.local'


class ReferenceTests(SimpleTestCase):
    def setUp(self):
        self.cluster = MagicMock()
        self.cluster.get.return_value = {'spec': {'template': {'spec': {'containers': [
            {'name': 'gateway', 'envFrom': [{'configMapRef': {'name': 'contextforge-config'}},
                                         {'secretRef': {'name': 'contextforge-secrets'}}]}]}}}}

    def test_discovery_does_not_fetch_secret_or_configmap_data(self):
        source = helper.discover(self.cluster, 'buro-agentic')
        self.cluster.get.assert_called_once_with('deployment', 'contextforge', namespace='buro-agentic')
        self.cluster.run.assert_not_called()
        # The later Secret envFrom takes precedence over the earlier ConfigMap.
        self.assertEqual(source['environments'][0]['valueFrom']['secretKeyRef'], {
            'name': 'contextforge-secrets', 'key': 'DATABASE_URL', 'optional': True})
        text = json.dumps(source)
        self.assertNotIn('REDIS_PASSWORD', text)
        self.assertNotIn('JWT_SECRET', text)

    def test_job_uses_only_candidate_keys_in_original_namespace_and_no_service_token(self):
        source = helper.discover(self.cluster, 'buro-agentic')
        config, secret, job = helper.documents(source, IMAGE, HOST, 'check')
        self.assertIsNone(secret)
        self.assertEqual(job['metadata']['namespace'], 'buro-agentic')
        pod = job['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(job['spec']['backoffLimit'], 0)
        container = pod['containers'][0]
        self.assertNotIn('envFrom', container)
        self.assertEqual(container['command'], ['python', '/etc/db-setup/setup.py'])
        self.assertEqual(container['env'][-2]['value'], HOST)
        self.assertIn('etlreg-virtual-machine', json.dumps(pod['affinity']))

    def test_explicit_reference_overrides_envfrom_and_literal_override_is_rejected(self):
        container = self.cluster.get.return_value['spec']['template']['spec']['containers'][0]
        container['env'] = [{'name': 'DATABASE_URL', 'valueFrom': {'secretKeyRef': {'name': 'special', 'key': 'uri'}}}]
        source = helper.discover(self.cluster, 'buro-agentic')
        self.assertEqual(source['environments'][0]['valueFrom']['secretKeyRef']['name'], 'special')
        container['env'] = [{'name': 'DATABASE_URL', 'value': 'never-print-this'}]
        with self.assertRaises(RuntimeError) as error:
            helper.discover(self.cluster, 'buro-agentic')
        self.assertNotIn('never-print-this', str(error.exception))

    def test_generated_password_payload_is_mounted_as_a_secret_not_an_argument(self):
        source = helper.discover(self.cluster, 'buro-agentic')
        passwords = {'role': 'private-generated-password'}
        config, secret, job = helper.documents(source, IMAGE, HOST, 'provision', passwords)
        self.assertIn('private-generated-password', secret['stringData']['credentials.json'])
        self.assertNotIn('private-generated-password', json.dumps(job))
        self.assertNotIn('private-generated-password', json.dumps(config))

    def test_job_success_uses_status_api_and_cleans_up_without_logs_or_exec(self):
        source = helper.discover(self.cluster, 'buro-agentic')
        self.cluster.reset_mock()
        self.cluster.get.return_value = {'status': {'conditions': [{'type': 'Complete', 'status': 'True'}]}}
        with contextlib.redirect_stdout(io.StringIO()):
            helper.run_job(self.cluster, source, IMAGE, HOST, 'check')
        self.assertEqual(self.cluster.create.call_count, 2)
        commands = [call.args for call in self.cluster.run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertTrue(all('delete' in command for command in commands))
        self.assertTrue(all('exec' not in command and 'logs' not in command for command in commands))

    def test_permission_failure_reports_safe_status_and_keeps_evidence(self):
        source = helper.discover(self.cluster, 'buro-agentic')
        self.cluster.reset_mock()
        self.cluster.get.side_effect = [
            {'status': {'conditions': [{'type': 'Failed', 'status': 'True'}]}},
            {'items': [{'status': {'containerStatuses': [{'state': {'terminated': {'message': json.dumps(
                {'code': 'provisioning_requires_postgres_admin_credential'})}}}]}}]},
        ]
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'non-admin'):
            helper.run_job(self.cluster, source, IMAGE, HOST, 'check')
        self.cluster.run.assert_not_called()


class FakeCursor:
    def __init__(self, admin=True, collision=False):
        self.admin = admin
        self.collision = collision
        self.last = ''
        self.statements = []

    def execute(self, statement, params=None):
        self.last = statement
        self.statements.append(statement)

    def fetchone(self):
        return (self.admin,) if 'rolsuper' in self.last else (False,)

    def fetchall(self):
        return [('agentic_superset',)] if self.collision else []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class WorkerTests(SimpleTestCase):
    def run_worker(self, uri, admin=True, mode='check', collision=False):
        cursor = FakeCursor(admin, collision)
        connection = MagicMock()
        connection.cursor.return_value = cursor
        class DatabaseError(Exception):
            pass
        class SQL(str):
            def format(self, *values):
                return str.format(self, *values)
        sql = SimpleNamespace(SQL=SQL, Identifier=lambda text: '"' + text + '"', Literal=lambda text: repr(text))
        psycopg = SimpleNamespace(connect=MagicMock(return_value=connection), Error=DatabaseError, sql=sql)
        status_file = io.StringIO()
        class StatusFile:
            def __enter__(self):
                return status_file
            def __exit__(self, *args):
                pass
        environment = {'CONTEXTFORGE_URI_CANDIDATES': '["ref"]', 'ref': uri,
                       'TARGET_PG_HOST': HOST, 'SETUP_MODE': mode}
        def open_file(path, *args):
            if path == '/dev/termination-log':
                return StatusFile()
            return io.StringIO(json.dumps({role: 'test-password' for role in (
                'agentic_superset', 'agentic_snapshot_writer', 'agentic_snapshot_reader')}))
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), patch.dict(sys.modules, {'psycopg2': psycopg}), patch(
                'builtins.open', side_effect=open_file), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit):
                exec(compile(helper.WORKER, '<database-worker>', 'exec'), {})
        return json.loads(status_file.getvalue()), cursor, psycopg, output.getvalue()

    def test_read_only_check_validates_admin_without_creating_anything(self):
        result, cursor, psycopg, output = self.run_worker('postgresql+psycopg://admin:private-pass@original/gateway')
        self.assertTrue(result['ok'])
        self.assertTrue(all(statement.startswith('SELECT ') for statement in cursor.statements))
        self.assertNotIn('private-pass', output)
        self.assertEqual(psycopg.connect.call_args.kwargs['host'], HOST)
        self.assertEqual(psycopg.connect.call_args.kwargs['dbname'], 'gateway')

    def test_non_admin_connection_stops_before_role_or_database_creation(self):
        result, cursor, psycopg, output = self.run_worker('postgresql://app:private-pass@original/gateway', admin=False)
        self.assertEqual(result['code'], 'provisioning_requires_postgres_admin_credential')
        self.assertTrue(all(statement.startswith('SELECT ') for statement in cursor.statements))
        self.assertNotIn('private-pass', output)

    def test_sqlite_connection_is_not_reused_as_a_postgres_admin_connection(self):
        result, cursor, psycopg, output = self.run_worker('sqlite:////data/contextforge.db')
        self.assertEqual(result['code'], 'connection_is_not_postgresql')
        psycopg.connect.assert_not_called()

    def test_provisioning_creates_only_dedicated_accounts_databases_and_analytics_schema(self):
        result, cursor, psycopg, output = self.run_worker('postgresql://admin:private-pass@original/gateway', mode='provision')
        self.assertEqual(result['code'], 'superset_databases_created')
        writes = [statement for statement in cursor.statements if not statement.startswith('SELECT ')]
        self.assertEqual(sum(statement.startswith('CREATE ROLE') for statement in writes), 3)
        self.assertEqual(sum(statement.startswith('CREATE DATABASE') for statement in writes), 2)
        self.assertFalse(any('gateway' in statement for statement in writes))
        self.assertFalse(any(statement.startswith('DROP ') for statement in writes))
        self.assertIn('GRANT USAGE ON SCHEMA agentic TO agentic_snapshot_reader', writes)
        self.assertEqual(psycopg.connect.call_args.kwargs['dbname'], 'agentic_analytics')
        self.assertNotIn('private-pass', output)
        self.assertNotIn('test-password', output)

    def test_existing_accounts_stop_the_provisioning_worker_before_any_writes(self):
        result, cursor, psycopg, output = self.run_worker('postgresql://admin:private-pass@original/gateway', mode='provision', collision=True)
        self.assertEqual(result['code'], 'superset_account_names_already_exist')
        self.assertTrue(all(statement.startswith('SELECT ') for statement in cursor.statements))
