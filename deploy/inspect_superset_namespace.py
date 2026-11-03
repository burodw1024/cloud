#!/usr/bin/env python3
"""Read-only deployment inventory with container values and Secret data omitted."""
import argparse
import json
import subprocess


def summarize(document):
    resources = []
    for item in document.get('items', []):
        kind = item.get('kind')
        spec = item.get('spec', {})
        template = spec.get('template', {}).get('spec', {})
        containers = []
        for container in template.get('initContainers', []) + template.get('containers', []):
            references = []
            for value in container.get('env', []):
                for ref_type in ('secretKeyRef', 'configMapKeyRef'):
                    ref = value.get('valueFrom', {}).get(ref_type)
                    if ref:
                        references.append({'variable': value['name'], 'type': ref_type,
                                           'name': ref.get('name'), 'key': ref.get('key')})
            containers.append({'name': container.get('name'), 'image': container.get('image'),
                'resources': container.get('resources', {}), 'environment_references': references,
                'environment_sources': container.get('envFrom', []),
                'literal_environment_variable_names': [value['name'] for value in container.get('env', [])
                                                        if 'value' in value]})
        resources.append({'kind': kind, 'name': item['metadata']['name'],
            'replicas': spec.get('replicas'), 'ready_replicas': item.get('status', {}).get('readyReplicas'),
            'node_selector': template.get('nodeSelector', {}),
            'containers': containers,
            'volumes': [{'name': volume.get('name'),
                         'secret_name': volume.get('secret', {}).get('secretName'),
                         'config_map_name': volume.get('configMap', {}).get('name'),
                         'claim_name': volume.get('persistentVolumeClaim', {}).get('claimName')}
                        for volume in template.get('volumes', [])]})
    return resources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace', default='superset')
    parser.add_argument('--context')
    args = parser.parse_args()
    command = ['kubectl'] + (['--context', args.context] if args.context else [])
    command += ['-n', args.namespace, 'get', 'deployments,statefulsets,jobs', '-o', 'json', '--request-timeout=20s']
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    if result.returncode:
        raise SystemExit('Namespace inventory failed. Check the kubectl context and read permissions locally.')
    print(json.dumps(summarize(json.loads(result.stdout)), indent=2))


if __name__ == '__main__':
    main()
