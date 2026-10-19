import importlib.util
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, RequestFactory
from buro_first import health

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('onprem_manifest', ROOT / 'deploy/onprem_manifest.py')
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


class OnPremManifestTests(SimpleTestCase):
    def test_manifest_pins_worker_preserves_other_services_and_pulls_registry_images(self):
        env = {'APP_NAME': 'buro-django', 'NAMESPACE': 'rag', 'APP_IMAGE': 'docker.io/library/buro-django:test',
               'NGINX_IMAGE': 'nginx:1.28-alpine', 'MEDIA_PVC': 'django-media',
               'RELEASE_ID': 'test-release', 'JOB': 'django-release-test', 'WORKER_NODE': 'worker-99',
               'TLS_SECRET': 'django-tls', 'CREATE_PVC': '1', 'STORAGE_SIZE': '20Gi', 'APP_NODE_PORT': '31135',
               'BOOTSTRAP_SECRET': 'django-bootstrap', 'CHECK_MCP': '0',
               'IMAGE_DELIVERY': 'registry', 'IMAGE_PULL_SECRET': 'registry-credentials'}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env):
            stage = Path(directory)
            (stage / 'public.json').write_text(json.dumps({'host': '192.168.1.144:31135'}))
            manifest.render(stage)
            application = json.loads((stage / 'application.json').read_text())['items']
            deployment, service = application
            pod = deployment['spec']['template']['spec']
            self.assertEqual(service['spec']['ports'][0]['nodePort'], 31135)
            self.assertEqual(service['metadata']['namespace'], 'rag')
            self.assertFalse(pod['automountServiceAccountToken'])
            self.assertIn('worker-99', json.dumps(pod['affinity']))
            for container in pod['containers'] + pod['initContainers']:
                self.assertEqual(container['imagePullPolicy'], 'Always')
            proxy = pod['containers'][1]
            self.assertEqual(proxy['readinessProbe']['httpGet']['scheme'], 'HTTPS')
            self.assertEqual(proxy['readinessProbe']['httpGet']['path'], '/nginx-live/')
            self.assertIn('/healthz/ready/', json.dumps(pod['containers'][0]['readinessProbe']['exec']))
            self.assertNotIn('open-webui', json.dumps(application))
            job = json.loads((stage / 'migration-job.json').read_text())['items'][0]
            self.assertEqual(job['spec']['backoffLimit'], 0)
            self.assertEqual(job['spec']['template']['spec']['initContainers'][0]['name'], 'database-backup')
            self.assertIn('SQLite backup completed.', json.dumps(job))
            self.assertEqual(deployment['spec']['strategy'], {'type': 'Recreate'})
            self.assertIn('/data', json.dumps(pod['containers'][0]['volumeMounts']))
            for workload in (pod, job['spec']['template']['spec']):
                self.assertEqual(workload['imagePullSecrets'], [{'name': 'registry-credentials'}])
                for container in workload['containers'] + workload['initContainers']:
                    self.assertEqual(container['imagePullPolicy'], 'Always')
            with patch.dict(os.environ, {'STORAGE_MODE': 'hostpath'}):
                manifest.render(stage)
                host_deploy = json.loads((stage / 'application.json').read_text())['items'][0]['spec']['template']['spec']
                host_job = json.loads((stage / 'migration-job.json').read_text())['items'][0]['spec']['template']['spec']
                for spec in (host_deploy, host_job):
                    data = next(v for v in spec['volumes'] if v['name'] == 'media')
                    self.assertEqual(data['hostPath']['path'], '/opt/buro-ai/buro-django')
                    self.assertNotIn('persistentVolumeClaim', data)
                self.assertEqual(host_job['initContainers'][0]['name'], 'prepare-django-data')
                self.assertNotIn('envFrom', host_job['initContainers'][0])
                self.assertNotIn('0777', json.dumps(host_job))
                storage = json.loads((stage / 'storage-and-config.json').read_text())['items']
                self.assertFalse(any(o['kind'] == 'PersistentVolumeClaim' for o in storage))
            with patch.dict(os.environ, {'IMAGE_DELIVERY': 'copy'}):
                manifest.render(stage)
                copied = json.loads((stage / 'application.json').read_text())['items'][0]['spec']['template']['spec']
                for container in copied['containers'] + copied['initContainers']:
                    self.assertEqual(container['imagePullPolicy'], 'Never')

    def test_bootstrap_password_excluded_from_application_environment(self):
        env = {'DJANGO_SECRET_KEY': 'x' * 64, 'DJANGO_ALLOWED_HOSTS': '192.168.1.144',
               'DJANGO_SUPERUSER_PASSWORD': 'bootstrap-test-only',
               'DJANGO_SUPERUSER_USERNAME': 'admin'}
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            source = stage / 'private.env'
            source.write_text(''.join(f'{key}={value}\n' for key, value in env.items()))
            source.chmod(0o600)
            with patch.dict(os.environ, {'ENV_FILE': str(source), 'PUBLIC_URL': 'https://192.168.1.144:31135'}):
                manifest.prepare(stage)
            self.assertNotIn('DJANGO_SUPERUSER_PASSWORD', (stage / 'app.env').read_text())
            self.assertIn('DJANGO_SUPERUSER_PASSWORD', (stage / 'bootstrap.env').read_text())
            self.assertIn('OLLAMA_URL=http://192.168.1.144:31134', (stage / 'app.env').read_text())


