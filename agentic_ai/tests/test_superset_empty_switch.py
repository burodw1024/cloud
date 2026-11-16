import json
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch


class EmptySupersetSwitchTests(unittest.TestCase):
    def execute(self, counts=('0', '0'), retained=False):
        helper = runpy.run_path(str(Path(__file__).resolve().parents[2]/'deploy/superset/switch_empty_local.py'))
        database = {'metadata': {'uid': 'pg-uid'}, 'spec': {'replicas': 0 if retained else 1}}
        application = {'metadata': {'uid': 'app-uid'}, 'spec': {'replicas': 1}}
        def replica(uid):
            return {'metadata': {'uid': uid+'-rs', 'ownerReferences': [
                {'kind': 'Deployment', 'uid': uid, 'controller': True}]}}
        def pod(uid, name, phase):
            return {'metadata': {'name': name, 'ownerReferences': [
                {'kind': 'ReplicaSet', 'uid': uid+'-rs', 'controller': True}]},
                'spec': {}, 'status': {'phase': phase}}
        pods = [pod('pg-uid', 'local-postgres', 'Running'), pod('app-uid', 'pending-superset', 'Pending')]
        calls = []
        results = iter(counts)
        def output(command, **kwargs):
            if 'exec' in command:
                return next(results)
            kind = command[command.index('get')+1]
            if kind == 'deployment': return json.dumps(database if 'superset-db' in command else application)
            if kind == 'configmap': return json.dumps({'data': {'database_uid': 'pg-uid'}}) if retained else ''
            if kind == 'secret':
                return '' if 'superset-local-credentials-retained' in command else json.dumps({'data': {'password': 'test-only'}})
            if kind == 'replicasets': return json.dumps({'items': [replica('pg-uid'), replica('app-uid')]})
            if kind == 'pods': return json.dumps({'items': pods})
            raise AssertionError(command)
        def run(command, **kwargs):
            calls.append((command, kwargs))
            if 'wait' in command:
                name = next(x[4:] for x in command if x.startswith('pod/'))
                pods[:] = [p for p in pods if p['metadata']['name'] != name]
            if 'scale' in command:
                (database if 'deployment/superset-db' in command else application)['spec']['replicas'] = 0
        failure = None
        with patch.object(sys, 'argv', ['switch_empty_local.py', '--context', 'mamun', '--switch-empty-local']), \
             patch('subprocess.check_output', side_effect=output), patch('subprocess.run', side_effect=run):
            try:
                helper['main']()
            except RuntimeError as exc:
                failure = exc
        return calls, failure

    def test_empty_database_is_retained_stopped_and_credentials_are_backed_up(self):
        calls, failure = self.execute()
        self.assertIsNone(failure)
        self.assertEqual(sum('scale' in command for command, _ in calls), 2)
        self.assertEqual(sum('wait' in command for command, _ in calls), 2)
        self.assertFalse(any('delete' in command for command, _ in calls))
        backup = next(json.loads(options['input']) for _, options in calls if 'input' in options)
        self.assertEqual(backup['metadata']['name'], 'superset-local-credentials-retained')
        self.assertEqual(backup['data'], {'password': 'test-only'})

    def test_nonempty_database_refuses_before_stopping_anything(self):
        calls, failure = self.execute(counts=('1',))
        self.assertIn('user tables', str(failure))
        self.assertEqual(calls, [])

    def test_table_created_during_drain_blocks_database_switch(self):
        calls, failure = self.execute(counts=('0', '1'))
        self.assertIn('user tables', str(failure))
        self.assertFalse(any('deployment/superset-db' in command for command, _ in calls))

    def test_repeated_install_accepts_matching_retained_database_proof(self):
        calls, failure = self.execute(retained=True)
        self.assertIsNone(failure)
        self.assertEqual(calls, [])
