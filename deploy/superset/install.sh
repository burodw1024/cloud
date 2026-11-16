#!/usr/bin/env bash
set -Eeuo pipefail
set +x
export KUBE_CONTEXT="${KUBE_CONTEXT:-mamun}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
k() { kubectl --context "$KUBE_CONTEXT" "$@"; }

for tool in kubectl python3; do
  command -v "$tool" >/dev/null || { echo "Missing command: $tool" >&2; exit 1; }
done
k get node superset -o json | python3 -c '
import json,sys
node=json.load(sys.stdin)
assert node["metadata"]["labels"].get("kubernetes.io/hostname")=="superset", "Unexpected hostname label"
assert any(a["type"]=="InternalIP" and a["address"]=="192.168.1.176" for a in node["status"]["addresses"]), "Node superset is not 192.168.1.176"
assert any(c["type"]=="Ready" and c["status"]=="True" for c in node["status"]["conditions"]), "Superset node is not Ready"
'
k get services -A -o json | python3 -c '
import json,sys
for service in json.load(sys.stdin)["items"]:
    if any(p.get("nodePort")==32086 for p in service["spec"].get("ports", [])):
        assert (service["metadata"]["namespace"],service["metadata"]["name"]) == ("superset-onprem","superset"), "NodePort 32086 is already used by another Service"
'
k create namespace superset-onprem --dry-run=client -o yaml | k apply -f -
existing="$(k -n superset-onprem get secret superset-secrets --ignore-not-found -o name)"
if [[ -z "$existing" ]]; then
  database="$(k -n superset-onprem get deployment superset-db --ignore-not-found -o name)"
  [[ -z "$database" ]] || { echo 'Database already exists but its Secret is missing. Restore the original Secret; do not generate replacement credentials.' >&2; exit 1; }
  python3 - <<'PY' | k create -f -
import json,secrets
print(json.dumps({
    'apiVersion':'v1','kind':'Secret',
    'metadata':{'name':'superset-secrets','namespace':'superset-onprem'},
    'type':'Opaque','stringData':{
        'POSTGRES_USER':'superset','POSTGRES_DB':'superset',
        'POSTGRES_PASSWORD':secrets.token_urlsafe(36),
        'SUPERSET_SECRET_KEY':secrets.token_urlsafe(64),
        'ADMIN_USERNAME':'admin','ADMIN_EMAIL':'admin@buro.local',
        'ADMIN_PASSWORD':secrets.token_urlsafe(24)}}))
PY
fi
k apply --dry-run=server -f "$SOURCE_DIR/deploy.yaml" >/dev/null
k apply -f "$SOURCE_DIR/deploy.yaml"
k -n superset-onprem rollout status deployment/superset-db --timeout=900s
k -n superset-onprem rollout status deployment/superset --timeout=1200s
k -n superset-onprem get pods -o wide
printf '\nSuperset: http://192.168.1.176:32086\nUsername: admin\n'
echo 'Retrieve your generated password locally:'
printf 'kubectl --context %q -n superset-onprem get secret superset-secrets -o jsonpath=\x27{.data.ADMIN_PASSWORD}\x27 | base64 -d; echo\n' "$KUBE_CONTEXT"
