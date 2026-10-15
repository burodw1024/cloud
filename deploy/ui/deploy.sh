#!/usr/bin/env bash
set -Eeuo pipefail
release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "$release_dir/../.." && pwd)"
cd -- "$release_dir"
namespace=test
deployment=cloud-live-test
container=django
expected_base=192.168.1.224:80/cloud:model-20261014t104535z
release_image="192.168.1.224:80/cloud:ui-$(date -u +%Y%m%dT%H%M%SZ)"
for binary in docker kubectl python3; do
  command -v "$binary" >/dev/null || { echo "Missing required command: $binary" >&2; exit 1; }
done
current_image="$(kubectl -n "$namespace" get deployment "$deployment" -o jsonpath='{.spec.template.spec.containers[?(@.name=="django")].image}')"
if [[ "$current_image" != "$expected_base" ]]; then
  echo "Deployment image changed: $current_image" >&2
  echo 'Stopping to avoid building from the wrong release.' >&2
  exit 1
fi
# Verify the live image uses the source layout this UI package expects.
for template in chat/templates/chat/index.html agentic_ai/templates/agentic_ai/dashboard.html; do
  kubectl -n "$namespace" exec "deployment/$deployment" -c "$container" -- test -f "/app/$template"
done
# Mounted source directories would hide templates baked into the new image.
kubectl -n "$namespace" get deployment "$deployment" -o json > deployment-before.json
python3 - <<'PY'
import json
from pathlib import PurePosixPath
spec=json.load(open('deployment-before.json'))['spec']['template']['spec']
c=next(c for c in spec['containers'] if c['name']=='django')
files=[PurePosixPath(p) for p in ['/app/chat/templates/chat/index.html','/app/chat/templates/chat/workspace_styles.html','/app/agentic_ai/templates/agentic_ai/dashboard.html']]
for mount in c.get('volumeMounts',[]):
    path=PurePosixPath(mount['mountPath'])
    if any(path==f or path in f.parents for f in files):
        raise SystemExit('Source volume mount would hide UI changes: '+str(path)+'. Stop and update the mounted source instead.')
PY
build_context="$(mktemp -d)"
trap 'rm -rf -- "$build_context"' EXIT
cp "$release_dir/Dockerfile" "$build_context/Dockerfile"
for template in chat/templates/chat/index.html chat/templates/chat/workspace_styles.html agentic_ai/templates/agentic_ai/dashboard.html; do
  mkdir -p "$build_context/templates/$(dirname -- "$template")"
  cp "$repo_dir/$template" "$build_context/templates/$template"
done
docker pull "$current_image"
docker build --build-arg "BASE_IMAGE=$current_image" -t "$release_image" "$build_context"
# Confirm the image contains the additional stylesheet template.
docker run --rm --entrypoint python "$release_image" -c 'from pathlib import Path; p=Path("/app/chat/templates/chat/workspace_styles.html"); assert p.is_file() and "BURO workspace" in p.read_text(); print("UI image verified")'
docker push "$release_image"
printf 'Previous image: %s\nNew image: %s\n' "$current_image" "$release_image" > release-images.txt
# Recheck after the build in case another release happened meanwhile.
latest_image="$(kubectl -n "$namespace" get deployment "$deployment" -o jsonpath='{.spec.template.spec.containers[?(@.name=="django")].image}')"
[[ "$latest_image" == "$current_image" ]] || { echo 'Deployment changed during build. Stopping before rollout.' >&2; exit 1; }
kubectl -n "$namespace" set image "deployment/$deployment" "$container=$release_image"
if ! kubectl -n "$namespace" rollout status "deployment/$deployment" --timeout=300s; then
  echo 'Rollout failed. Restoring the previous image.' >&2
  kubectl -n "$namespace" set image "deployment/$deployment" "$container=$current_image"
  kubectl -n "$namespace" rollout status "deployment/$deployment" --timeout=300s
  exit 1
fi
# Verify templates from the running deployment, without modifying live files.
kubectl -n "$namespace" exec "deployment/$deployment" -c "$container" -- python -c 'from pathlib import Path; p=Path("/app/chat/templates/chat/workspace_styles.html"); assert p.is_file() and "BURO workspace" in p.read_text(); print("New UI template present in running container")'
echo "Deployed: $release_image"
echo 'Open your Django URL and hard-refresh the browser.'
echo 'If GitOps/Helm manages this Deployment, persist the new image in its source configuration.'
