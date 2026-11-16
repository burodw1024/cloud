from contextlib import nullcontext
from copy import deepcopy
import os
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]


class SupersetBootstrapTests(unittest.TestCase):
    def execute(self, tables=(), available=True, app_version='4.1.2'):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (available,)
        cursor.fetchall.return_value = list(tables)
        driver = SimpleNamespace(connect=MagicMock(return_value=connection))
        manager = MagicMock()
        manager.find_user.return_value = None
        manager.add_user.return_value = True
        package = ModuleType('superset')
        package.__path__ = []
        app = SimpleNamespace(app_context=lambda: nullcontext())
        modules = {'psycopg2': driver, 'superset': package,
                   'superset.app': SimpleNamespace(create_app=lambda: app),
                   'superset.extensions': SimpleNamespace(appbuilder=SimpleNamespace(sm=manager))}
        values = {'POSTGRES_HOST': '192.168.1.223', 'POSTGRES_PORT': '5432', 'POSTGRES_DB': 'superset',
                  'POSTGRES_USER': 'supersetadmin', 'POSTGRES_PASSWORD': 'test-db-password', 'POSTGRES_SSLMODE': 'require',
                  'BOOTSTRAP_ADMIN_USERNAME': 'admin', 'BOOTSTRAP_ADMIN_PASSWORD': 'test-web-password',
                  'BOOTSTRAP_ADMIN_EMAIL': 'admin@buro.local'}
        failure = None
        with patch.dict(sys.modules, modules), patch.dict(os.environ, values), \
             patch('importlib.metadata.version', return_value=app_version), patch('subprocess.run') as run:
            helper = runpy.run_path(str(ROOT/'deploy/superset/bootstrap_metadata.py'))
            try:
                helper['main']()
            except RuntimeError as exc:
                failure = exc
        return failure, run, manager, connection, driver

    def test_empty_database_initializes_and_creates_web_admin(self):
        failure, run, manager, connection, _ = self.execute()
        self.assertIsNone(failure)
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [['superset', 'db', 'upgrade'], ['superset', 'init']])
        manager.add_user.assert_called_once()
        self.assertEqual(manager.add_user.call_args.kwargs['password'], 'test-web-password')
        connection.close.assert_called_once()

    def test_existing_tables_block_migrations_and_account_changes(self):
        failure, run, manager, connection, _ = self.execute(tables=[('another_schema', 'existing_data')])
        self.assertIn('not empty', str(failure))
        run.assert_not_called()
        manager.add_user.assert_not_called()
        connection.close.assert_called_once()

    def test_concurrent_initializer_is_blocked(self):
        failure, run, manager, connection, _ = self.execute(available=False)
        self.assertIn('Another metadata initialization', str(failure))
        run.assert_not_called()
        manager.add_user.assert_not_called()
        connection.close.assert_called_once()

    def test_wrong_application_version_blocks_database_connection(self):
        failure, run, manager, _, driver = self.execute(app_version='5.0.0')
        self.assertIn('4.1.2', str(failure))
        driver.connect.assert_not_called()
        run.assert_not_called()

    def test_job_reuses_imported_image_and_database_secret_without_web_probes(self):
        helper = runpy.run_path(str(ROOT/'deploy/superset/initialize_empty_db.py'))
        deployment = {'spec': {'template': {'spec': {
            'nodeSelector': {'kubernetes.io/hostname': 'superset'},
            'containers': [{'name': 'superset', 'image': '192.168.1.224:80/superset:4.1.2-pg-test',
                'envFrom': [{'secretRef': {'name': 'superset-external-db-secrets'}}],
                'startupProbe': {'httpGet': {'path': '/health', 'port': 8088}},
                'volumeMounts': [{'name': 'config', 'mountPath': '/etc/superset'}]}],
            'volumes': [{'name': 'config', 'configMap': {'name': 'superset-config'}}]}}}}
        original = deepcopy(deployment)
        job = helper['build_job'](deployment, 'init-test', 'print("test")')
        pod = job['spec']['template']['spec']
        self.assertEqual(deployment, original)
        self.assertEqual(pod['containers'][0]['imagePullPolicy'], 'Never')
        self.assertNotIn('startupProbe', pod['containers'][0])
        self.assertEqual(pod['containers'][0]['envFrom'][-1]['secretRef']['name'], 'init-test')
        self.assertEqual(job['spec']['backoffLimit'], 0)
        self.assertNotEqual(job['spec']['template']['metadata']['labels']['app'], 'superset')
