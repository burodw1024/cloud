#!/usr/bin/env bash
# Run on the Ubuntu Docker/kubectl machine, not inside the application pod.
set -euo pipefail
cd "$(dirname "$0")/.."
for tool in docker kubectl ssh scp sha256sum; do
  command -v "$tool" >/dev/null || { echo "Missing command: $tool" >&2; exit 1; }
done
worker_ssh="${WORKER_SSH:-dw@192.168.1.99}"
image_tag="$(git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)"
image="192.168.1.224/cloud:${image_tag}"
umask 077
artifact_dir="$(mktemp -d /tmp/cloud-image-update.XXXXXX)"
echo "Artifacts and database backup: $artifact_dir"

# Snapshot the live-test SQLite database consistently before replacing its pod.
kubectl exec -n test deployment/cloud-live-test -- python -c '
import sqlite3
from django.conf import settings
import django, os
django.setup()
source = sqlite3.connect(settings.DATABASES["default"]["NAME"])
target = sqlite3.connect("/tmp/cloud-before-update.sqlite3")
source.backup(target)
target.close()
source.close()
'
kubectl exec -n test deployment/cloud-live-test -- python -c \
  'import sys; sys.stdout.buffer.write(open("/tmp/cloud-before-update.sqlite3", "rb").read())' \
  > "$artifact_dir/cloud-before-update.sqlite3"
test -s "$artifact_dir/cloud-before-update.sqlite3"

# Use the updated repository Dockerfile and a new tag on every deployment.
docker build -t "$image" .
docker run --rm --entrypoint python "$image" manage.py check

docker save "$image" -o "$artifact_dir/cloud-image.tar"
(cd "$artifact_dir" && sha256sum cloud-image.tar > cloud-image.tar.sha256)
remote_dir="$(ssh "$worker_ssh" 'mktemp -d /tmp/cloud-image-import.XXXXXX')"
[[ "$remote_dir" =~ ^/tmp/cloud-image-import\.[A-Za-z0-9]+$ ]] || exit 1
scp "$artifact_dir/cloud-image.tar" "$artifact_dir/cloud-image.tar.sha256" "$worker_ssh:$remote_dir/"
ssh -t "$worker_ssh" "cd '$remote_dir' && sha256sum -c cloud-image.tar.sha256 && sudo ctr -n k8s.io images import cloud-image.tar"

# Existing node selector, environment bindings, and test settings are retained.
kubectl set image deployment/cloud-live-test -n test "django=$image"
kubectl rollout status deployment/cloud-live-test -n test --timeout=180s
kubectl exec -n test deployment/cloud-live-test -- python -c '
import urllib.request
for path in ("/", "/login/"):
    response = urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=20)
    assert response.status == 200
    print(path, response.status)
'
echo "Deployed: $image"
echo "Test database/users reset on pod replacement. Previous database saved at: $artifact_dir/cloud-before-update.sqlite3"
echo "MCP mode is not enabled by this script; verify its gateway connection before enabling it."
echo "Image archives retained locally and on the worker for diagnosis; remove them when no longer needed."
