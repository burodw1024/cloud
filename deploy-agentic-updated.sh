#!/usr/bin/env bash
# Build reviewed GitHub source and deploy through the registry and worker import.
set -Eeuo pipefail
set +x
umask 077
export KUBE_CONTEXT="${KUBE_CONTEXT:-mamun}"
export NAMESPACE=rag
UPDATE_CONTEXTFORGE_TIMEOUTS="${UPDATE_CONTEXTFORGE_TIMEOUTS:-0}"
[[ "$UPDATE_CONTEXTFORGE_TIMEOUTS" == 0 || "$UPDATE_CONTEXTFORGE_TIMEOUTS" == 1 ]] || { echo "UPDATE_CONTEXTFORGE_TIMEOUTS must be 0 or 1" >&2; exit 1; }
RELEASE_ID="$(date -u +%Y%m%d%H%M%S)-$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
export RELEASE_ID
export MIGRATION_JOB="buro-django-release-$RELEASE_ID"
export APP_IMAGE="192.168.1.224:80/buro-django:$RELEASE_ID"
export NGINX_IMAGE="192.168.1.224:80/buro-nginx:$RELEASE_ID"
TLS_CERT="${PUBLIC_CA_FILE:-${TLS_CERT:-$HOME/.config/agentic/tls/tls.crt}}"
TLS_SECRET="${TLS_SECRET:-buro-django-tls}"
WORKER_SSH=dw@192.168.1.99
PUBLIC_URL=https://192.168.1.144:31135
NGINX_SOURCE=docker.io/library/nginx@sha256:ec4ed8b5299e5e90694af7750eb6dffd2627317d30544d056b0371f8082f7bce
LOCK_NAME=buro-django-release-lock
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_COMMIT="$(python3 "$SOURCE_DIR/deploy/source_checkout.py" "$SOURCE_DIR")"
export SOURCE_DIR SOURCE_COMMIT
if [[ "${1:-}" == --check-source ]]; then
  printf 'Verified GitHub source commit: %s\n' "$SOURCE_COMMIT"
  exit 0
fi
[[ "$#" == 0 ]] || { echo 'Usage: bash deploy-agentic-updated.sh [--check-source]' >&2; exit 1; }
LOCKED=0
WRITERS_STOPPED=0
MIGRATION_STARTED=0
SUCCEEDED=0
k() { kubectl --context "$KUBE_CONTEXT" -n rag "$@"; }
kg() { kubectl --context "$KUBE_CONTEXT" -n buro-agentic "$@"; }
fail() { echo "ERROR: $*" >&2; exit 1; }
for tool in git docker kubectl python3 tar gzip sha256sum ssh scp curl openssl; do
  command -v "$tool" >/dev/null || fail "Missing required command: $tool"
done
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
[[ "$TLS_SECRET" == buro-django-tls ]] || fail "Unexpected TLS Secret; existing buro-django-tls must be preserved."
[[ "$KUBE_CONTEXT" == mamun ]] || fail 'This release is prepared for context mamun; no other context is selected automatically.'
[[ -t 0 && -t 1 ]] || fail 'Run in an interactive terminal for Git/registry/SSH credentials and worker sudo.'
[[ -r "$TLS_CERT" ]] || fail "Public TLS certificate missing: $TLS_CERT"
# This updater clones the existing Deployment and retains env/envFrom/Secrets.
# It never builds a replacement production Secret from a local env file.
k exec deployment/buro-django -c django -- python manage.py shell -c \
  'from django.conf import settings; assert settings.SECRET_KEY and settings.ALLOWED_HOSTS, "Existing Django runtime configuration is incomplete"; print("Existing Kubernetes-backed Django configuration verified; secret values were not exported.")'
