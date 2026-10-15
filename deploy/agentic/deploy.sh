#!/usr/bin/env bash
# Run on the master with Docker registry access and kubectl credentials.
set -Eeuo pipefail
release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "$release_dir/../.." && pwd)"
namespace=test
deployment=cloud-live-test
container=django
for binary in docker kubectl python3; do
  command -v "$binary" >/dev/null || { echo "Missing required command: $binary" >&2; exit 1; }
done
current_image="$(kubectl -n "$namespace" get deployment "$deployment" -o jsonpath='{.spec.template.spec.containers[?(@.name=="django")].image}')"
[[ -n "$current_image" ]] || { echo 'Django container was not found.' >&2; exit 1; }
base_image="${BASE_IMAGE:-$current_image}"
release_image="192.168.1.224:80/cloud:agentic-$(date -u +%Y%m%dT%H%M%SZ)"
context="$(mktemp -d)"
trap 'rm -rf -- "$context"' EXIT
# Inspect mounts without requiring kubelet exec access. No deployment env or
# credentials are exported to the build context.
kubectl -n "$namespace" get deployment "$deployment" -o json > "$context/deployment.json"
python3 - "$repo_dir" "$release_dir/files.txt" "$context" <<'PY'
import hashlib, json, shutil, sys
from pathlib import Path, PurePosixPath
root, manifest, context = map(Path, sys.argv[1:])
files = manifest.read_text().splitlines()
template = json.loads((context/'deployment.json').read_text())['spec']['template']
spec = template['spec']
(context/'template.sha256').write_text(hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest())
container = next(c for c in spec['containers'] if c['name'] == 'django')
replaced_mounts = []
volumes = {v['name']: v for v in spec.get('volumes', [])}
for mount in container.get('volumeMounts', []):
    path = PurePosixPath(mount['mountPath'])
    if any(path == PurePosixPath('/app')/f or path in (PurePosixPath('/app')/f).parents for f in files):
        volume = volumes.get(mount['name'], {})
        if (str(path) == '/app/agentic_ai/orchestrator.py'
                and mount['name'] == 'dashboard-mcp-code'
                and mount.get('subPath') == 'orchestrator.py'
                and volume.get('configMap', {}).get('name', '').startswith('dashboard-mcp-display-')):
            replaced_mounts.append(mount)
            print('Release will replace the orchestrator.py ConfigMap override with image code.')
        else:
            raise SystemExit('A mounted source path would hide this release: '+str(path))
patch_container = {'name': 'django', 'imagePullPolicy': 'IfNotPresent'}
rollback_container = {'name': 'django', 'image': container['image'], 'imagePullPolicy': container.get('imagePullPolicy', 'IfNotPresent')}
if replaced_mounts:
    patch_container['volumeMounts'] = [{'mountPath': m['mountPath'], '$patch': 'delete'} for m in replaced_mounts]
    rollback_container['volumeMounts'] = replaced_mounts
for name, c in [('deploy-patch.json', patch_container), ('rollback-patch.json', rollback_container)]:
    (context/name).write_text(json.dumps({'spec': {'template': {'spec': {'containers': [c]}}}}))
(context/'deployment.json').unlink()
lines = ['ARG BASE_IMAGE', 'FROM ${BASE_IMAGE}']
for name in files:
    target = context/'payload'/name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root/name, target)
    target.chmod(0o644)
    lines.append(f'COPY --chmod=644 payload/{name} /app/{name}')
(context/'Dockerfile').write_text('\n'.join(lines)+'\n')
PY
docker pull "$base_image"
docker build --build-arg "BASE_IMAGE=$base_image" -t "$release_image" "$context"
docker run --rm --entrypoint python "$release_image" -c 'from pathlib import Path; import django, requests; paths=["agentic_ai/agent_runtime.py","agentic_ai/mcp_client.py","agentic_ai/agentic_api.py"]; [compile((Path("/app")/p).read_text(),p,"exec") for p in paths]; assert (Path("/app/openai_policy/templates/openai_policy/base.html")).is_file(); print("Release source checks passed")'
docker push "$release_image"
printf 'Previous image: %s\nNew image: %s\n' "$current_image" "$release_image" > "$release_dir/release-images.txt"
latest_hash="$(kubectl -n "$namespace" get deployment "$deployment" -o json | python3 -c 'import hashlib,json,sys; t=json.load(sys.stdin)["spec"]["template"]; print(hashlib.sha256(json.dumps(t,sort_keys=True).encode()).hexdigest())')"
[[ "$latest_hash" == "$(cat "$context/template.sha256")" ]] || { echo 'Another release changed the pod template during build; stopping.' >&2; exit 1; }
python3 - "$context/deploy-patch.json" "$release_image" <<'PYIMAGE'
import json,sys
from pathlib import Path
path = Path(sys.argv[1]); patch = json.loads(path.read_text())
patch['spec']['template']['spec']['containers'][0]['image'] = sys.argv[2]
path.write_text(json.dumps(patch))
PYIMAGE
# Change image and remove its one obsolete source override in one rollout.
kubectl -n "$namespace" patch deployment "$deployment" --type=strategic --patch-file "$context/deploy-patch.json"
if ! kubectl -n "$namespace" rollout status "deployment/$deployment" --timeout=300s; then
  echo 'Rollout failed. Restoring the previous image and orchestrator mount.' >&2
  kubectl -n "$namespace" patch deployment "$deployment" --type=strategic --patch-file "$context/rollback-patch.json"
  kubectl -n "$namespace" rollout status "deployment/$deployment" --timeout=300s
  exit 1
fi
echo "Deployed: $release_image"
echo 'Persist this image in GitOps/Helm if it manages this workload.'
echo 'Configure and test the ContextForge endpoint and tools before using Agentic AI.'
