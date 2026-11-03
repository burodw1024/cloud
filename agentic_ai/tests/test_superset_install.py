"""Deployment contract tests; no Kubernetes cluster or database is contacted."""
import contextlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

spec = importlib.util.spec_from_file_location('superset_installer', Path(__file__).resolve().parents[2] / 'deploy/install_superset.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
IMAGE = 'registry.internal/agentic-superset@sha256:' + 'a' * 64


class FakeCluster:
    def __init__(self):
        self.events = []
        self.resources = []
        self.node = {'spec': {}, 'status': {'nodeInfo': {'architecture': 'amd64'}, 'conditions': [{'type': 'Ready', 'status': 'True'}],
                    'allocatable': {'cpu': '4', 'memory': '16Gi', 'pods': '110'}}}
        self.pods = []
        self.endpoint = {'subsets': [{'addresses': [{'ip': '10.0.0.1'}], 'ports': [{'port': 5432}]}]}
        self.primary = {'metadata': {'name': 'acid-postgres-test-0'}, 'spec': {
            'containers': [{'name': 'postgres', 'image': 'ghcr.io/zalando/spilo-17:4.0-p3'}]}}
        self.sql_preflight = 'f\n'
        self.secret_names = ''
        self.fail_job = False

    def get(self, kind, name=None, namespace=None, extra=()):
        if kind == 'node':
            return self.node
        if kind.startswith('deployments,'):
            return {'items': self.resources}
        if kind == 'endpoints':
            return self.endpoint
        if kind == 'pods':
            return {'items': [self.primary] if namespace == installer.PG_NAMESPACE else self.pods}
        raise AssertionError('Unexpected read: ' + kind)

    def run(self, *args, data=None, timeout=60):
        self.events.append(('run', args, data))
        if 'secrets' in args:
            return self.secret_names
        if '--command' in args and args[-1].startswith('SELECT pg_is_in_recovery'):
            return self.sql_preflight
        if 'wait' in args and self.fail_job:
            raise RuntimeError('Initialization failed')
        return ''

    def create(self, document):
        self.events.append(('create', document))

    def apply(self, document):
        self.events.append(('apply', document))


class SupersetInstallTests(SimpleTestCase):
    def run_install(self, cluster, apply=False, allow_existing=False):
        args = ['install_superset.py', '--image', IMAGE] + (['--apply'] if apply else [])
        if allow_existing:
            args.append('--allow-existing-superset')
        output = io.StringIO()
        with patch.object(installer, 'Cluster', return_value=cluster), patch('sys.argv', args), patch.object(
                installer.getpass, 'getpass', return_value='test-only-password-12345'), contextlib.redirect_stdout(output):
            installer.main()
        return output.getvalue()

    def test_dry_run_only_queries_cluster_and_does_not_prompt_or_write(self):
        cluster = FakeCluster()
        with patch.object(installer.getpass, 'getpass', side_effect=AssertionError('No password needed')):
            result = self.run_install(cluster)
        self.assertIn('Dry run only', result)
        self.assertFalse(any(event[0] in {'create', 'apply'} for event in cluster.events))
        self.assertTrue(all(not event[2] or event[2].startswith('SELECT ') for event in cluster.events))

    def test_fresh_install_orders_initialization_before_web_and_reuses_ha(self):
        cluster = FakeCluster()
        output = self.run_install(cluster, apply=True)
        written = [event[1] for event in cluster.events if event[0] in {'create', 'apply'}]
        self.assertNotIn('StatefulSet', [item['kind'] for item in written])
        self.assertNotIn('PersistentVolumeClaim', [item['kind'] for item in written])
        secrets = [item for item in written if item['kind'] == 'Secret']
        self.assertEqual(len(secrets), 3)
        runtime = next(item for item in secrets if item['metadata']['name'].endswith('-runtime'))
        self.assertIn('acid-postgres-primary-nodeport.postgres-ha-test.svc.cluster.local:5432', runtime['stringData']['SUPERSET_METADATA_URI'])
        self.assertIn('sslmode=require', runtime['stringData']['SUPERSET_METADATA_URI'])
        self.assertNotIn('test-only-password-12345', output)
        for secret in secrets:
            for value in secret['stringData'].values():
                if len(value) > 30:
                    self.assertNotIn(value, output)
        wait = next(i for i, event in enumerate(cluster.events) if event[0] == 'run' and 'wait' in event[1])
        deploy = next(i for i, event in enumerate(cluster.events) if event[0] == 'apply' and event[1]['kind'] == 'Deployment')
        self.assertLess(wait, deploy)

    def test_job_failure_retains_credentials_and_does_not_start_web(self):
        cluster = FakeCluster(); cluster.fail_job = True
        with self.assertRaises(RuntimeError):
            self.run_install(cluster, apply=True)
        writes = [event[1] for event in cluster.events if event[0] in {'create', 'apply'}]
        self.assertEqual(sum(item['kind'] == 'Secret' for item in writes), 3)
        self.assertFalse(any(item['kind'] == 'Deployment' for item in writes))

    def test_existing_database_or_standby_blocks_all_changes(self):
        for result in ['t\n', 'f\nagentic_superset\n']:
            cluster = FakeCluster(); cluster.sql_preflight = result
            with self.assertRaises(RuntimeError):
                self.run_install(cluster, apply=True)
            self.assertFalse(any(event[0] in {'create', 'apply'} for event in cluster.events))

    def test_existing_secret_or_web_installation_is_not_overwritten(self):
        cluster = FakeCluster(); cluster.secret_names = 'agentic-superset-runtime\n'
        with self.assertRaises(RuntimeError):
            self.run_install(cluster, apply=True)
        cluster = FakeCluster()
        cluster.resources = [{'kind': 'Deployment', 'metadata': {'name': 'old-superset'}, 'spec': {
            'template': {'spec': {'containers': [{'image': 'apache/superset:old'}]}}}}]
        with self.assertRaises(RuntimeError):
            self.run_install(cluster, apply=True)

    def test_node_pressure_or_insufficient_requests_stops_install(self):
        for change in ['pressure', 'memory', 'taint']:
            cluster = FakeCluster()
            if change == 'pressure':
                cluster.node['status']['conditions'].append({'type': 'MemoryPressure', 'status': 'True'})
            elif change == 'memory':
                cluster.node['status']['allocatable']['memory'] = '1Gi'
            else:
                cluster.node['spec']['taints'] = [{'key': 'dedicated', 'effect': 'NoSchedule'}]
            with self.assertRaises(RuntimeError):
                self.run_install(cluster, apply=True)
            self.assertFalse(any(event[0] in {'create', 'apply'} for event in cluster.events))

    def test_separate_unused_installation_can_coexist_without_being_modified(self):
        cluster = FakeCluster()
        cluster.resources = [{'kind': 'Deployment', 'metadata': {'name': 'superset'}, 'spec': {
            'template': {'spec': {'containers': [{'image': 'apache/superset:old'}]}}}}]
        with self.assertRaisesRegex(RuntimeError, 'Deployment/superset'):
            self.run_install(cluster)
        self.run_install(cluster, apply=True, allow_existing=True)
        for event in cluster.events:
            if event[0] in {'create', 'apply'}:
                self.assertTrue(event[1]['metadata']['name'].startswith(installer.APP))

    def test_override_does_not_allow_own_name_secret_database_or_selector_collisions(self):
        scenarios = [
            {'kind': 'Deployment', 'metadata': {'name': installer.APP}},
            {'kind': 'Service', 'metadata': {'name': 'old-public-service'},
             'spec': {'selector': {'app.kubernetes.io/name': installer.APP}}},
            {'kind': 'Deployment', 'metadata': {'name': 'old-controller'},
             'spec': {'selector': {'matchExpressions': [{'key': 'app.kubernetes.io/name',
                       'operator': 'In', 'values': [installer.APP]}]}}},
        ]
        for resource in scenarios:
            cluster = FakeCluster(); cluster.resources = [resource]
            with self.assertRaisesRegex(RuntimeError, 'collision'):
                self.run_install(cluster, apply=True, allow_existing=True)
            self.assertFalse(any(event[0] in {'create', 'apply'} for event in cluster.events))
        for secret_names, database_result in [('agentic-superset-runtime\n', 'f\n'), ('', 'f\nagentic_superset\n')]:
            cluster = FakeCluster(); cluster.secret_names = secret_names; cluster.sql_preflight = database_result
            with self.assertRaises(RuntimeError):
                self.run_install(cluster, apply=True, allow_existing=True)
            self.assertFalse(any(event[0] in {'create', 'apply'} for event in cluster.events))

    def test_unrelated_service_and_controller_selectors_do_not_match_new_pods(self):
        for resource in [
            {'kind': 'Service', 'spec': {'selector': {'app': 'superset'}}},
            {'kind': 'Deployment', 'spec': {'selector': {'matchLabels': {'app': 'superset'}}}},
            {'kind': 'Deployment', 'spec': {'selector': {'matchExpressions': [
                {'key': 'app.kubernetes.io/name', 'operator': 'In', 'values': ['other']}]}}},
        ]:
            self.assertFalse(installer.selects_new_pods(resource))

    def test_manifest_has_required_placement_limits_probes_and_private_service(self):
        config, job, deployment, service = installer.manifests(IMAGE, 'registry-auth')
        pod = deployment['spec']['template']['spec']
        terms = pod['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']
        self.assertEqual(terms[0]['matchFields'][0]['values'], ['etlreg-virtual-machine'])
        container = pod['containers'][0]
        self.assertEqual(container['resources']['requests'], {'cpu': '1', 'memory': '2Gi'})
        self.assertEqual(container['resources']['limits'], {'cpu': '2', 'memory': '4Gi'})
        self.assertIn('startupProbe', container)
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['securityContext']['runAsUser'], 1000)
        self.assertEqual(service['spec']['type'], 'ClusterIP')
        self.assertEqual(job['spec']['backoffLimit'], 0)
        self.assertEqual(pod['imagePullSecrets'], [{'name': 'registry-auth'}])
        self.assertIn("'DASHBOARD_RBAC': True", config['data']['superset_config.py'])

    def test_sql_uses_distinct_roles_restricts_access_and_rejects_untrusted_passwords(self):
        passwords = {role: 'a' * 64 for role in (installer.META_USER, installer.WRITER, installer.READER)}
        sql = installer.sql_for_accounts(passwords)
        self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', sql)
        self.assertIn('REVOKE ALL ON DATABASE agentic_analytics FROM PUBLIC', sql)
        self.assertIn('GRANT SELECT ON TABLES TO agentic_snapshot_reader', sql)
        passwords[installer.META_USER] = "x'; DROP DATABASE postgres;"
        with self.assertRaises(ValueError):
            installer.sql_for_accounts(passwords)

    def test_scheduler_request_includes_init_sidecars_and_overhead(self):
        def container(cpu, sidecar=False):
            return {'resources': {'requests': {'cpu': cpu}}, **({'restartPolicy': 'Always'} if sidecar else {})}
        pod = {'spec': {'containers': [container('500m')], 'initContainers': [
            container('200m', True), container('2'), container('300m', True)], 'overhead': {'cpu': '100m'}}}
        self.assertEqual(installer.pod_request(pod, 'cpu'), installer.Decimal('2.3'))

    def test_empty_primary_service_is_rejected(self):
        cluster = FakeCluster(); cluster.endpoint = {'subsets': []}
        with self.assertRaises(RuntimeError):
            self.run_install(cluster, apply=True)

    def test_read_only_psql_avoids_stdin_and_password_prompts(self):
        cluster = FakeCluster()
        self.run_install(cluster)
        read = next(event for event in cluster.events if 'psql' in event[1])
        self.assertNotIn('-i', read[1])
        self.assertIn('-w', read[1])
        self.assertIn('PGCONNECT_TIMEOUT=5', read[1])
        self.assertIn('--command', read[1])
        self.assertIsNone(read[2])

    def test_credential_sql_uses_stdin_with_explicit_exit(self):
        cluster = FakeCluster()
        self.run_install(cluster, apply=True)
        provision = next(event for event in cluster.events if event[0] == 'run' and event[2] and event[2].startswith('CREATE ROLE'))
        self.assertIn('-i', provision[1])
        self.assertIn('-w', provision[1])
        self.assertNotIn('--command', provision[1])
        self.assertTrue(provision[2].endswith('\\quit\n'))
        self.assertNotIn('PASSWORD', ' '.join(provision[1]))

    def test_exec_timeout_has_actionable_error_and_hides_input(self):
        cluster = installer.Cluster(None)
        with patch.object(installer.subprocess, 'run', side_effect=installer.subprocess.TimeoutExpired(['kubectl'], 30)):
            with self.assertRaisesRegex(RuntimeError, 'read-only PostgreSQL preflight'):
                cluster.run('exec', '--', 'psql', '-w', '--command', 'SELECT 1', timeout=30)
            with self.assertRaises(RuntimeError) as error:
                cluster.run('exec', '-i', '--', 'psql', '-w', data='private provisioning SQL', timeout=120)
            self.assertIn('database provisioning', str(error.exception))
            self.assertNotIn('private provisioning SQL', str(error.exception))

    def test_authentication_and_transport_errors_are_distinguished_without_response_bodies(self):
        cluster = installer.Cluster(None)
        for stderr, expected in [('fe_sendauth: no password supplied private-value', 'authentication'),
                                 ('error dialing backend: private-value', 'Kubernetes exec')]:
            with patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr=stderr)):
                with self.assertRaises(RuntimeError) as error:
                    cluster.run('exec', '--', 'psql', '-w')
                self.assertIn(expected, str(error.exception))
                self.assertNotIn('private-value', str(error.exception))