docker info >/dev/null
docker buildx version >/dev/null
k get namespace rag >/dev/null
k get secret "$TLS_SECRET" >/dev/null
echo 'Using reviewed GitHub source directly; Gitea is not part of this deployment.'
RELEASE_DIR="$(mktemp -d "$PWD/agentic-release.XXXXXXXX")"
STAGE="$RELEASE_DIR/private"
PROJECT="$RELEASE_DIR/project"
mkdir -m 700 "$STAGE" "$PROJECT"
export RELEASE_DIR STAGE PROJECT
echo "Release: $RELEASE_ID"
echo "Private snapshots and build source: $RELEASE_DIR"
# shellcheck disable=SC2329 # Invoked by the EXIT trap.
cleanup() {
  local rc=$?
  trap - EXIT
  if [[ "$SUCCEEDED" == 1 ]]; then
    if [[ "$(k get configmap "$LOCK_NAME" -o jsonpath='{.data.release}' 2>/dev/null)" == "$RELEASE_ID" ]]; then
      k delete configmap "$LOCK_NAME" >/dev/null || true
    fi
  elif [[ "$LOCKED" == 1 ]]; then
    if [[ "$MIGRATION_STARTED" == 0 ]]; then
      if [[ "$WRITERS_STOPPED" == 1 ]]; then
        k scale deployment buro-django --replicas=1 >/dev/null || true
      fi
      if [[ "$(k get configmap "$LOCK_NAME" -o jsonpath='{.data.release}' 2>/dev/null)" == "$RELEASE_ID" ]]; then
        k delete configmap "$LOCK_NAME" >/dev/null || true
      fi
    else
      echo 'Release incomplete after migration dispatch. Lock retained; do not rerun blindly or automatically restore SQLite.' >&2
      echo "Inspect: kubectl --context mamun -n rag get job $MIGRATION_JOB" >&2
      echo "Inspect: kubectl --context mamun -n rag get pods -l job-name=$MIGRATION_JOB" >&2
      echo "Recovery snapshots: $STAGE ; SQLite backups: /opt/buro-ai/buro-django/deployment-backups/$RELEASE_ID" >&2
    fi
  fi
  if [[ "$rc" != 0 ]]; then
    echo 'No claim is made that Oracle/JDBC timeouts or NiFi disconnect errors have been resolved.' >&2
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# An existing lock is always inspected, never deleted automatically.
k get configmap "$LOCK_NAME" --ignore-not-found -o json > "$STAGE/existing-lock.json"
if [[ -s "$STAGE/existing-lock.json" ]]; then
  OLD_RELEASE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("data",{}).get("release",""))' "$STAGE/existing-lock.json")"
  OLD_JOB="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])).get("data",{}); print(d.get("job") or "buro-django-release-"+d.get("release",""))' "$STAGE/existing-lock.json")"
  [[ "$OLD_JOB" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || fail 'Existing lock has an invalid Job reference; inspect it manually.'
  echo "Existing release lock: $OLD_RELEASE ; matching Job: $OLD_JOB"
  k get job "$OLD_JOB" --ignore-not-found -o wide
  k get pods -l "job-name=$OLD_JOB" -o wide
  fail 'Another release lock exists. Its Job and pods were inspected above; resolve it explicitly before a new release.'
fi
k create configmap "$LOCK_NAME" --from-literal="release=$RELEASE_ID" --from-literal="job=$MIGRATION_JOB" >/dev/null
LOCKED=1
k get deployment buro-django -o json > "$STAGE/deployment.before.json"
k get services -o json > "$STAGE/services.before.json"
python3 - "$STAGE/services.before.json" <<'WEBUI'
import json,sys
assert any(p.get('nodePort')==30888 for s in json.load(open(sys.argv[1]))['items']
           for p in s.get('spec',{}).get('ports',[])), 'Expected Open WebUI service on 30888 was not found.'
print('Existing Open WebUI service on 30888 recorded for preservation.')
WEBUI
k get node worker-99 -o json > "$STAGE/node.json"
SELECTOR="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(",".join(k+"="+v for k,v in d["spec"]["selector"]["matchLabels"].items()))' "$STAGE/deployment.before.json")"
[[ -n "$SELECTOR" ]] || fail 'Empty deployment selector.'
k get replicasets -l "$SELECTOR" -o json > "$STAGE/replicasets.before.json"
k get pods -l "$SELECTOR" -o json > "$STAGE/pods.before.json"
k get pods -o json > "$STAGE/all-pods.before.json"
k get cronjobs -o json > "$STAGE/cronjobs.before.json"
kg get deployment contextforge -o json > "$STAGE/contextforge.before.json"
k get configmap buro-django-router-fix --ignore-not-found -o json > "$STAGE/router-fix.before.json"
k get secret "$TLS_SECRET" -o 'jsonpath={.data.tls\.crt}' | python3 -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' > "$STAGE/public-tls.crt"
openssl x509 -in "$TLS_CERT" -noout -fingerprint -sha256 > "$STAGE/local-cert.fingerprint"
openssl x509 -in "$STAGE/public-tls.crt" -noout -fingerprint -sha256 > "$STAGE/cluster-cert.fingerprint"
cmp -s "$STAGE/local-cert.fingerprint" "$STAGE/cluster-cert.fingerprint" || fail 'The supplied public certificate differs from the existing TLS Secret.'
curl --fail --silent --show-error --cacert "$TLS_CERT" --connect-timeout 5 --max-time 20 "$PUBLIC_URL/healthz/ready/" >/dev/null
# Build only committed cloud source; no local edits or code overlay enter the image.
git -C "$SOURCE_DIR" archive "$SOURCE_COMMIT" | tar -x -C "$PROJECT"
cd "$PROJECT"
echo "Building pulled cloud commit: $SOURCE_COMMIT"
python3 deploy/release_manifest.py validate "$STAGE"
LIVE_POD="$(cat "$STAGE/pod-name")"
NGINX_CM="$(cat "$STAGE/nginx-name")"
k get configmap "$NGINX_CM" -o json > "$STAGE/nginx.before.json"
k exec "$LIVE_POD" -c django -- cat /app/agentic_ai/mcp_gateway.py > "$STAGE/live-mcp_gateway.py"
python3 deploy/verify_gateway_source.py "$STAGE/live-mcp_gateway.py" "$PROJECT"
# Inspect retained ConfigMap code as well, even when it is no longer mounted.
if [[ -s "$STAGE/router-fix.before.json" ]]; then
  python3 - "$STAGE" <<'OVERLAY'
import json,sys
from pathlib import Path
stage=Path(sys.argv[1])
cm=json.loads((stage/'router-fix.before.json').read_text())
source=cm.get('data',{}).get('mcp_gateway.py')
assert isinstance(source,str) and source, 'Unrecognized router-fix ConfigMap; review before release.'
(stage/'configmap-mcp_gateway.py').write_text(source)
OVERLAY
  python3 deploy/verify_gateway_source.py "$STAGE/configmap-mcp_gateway.py" "$PROJECT"
fi
# Check for any other known Kubernetes writer to the same HostPath.
python3 - "$STAGE" <<'PY'
import json,sys
from pathlib import Path
from deploy.pod_ownership import staged_pods
s=Path(sys.argv[1]);known=staged_pods(s, include_terminating=True)
names={p['metadata']['name'] for p in known}
def uses_data(spec):
    return any(v.get('hostPath',{}).get('path')=='/opt/buro-ai/buro-django' for v in spec.get('volumes',[]))
for pod in json.loads((s/'all-pods.before.json').read_text())['items']:
    if pod['metadata']['name'] not in names and pod.get('status',{}).get('phase') in ('Running','Pending') and uses_data(pod['spec']):
        raise SystemExit('Another active pod uses production SQLite: '+pod['metadata']['name'])
for cron in json.loads((s/'cronjobs.before.json').read_text())['items']:
    if not cron['spec'].get('suspend') and uses_data(cron['spec']['jobTemplate']['spec']['template']['spec']):
        raise SystemExit('An active CronJob may write production SQLite: '+cron['metadata']['name'])
PY
# A Django feature deployment preserves the gateway by default. Updating its
# timeout is a separate, explicit operation requiring a compatible gateway image.
if [[ "$UPDATE_CONTEXTFORGE_TIMEOUTS" == 1 ]]; then
# ContextForge configuration is checked before stopping Django writers.
GATEWAY_CONTAINER="$(python3 - "$STAGE/contextforge.before.json" <<'PY'
import json,sys
containers=json.load(open(sys.argv[1]))['spec']['template']['spec']['containers']
candidates=[c for c in containers if 'contextforge' in c.get('image','').lower() or 'mcpgateway' in c.get('image','').lower()]
if len(candidates)!=1:
    assert len(containers)==1, 'Cannot identify the ContextForge container safely'
    candidates=containers
print(candidates[0]['name'])
PY
)"
[[ "$GATEWAY_CONTAINER" =~ ^[a-z0-9][a-z0-9-]*$ ]] || fail 'Invalid gateway container name.'
GATEWAY_SELECTOR="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(",".join(k+"="+v for k,v in d["spec"]["selector"]["matchLabels"].items()))' "$STAGE/contextforge.before.json")"
kg get pods -l "$GATEWAY_SELECTOR" -o json > "$STAGE/contextforge-pods.json"
GATEWAY_POD="$(python3 -c 'import json,sys; p=[p for p in json.load(open(sys.argv[1]))["items"] if not p["metadata"].get("deletionTimestamp") and all(c.get("ready") for c in p.get("status",{}).get("containerStatuses",[]))]; assert p; print(p[0]["metadata"]["name"])' "$STAGE/contextforge-pods.json")"
# Probe in a temporary process before changing the Deployment. v1.0.10 caps
# this field at 600; testing the override must not crash the working gateway.
if ! kg exec "$GATEWAY_POD" -c "$GATEWAY_CONTAINER" -- env HTTPX_READ_TIMEOUT=1500 \
  python -c 'from mcpgateway.config import settings; assert float(settings.httpx_read_timeout) == 1500' \
  > "$STAGE/contextforge-timeout-preflight.log" 2>&1; then
  fail "The running ContextForge image does not accept HTTPX_READ_TIMEOUT=1500. No gateway deployment change was made. Inspect $STAGE/contextforge-timeout-preflight.log and deploy a compatible image first; see deploy/CONTEXTFORGE_TIMEOUT_COMPATIBILITY.md."
fi
kg set env deployment/contextforge --containers="$GATEWAY_CONTAINER" HTTPX_READ_TIMEOUT=1500 >/dev/null
kg rollout status deployment/contextforge --timeout=600s
GATEWAY_SELECTOR="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(",".join(k+"="+v for k,v in d["spec"]["selector"]["matchLabels"].items()))' "$STAGE/contextforge.before.json")"
kg get pods -l "$GATEWAY_SELECTOR" -o json > "$STAGE/contextforge-pods.json"
GATEWAY_POD="$(python3 -c 'import json,sys; p=[p for p in json.load(open(sys.argv[1]))["items"] if not p["metadata"].get("deletionTimestamp") and all(c.get("ready") for c in p.get("status",{}).get("containerStatuses",[]))]; assert p; print(p[0]["metadata"]["name"])' "$STAGE/contextforge-pods.json")"
kg exec "$GATEWAY_POD" -c "$GATEWAY_CONTAINER" -- python -c 'from mcpgateway.config import settings; value=float(settings.httpx_read_timeout); assert value == 1500, "ContextForge effective HTTPX timeout differs from requested value"; print("ContextForge effective HTTPX read timeout:",value)'
else
  echo 'Preserving existing ContextForge image and timeout configuration. Gateway timeout upgrade was not requested.'
fi
# The committed Docker ignore rules exclude env files, data, certificates and Git metadata.
docker buildx build --platform linux/amd64 --load --tag "$APP_IMAGE" --label "org.opencontainers.image.revision=$SOURCE_COMMIT" .
# Run isolated application tests inside the actual image, without production env/data/network.
docker run --rm --network none --tmpfs /tmp:rw,mode=1777 -e DJANGO_SETTINGS_MODULE=agentic_ai.tests.settings "$APP_IMAGE" python -m django test agentic_ai.tests --verbosity 1
if ! docker image inspect "$NGINX_SOURCE" >/dev/null 2>&1; then
  docker pull --platform linux/amd64 "$NGINX_SOURCE"
fi
docker tag "$NGINX_SOURCE" "$NGINX_IMAGE"
for image in "$APP_IMAGE" "$NGINX_IMAGE"; do
  [[ "$(docker image inspect --format '{{.Architecture}}' "$image")" == amd64 ]] || fail "Wrong image architecture: $image"
  if ! docker push "$image" > "$STAGE/registry-push.log" 2>&1; then
    if grep -Eiq 'unauthorized|authentication required|denied' "$STAGE/registry-push.log"; then
      echo 'Registry authentication required; Docker will prompt privately.'
      docker login 192.168.1.224:80
      docker push "$image"
    else
      cat "$STAGE/registry-push.log" >&2
      fail "Registry push failed: $image"
    fi
  else
    cat "$STAGE/registry-push.log"
  fi
done
APP_CONFIG_ID="$(docker image inspect --format '{{.Id}}' "$APP_IMAGE")"
NGINX_CONFIG_ID="$(docker image inspect --format '{{.Id}}' "$NGINX_IMAGE")"
docker save "$APP_IMAGE" "$NGINX_IMAGE" | gzip -1 > "$STAGE/images.tar.gz"
(cd "$STAGE" && sha256sum images.tar.gz > images.tar.gz.sha256)
REMOTE_DIR="/tmp/agentic-$RELEASE_ID"
# shellcheck disable=SC2029 # REMOTE_DIR is generated locally from a restricted release ID.
ssh "$WORKER_SSH" "umask 077; mkdir '$REMOTE_DIR'"
scp "$STAGE/images.tar.gz" "$STAGE/images.tar.gz.sha256" deploy/verify_imported_images.py "$WORKER_SSH:$REMOTE_DIR/"
cat > "$STAGE/import-images.sh" <<IMPORT
#!/usr/bin/env bash
set -Eeuo pipefail
cd '$REMOTE_DIR'
sha256sum -c images.tar.gz.sha256
gzip -dc images.tar.gz > images.tar
ctr --address /run/containerd/containerd.sock --namespace k8s.io images import images.tar
for ref in '$APP_IMAGE' '$NGINX_IMAGE'; do
  ctr --address /run/containerd/containerd.sock --namespace k8s.io images ls -q | grep -Fx -- "\$ref" >/dev/null
done
python3 verify_imported_images.py '$APP_IMAGE' '$APP_CONFIG_ID' '$NGINX_IMAGE' '$NGINX_CONFIG_ID'
rm images.tar
printf '%s\n' 'Exact app and Nginx references are present in worker containerd.'
IMPORT
scp "$STAGE/import-images.sh" "$WORKER_SSH:$REMOTE_DIR/import-images.sh"
echo 'Importing on worker-99; sudo may ask for the worker password.'
ssh -t "$WORKER_SSH" "sudo bash '$REMOTE_DIR/import-images.sh'"
python3 deploy/release_manifest.py render "$STAGE"
# Kubernetes validates the exact replacement before downtime.
k apply --dry-run=server -f "$STAGE/nginx.after.json" >/dev/null
k apply --dry-run=server -f "$STAGE/migration.json" >/dev/null
k apply --dry-run=server -f "$STAGE/deployment.after.json" >/dev/null
echo 'Images imported and replacement validated. Stopping application writers for SQLite backup and migration.'
k scale deployment buro-django --replicas=0 >/dev/null
WRITERS_STOPPED=1
DRAIN_DEADLINE=$((SECONDS+2100))
while true; do
  k get pods -l "$SELECTOR" -o json > "$STAGE/draining-pods.json"
  POD_COUNT="$(python3 deploy/pod_ownership.py drain-count "$STAGE")"
  [[ "$POD_COUNT" != 0 ]] || break
  [[ "$SECONDS" -lt "$DRAIN_DEADLINE" ]] || fail 'Old application pods did not fully terminate; migration was not started.'
  sleep 5
done
# Job creation is treated as dispatch even if the client loses its response.
MIGRATION_STARTED=1
k create -f "$STAGE/migration.json" >/dev/null
JOB_DEADLINE=$((SECONDS+2150))
while true; do
  k get job "$MIGRATION_JOB" -o json > "$STAGE/migration.status.json"
  JOB_STATE="$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])).get("status",{}); print("complete" if s.get("succeeded",0) else "failed" if s.get("failed",0) or any(c["type"]=="Failed" and c["status"]=="True" for c in s.get("conditions",[])) else "running")' "$STAGE/migration.status.json")"
  [[ "$JOB_STATE" != failed ]] || fail "Migration failed. Inspect logs for job/$MIGRATION_JOB; lock retained."
  [[ "$JOB_STATE" != complete ]] || break
  [[ "$SECONDS" -lt "$JOB_DEADLINE" ]] || fail 'Migration deadline reached. Inspect the existing Job before any retry.'
  sleep 5
done
k logs "job/$MIGRATION_JOB" -c migrate --tail=30
k apply -f "$STAGE/nginx.after.json" >/dev/null
k apply -f "$STAGE/deployment.after.json" >/dev/null
k rollout status deployment/buro-django --timeout=900s
k get replicasets -l "$SELECTOR" -o json > "$STAGE/replicasets.after.json"
k get pods -l "$SELECTOR" -o json > "$STAGE/pods.after.json"
NEW_POD="$(python3 deploy/pod_ownership.py new-ready "$STAGE")"
k exec "$NEW_POD" -c django -- python /app/deploy/runtime_verify.py
LOCAL_GATEWAY_SHA="$(sha256sum agentic_ai/mcp_gateway.py | cut -d ' ' -f1)"
RUNNING_GATEWAY_SHA="$(k exec "$NEW_POD" -c django -- python -c 'import hashlib; print(hashlib.sha256(open("/app/agentic_ai/mcp_gateway.py","rb").read()).hexdigest())')"
[[ "$LOCAL_GATEWAY_SHA" == "$RUNNING_GATEWAY_SHA" ]] || fail 'Running gateway code differs from the reviewed baked-in replacement.'
k exec "$NEW_POD" -c https -- nginx -t
k exec "$NEW_POD" -c https -- cat /etc/nginx/nginx.conf > "$STAGE/nginx.running.conf"
python3 - "$STAGE/nginx.running.conf" <<'PY'
import re,sys
s=open(sys.argv[1]).read()
for key in ('proxy_read_timeout','proxy_send_timeout'):
    values=re.findall(r'\b'+key+r'\s+([^;]+);',s)
    assert values and all(x.strip()=='1900s' for x in values)
print('Running Nginx read/send timeouts verified at 1900s.')
PY
curl --fail --silent --show-error --cacert "$TLS_CERT" --connect-timeout 5 --max-time 30 "$PUBLIC_URL/healthz/live/" >/dev/null
curl --fail --silent --show-error --cacert "$TLS_CERT" --connect-timeout 5 --max-time 30 "$PUBLIC_URL/healthz/ready/" >/dev/null
# Keep the old overlay ConfigMap for rollback; the new Deployment no longer mounts it.
k get services -o json > "$STAGE/services.after.json"
python3 - "$STAGE" <<'SERVICES'
import json,sys
from pathlib import Path
stage=Path(sys.argv[1])
def webui(name):
    return [(x['metadata']['uid'],x['spec']) for x in json.loads((stage/name).read_text())['items']
            if any(p.get('nodePort')==30888 for p in x.get('spec',{}).get('ports',[]))]
before=webui('services.before.json');after=webui('services.after.json')
assert before and before==after, 'Open WebUI service on 30888 could not be verified unchanged.'
print('Open WebUI service on 30888 preserved unchanged.')
SERVICES
SUCCEEDED=1
echo 'Deployment and HTTPS readiness verified. Existing gateway overlay ConfigMap retained, unmounted.'
k get pods -l "$SELECTOR" -o wide
printf '\nURL: %s\nRelease: %s\nSnapshots: %s\n' "$PUBLIC_URL" "$RELEASE_ID" "$STAGE"
echo 'Deployment verified. Running one authenticated HTTPS report test; no automatic report retries.'
REPORT_RC=0
python3 deploy/verify_report_flow.py --url "$PUBLIC_URL" --cert "$TLS_CERT" --stage "$STAGE" || REPORT_RC=$?
python3 - "$STAGE" "$REPORT_RC" "$PUBLIC_URL" "$RELEASE_ID" "$SOURCE_COMMIT" <<'RESULTS'
import json,sys
from pathlib import Path
stage=Path(sys.argv[1]);report=stage/'report-verification.json'
summary={'deployment_verified':True,'https_readiness_verified':True,'pod_ready':'2/2',
         'url':sys.argv[3]+'/agentic/','release':sys.argv[4],'source_commit':sys.argv[5],
         'report':json.loads(report.read_text()) if report.exists() else {'verified':False,'error':'verification interrupted'}}
(stage/'release-results.json').write_text(json.dumps(summary,indent=2))
print('EXACT RELEASE RESULTS:',json.dumps(summary))
RESULTS
echo 'Deployment does not resolve Oracle/JDBC SQLTimeoutException, ORA-01013, or NiFi HTTP-context errors.'
exit "$REPORT_RC"
