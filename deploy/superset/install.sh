#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077
export KUBE_CONTEXT="${KUBE_CONTEXT:-mamun}"
SWITCH_EMPTY_LOCAL=0
if [[ "${1:-}" == --switch-empty-local ]]; then SWITCH_EMPTY_LOCAL=1; shift; fi
[[ "$#" == 0 ]] || { echo "Usage: bash install.sh [--switch-empty-local]" >&2; exit 1; }
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
# Build the existing Superset version with its driver before deploying.
[[ -n "${EXISTING_SUPERSET_IMAGE:-}" ]] || { echo 'Set EXISTING_SUPERSET_IMAGE to the prepared Superset 4.1.2 image with psycopg2 included. See deploy/superset/README.md for build/push commands.' >&2; exit 1; }
export EXISTING_SUPERSET_IMAGE
python3 -c '
import os,re
image=os.environ["EXISTING_SUPERSET_IMAGE"]
assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]+",image), "Invalid image reference"
assert "@sha256:" in image or (":" in image.rsplit("/",1)[-1] and not image.endswith(":latest")), "Use the exact existing Superset version or digest, not latest"
'
k create namespace superset-onprem --dry-run=client -o yaml | k apply -f -
existing="$(k -n superset-onprem get secret superset-external-db-secrets --ignore-not-found -o name)"
if [[ -z "$existing" ]]; then
  read -r -s -p 'Existing PostgreSQL password: ' POSTGRES_PASSWORD
  printf '\n'
  read -r -s -p 'Original existing Superset SUPERSET_SECRET_KEY (not the database password): ' SUPERSET_SECRET_KEY
  printf '\n'
  [[ -n "$POSTGRES_PASSWORD" && -n "$SUPERSET_SECRET_KEY" ]] || { echo 'Both existing secrets are required. No replacement key is generated.' >&2; exit 1; }
  export POSTGRES_PASSWORD SUPERSET_SECRET_KEY
  python3 - <<'PY' | k create -f -
import json,os
print(json.dumps({
    'apiVersion':'v1','kind':'Secret',
    'metadata':{'name':'superset-external-db-secrets','namespace':'superset-onprem'},
    'type':'Opaque','stringData':{
        'POSTGRES_HOST':'192.168.1.223','POSTGRES_PORT':'5432',
        'POSTGRES_USER':'supersetadmin','POSTGRES_DB':'superset',
        'POSTGRES_SSLMODE':os.environ.get('POSTGRES_SSLMODE','require'),
        'POSTGRES_PASSWORD':os.environ['POSTGRES_PASSWORD'],
        'SUPERSET_SECRET_KEY':os.environ['SUPERSET_SECRET_KEY']}}))
PY
  unset POSTGRES_PASSWORD SUPERSET_SECRET_KEY
fi
# Check destination metadata without printing any credential.
k -n superset-onprem get secret superset-external-db-secrets -o json | python3 -c '
import base64,json,sys
values=json.load(sys.stdin)["data"]
for key,expected in {"POSTGRES_HOST":"192.168.1.223","POSTGRES_PORT":"5432","POSTGRES_USER":"supersetadmin","POSTGRES_DB":"superset"}.items():
    assert base64.b64decode(values.get(key,"")).decode()==expected, "Existing Secret targets a different database; it was not changed"
assert all(values.get(key) for key in ("POSTGRES_PASSWORD","SUPERSET_SECRET_KEY","POSTGRES_SSLMODE")), "Existing database credentials, Superset key and TLS mode are required"
'
switch_args=()
if [[ "$SWITCH_EMPTY_LOCAL" == 1 ]]; then switch_args+=(--switch-empty-local); fi
python3 "$SOURCE_DIR/switch_empty_local.py" --context "$KUBE_CONTEXT" "${switch_args[@]}"
manifest="$(mktemp)"
trap 'rm -f "$manifest"' EXIT
python3 - "$SOURCE_DIR/deploy.yaml" > "$manifest" <<'PY'
import os,sys
from pathlib import Path
text=Path(sys.argv[1]).read_text()
assert text.count('REPLACE_WITH_EXISTING_SUPERSET_IMAGE')==1
print(text.replace('REPLACE_WITH_EXISTING_SUPERSET_IMAGE',os.environ['EXISTING_SUPERSET_IMAGE']))
PY
k apply --dry-run=server -f "$manifest" >/dev/null
k apply -f "$manifest"
k -n superset-onprem rollout status deployment/superset --timeout=1200s
k -n superset-onprem get pods -o wide
printf '\nSuperset: http://192.168.1.176:32086\nUse your existing Superset web login. Database credentials are not web credentials.\n'
