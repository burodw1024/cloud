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
import json, shutil, sys
from pathlib import Path, PurePosixPath
root, manifest, context = map(Path, sys.argv[1:])
files = manifest.read_text().splitlines()
spec = json.loads((context/'deployment.json').read_text())['spec']['template']['spec']
container = next(c for c in spec['containers'] if c['name'] == 'django')
for mount in container.get('volumeMounts', []):
    path = PurePosixPath(mount['mountPath'])
    if any(path == PurePosixPath('/app')/f or path in (PurePosixPath('/app')/f).parents for f in files):
        raise SystemExit('A mounted source path would hide this release: '+str(path))
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
latest="$(kubectl -n "$namespace" get deployment "$deployment" -o jsonpath='{.spec.template.spec.containers[?(@.name=="django")].image}')"
[[ "$latest" == "$current_image" ]] || { echo 'Another release changed the image during build; stopping.' >&2; exit 1; }
kubectl -n "$namespace" patch deployment "$deployment" --type=strategic \
  -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"$container\",\"image\":\"$release_image\",\"imagePullPolicy\":\"IfNotPresent\"}]}}}}"
if ! kubectl -n "$namespace" rollout status "deployment/$deployment" --timeout=300s; then
  echo 'Rollout failed. Restoring the previous image.' >&2
  kubectl -n "$namespace" set image "deployment/$deployment" "$container=$current_image"
  kubectl -n "$namespace" rollout status "deployment/$deployment" --timeout=300s
  exit 1
fi
echo "Deployed: $release_image"
echo 'Persist this image in GitOps/Helm if it manages this workload.'
echo 'Configure and test the ContextForge endpoint and tools before using Agentic AI.'
