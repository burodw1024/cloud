"""Run under worker sudo: match containerd manifests to expected Docker config IDs."""
import json
import subprocess
import sys

PREFIX = ['ctr', '--address', '/run/containerd/containerd.sock', '--namespace', 'k8s.io']


def config_digest(digest, read, depth=0):
    if depth > 4:
        raise ValueError('Image index nesting exceeds limit')
    document = json.loads(read(digest))
    if 'config' in document:
        return document['config']['digest']
    manifests = [x for x in document.get('manifests', [])
                 if x.get('platform', {}).get('os') == 'linux'
                 and x.get('platform', {}).get('architecture') == 'amd64']
    if len(manifests) != 1:
        raise ValueError('Cannot identify one linux/amd64 image manifest')
    return config_digest(manifests[0]['digest'], read, depth + 1)


def main():
    records = {}
    for line in subprocess.check_output(PREFIX + ['images', 'ls'], text=True).splitlines()[1:]:
        values = line.split()
        if len(values) >= 3:
            records[values[0]] = values[2]
    for reference, expected in zip(sys.argv[1::2], sys.argv[2::2]):
        actual = config_digest(records[reference], lambda digest: subprocess.check_output(PREFIX + ['content', 'get', digest]))
        if actual != expected:
            raise ValueError('Imported image config differs from built image: ' + reference)
        print('Verified imported image:', reference, 'config=' + actual)


if __name__ == '__main__':
    main()