class ProbeTests(TestCase):
    def test_liveness_and_readiness(self):
        request = RequestFactory().get('/healthz/ready/')
        self.assertEqual(health.live(request).status_code, 200)
        self.assertEqual(health.ready(request).status_code, 200)

    def test_readiness_failure_does_not_expose_credentials(self):
        with patch.object(health.cache, 'set', side_effect=RuntimeError('private-password')):
            response = health.ready(RequestFactory().get('/healthz/ready/'))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-password', response.content.decode())


class RegistryServiceTests(SimpleTestCase):
    def test_discovers_registry_service_on_datalake_not_pod_ip(self):
        pod = {'metadata': {'name': 'docker-registry-example', 'labels': {'app': 'docker-registry'}},
               'spec': {'nodeName': 'datalake'},
               'status': {'phase': 'Running', 'podIP': '172.25.25.13', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        svc = {'metadata': {'name': 'docker-registry', 'namespace': 'registry'},
               'spec': {'selector': {'app': 'docker-registry'}, 'ports': [{'port': 5000, 'nodePort': 30500}]}}
        node = {'metadata': {'name': 'datalake'}, 'status': {'addresses': [{'type': 'InternalIP', 'address': '192.168.1.67'}]}}
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            for name, items in [('registry-pods.json', [pod]), ('services.json', [svc]), ('nodes.json', [node])]:
                (stage / name).write_text(json.dumps({'items': items}))
            self.assertEqual(manifest.discover_registry(stage), '192.168.1.67:30500')
            svc['spec']['ports'][0]['port'] = 80
            svc['status'] = {'loadBalancer': {'ingress': [{'ip': '192.168.1.224'}]}}
            (stage / 'services.json').write_text(json.dumps({'items': [svc]}))
            self.assertEqual(manifest.discover_registry(stage), '192.168.1.224:80')
            svc['spec']['ports'][0].pop('nodePort')
            (stage / 'services.json').write_text(json.dumps({'items': [svc]}))
            self.assertEqual(manifest.discover_registry(stage), '192.168.1.224:80')
            svc.pop('status')
            (stage / 'services.json').write_text(json.dumps({'items': [svc]}))
            with self.assertRaises(SystemExit):
                manifest.discover_registry(stage)

    def test_registry_address_cannot_be_ollama_url_or_include_credentials(self):
        manifest.validate_registry('192.168.1.67:30500')
        for address in ['192.168.1.144:31134', 'http://192.168.1.67:30500', 'user:password@host:5000', 'host:99999']:
            with self.assertRaises(SystemExit):
                manifest.validate_registry(address)
