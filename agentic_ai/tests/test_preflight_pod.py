from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from deploy.pod_ownership import live_pod, preflight_pod


class PreflightPodTests(unittest.TestCase):
    def setUp(self):
        self.deployment = {'metadata': {'uid': 'app'}, 'spec': {'replicas': 1}}
        self.replicasets = {'items': [{'metadata': {'uid': 'rs', 'ownerReferences': [
            {'kind': 'Deployment', 'uid': 'app', 'controller': True}]}}]}
        self.pod = {'metadata': {'name': 'running-app', 'ownerReferences': [
            {'kind': 'ReplicaSet', 'uid': 'rs', 'controller': True}]},
            'spec': {'containers': [{'name': 'django'}, {'name': 'https'}]},
            'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}],
                'containerStatuses': [{'name': 'django', 'ready': True}, {'name': 'https', 'ready': True}]}}

    def test_failed_old_pod_cannot_be_chosen_ahead_of_running_pod(self):
        failed = deepcopy(self.pod)
        failed['metadata']['name'] = 'failed-app'
        failed['status']['phase'] = 'Failed'
        self.assertEqual(live_pod(self.deployment, self.replicasets,
                                 {'items': [failed, self.pod]}), 'running-app')

    def test_failed_only_or_scaled_down_requires_recovery(self):
        failed = deepcopy(self.pod)
        failed['status']['phase'] = 'Failed'
        with self.assertRaisesRegex(ValueError, 'recovery'):
            live_pod(self.deployment, self.replicasets, {'items': [failed]})
        self.deployment['spec']['replicas'] = 0
        with self.assertRaisesRegex(ValueError, 'recovery'):
            live_pod(self.deployment, self.replicasets, {'items': [self.pod]})

    def test_unready_terminating_foreign_and_ambiguous_pods_are_rejected(self):
        for kind in ('unready', 'terminating', 'foreign', 'ambiguous', 'statuses'):
            pod = deepcopy(self.pod)
            if kind == 'unready': pod['status']['conditions'][0]['status'] = 'False'
            if kind == 'terminating': pod['metadata']['deletionTimestamp'] = '2026-11-16T00:00:00Z'
            if kind == 'foreign': pod['metadata']['ownerReferences'][0]['uid'] = 'foreign'
            if kind == 'statuses': pod['status']['containerStatuses'] = []
            pods = [pod, deepcopy(pod)] if kind == 'ambiguous' else [pod]
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                live_pod(self.deployment, self.replicasets, {'items': pods})

    @patch('subprocess.check_output')
    def test_existing_lock_blocks_preflight_without_selecting_pods(self, output):
        output.return_value = json.dumps({'data': {'release': 'pending'}})
        with self.assertRaisesRegex(ValueError, 'release lock'):
            preflight_pod('mamun')
        output.assert_called_once()

    @patch('subprocess.check_output')
    def test_preflight_uses_context_and_verified_pod_ownership(self, output):
        output.side_effect = ['', json.dumps(self.deployment), json.dumps(self.replicasets),
                              json.dumps({'items': [self.pod]})]
        self.assertEqual(preflight_pod('mamun'), 'running-app')
        for call in output.call_args_list:
            self.assertEqual(call.args[0][:5], ['kubectl', '--context', 'mamun', '-n', 'rag'])
