"""Identify app pods by controller UIDs, never by shared labels alone."""
import json
from pathlib import Path
import sys


def deployment_pods(deployment, replicasets, pods, include_terminating=False):
    deployment_uid = deployment['metadata'].get('uid')
    if not deployment_uid:
        raise ValueError('Deployment UID is missing; ownership cannot be verified.')
    replica_uids = {
        rs['metadata']['uid'] for rs in replicasets['items']
        if any(ref.get('kind') == 'Deployment' and ref.get('uid') == deployment_uid
               and ref.get('controller') is True
               for ref in rs['metadata'].get('ownerReferences', []))
    }
    return [pod for pod in pods['items']
            if pod.get('status', {}).get('phase') not in ('Succeeded', 'Failed')
            and (include_terminating or not pod['metadata'].get('deletionTimestamp'))
            and any(ref.get('kind') == 'ReplicaSet' and ref.get('uid') in replica_uids
                    and ref.get('controller') is True
                    for ref in pod['metadata'].get('ownerReferences', []))]


def staged_pods(stage, pod_file='pods.before.json', rs_file='replicasets.before.json', include_terminating=False):
    def read(name): return json.loads((Path(stage) / name).read_text())
    return deployment_pods(read('deployment.before.json'), read(rs_file), read(pod_file), include_terminating)


def describe(pods):
    return '; '.join(f"{p['metadata']['name']} node={p['spec'].get('nodeName', '<unscheduled>')} phase={p.get('status', {}).get('phase', '<unknown>')}"
                     for p in pods) or 'none'


if __name__ == '__main__':
    action, stage = sys.argv[1:3]
    if action == 'drain-count':
        print(len(staged_pods(stage, 'draining-pods.json', include_terminating=True)))
    elif action == 'new-ready':
        pods = staged_pods(stage, 'pods.after.json', 'replicasets.after.json')
        if len(pods) != 1:
            raise SystemExit('Expected one Deployment-owned application pod; found: ' + describe(pods))
        pod = pods[0]
        statuses = pod.get('status', {}).get('containerStatuses', [])
        if (pod.get('status', {}).get('phase') != 'Running'
                or pod['spec'].get('nodeName') != 'worker-99' or len(statuses) != 2
                or not all(c.get('ready') and c.get('restartCount') == 0 for c in statuses)):
            raise SystemExit('Application pod must be on worker-99 and 2/2 Ready with zero restarts: ' + describe(pods))
        print(pod['metadata']['name'])
    else:
        raise SystemExit('Unknown ownership action')
