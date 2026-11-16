"""Prepare a pinned Superset 4.1.2 frontend/image build without touching a cluster."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

UPSTREAM_COMMIT = '72538bda06c0deef2140e0df65bf0a13bcddb16c'
KEY = 'buro_icon_map'


def register(source):
    if KEY in source or 'BuroIconMapPlugin' in source:
        raise ValueError('Icon Map is already registered; start from the clean pinned upstream source.')
    marker = "import { isFeatureEnabled, FeatureFlag, Preset } from '@superset-ui/core';"
    if source.count(marker) != 1 or source.count('plugins: [') != 1:
        raise ValueError('Unexpected MainPreset structure; refuse to patch a different Superset version.')
    return source.replace(marker, marker+"\nimport BuroIconMapPlugin from '../BuroIconMap';").replace(
        'plugins: [', "plugins: [\n        new BuroIconMapPlugin().configure({ key: 'buro_icon_map' }),", 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--upstream', type=Path, help='Optional existing clean 4.1.2 source clone; only committed source is copied.')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if output.exists():
        raise SystemExit('Build directory already exists. Choose a new directory; existing files were not changed.')
    if args.upstream:
        revision = subprocess.check_output(['git', '-C', str(args.upstream), 'rev-parse', 'HEAD'], text=True).strip()
        if revision != UPSTREAM_COMMIT:
            raise SystemExit('The source clone must be exactly Apache Superset 4.1.2: '+UPSTREAM_COMMIT)
    output.mkdir(parents=True)
    upstream = output/'upstream'
    if args.upstream:
        subprocess.run(['git', 'clone', '--no-hardlinks', '--no-checkout', str(args.upstream.resolve()), str(upstream)], check=True)
        subprocess.run(['git', '-C', str(upstream), 'checkout', '--detach', UPSTREAM_COMMIT], check=True)
    else:
        subprocess.run(['git', 'clone', '--depth', '1', '--single-branch', '--branch', '4.1.2',
                        'https://github.com/apache/superset.git', str(upstream)], check=True)
    revision = subprocess.check_output(['git', '-C', str(upstream), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != UPSTREAM_COMMIT:
        raise SystemExit('Upstream revision changed; no patch or build was performed.')
    dependency_patch = root/'deploy/superset/icon-map-dependencies.patch'
    subprocess.run(['git', '-C', str(upstream), 'apply', '--check', str(dependency_patch)], check=True)
    subprocess.run(['git', '-C', str(upstream), 'apply', str(dependency_patch)], check=True)
    frontend = upstream/'superset-frontend'
    plugin = root/'plugins/superset-icon-map'
    shutil.copytree(plugin/'src', frontend/'src/visualizations/BuroIconMap')
    shutil.copy2(root/'deploy/superset/webpack.icon-map.cjs', frontend/'webpack.icon-map.cjs')
    preset = frontend/'src/visualizations/presets/MainPreset.js'
    preset.write_text(register(preset.read_text()))
    shutil.copy2(root/'deploy/superset/Dockerfile.icon-map', output/'Dockerfile')
    shutil.copy2(plugin/'THIRD_PARTY_NOTICES.md', output/'ICON_MAP_NOTICE.txt')
    (output/'.dockerignore').write_text('**/.git\n**/node_modules\n**/.cache\n')
    (output/'icon-map-build.json').write_text(json.dumps({
        'upstream_version': '4.1.2', 'upstream_commit': revision, 'chart_key': KEY,
        'leaflet': '1.9.4', 'postgres_driver': '2.9.10',
    }, indent=2)+'\n')
    print('Prepared Icon Map image build:', output)
    print('Chart key:', KEY)
    print('No Kubernetes resources or database contents were changed.')


if __name__ == '__main__':
    main()
