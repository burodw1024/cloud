#!/bin/sh
# POSIX entry point: `sh auto-deployment.sh` also works.
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set +x
set -Eeuo pipefail
umask 077

# Daily release: master builds/pushes, copies to worker, imports and rolls out.
NAMESPACE=test
DEPLOYMENT=cloud-live-test
CONTAINER=django
WORKER_NODE=worker-99
WORKER=dw@192.168.1.99
REGISTRY=192.168.1.224:80
REPOSITORY=cloud
WSGI_MODULE=buro_first.wsgi
SOURCE_REMOTE=origin
SOURCE_BRANCH=release/buro-ui-20261015
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$ROOT"

for tool in bash git docker kubectl python3 ssh scp sha256sum flock; do
  command -v "$tool" >/dev/null || { echo "Missing command: $tool" >&2; exit 1; }
done
# A daily deployment builds committed source. Pull and then re-execute the updated
# script so the shell never continues reading a file overwritten by git pull.
if [[ "${AUTO_DEPLOY_SOURCE_READY:-0}" != 1 ]]; then
  [[ -z "$(git status --porcelain --untracked-files=no)" ]] || {
    echo 'Commit or stash tracked changes before auto deployment.' >&2; exit 1;
  }
  if [[ "${AUTO_DEPLOY_PULL:-1}" == 1 ]]; then
    git pull --ff-only "$SOURCE_REMOTE" "$SOURCE_BRANCH"
  fi
  export AUTO_DEPLOY_SOURCE_READY=1
  exec bash "$ROOT/auto-deployment.sh" "$@"
fi
mkdir -p "$ROOT/.manager-state"
chmod 700 "$ROOT/.manager-state"
exec 9>"$ROOT/.manager-state/auto-deployment.lock"
flock -n 9 || { echo 'Another auto deployment is already running.' >&2; exit 1; }
WORK="$(mktemp -d)"
REMOTE_DIR=""
SSH_PREFIX=()
SSH_OPTIONS=(-o NumberOfPasswordPrompts=1 -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
SUDO_PASSWORD=0
ssh_run() { "${SSH_PREFIX[@]}" ssh "${SSH_OPTIONS[@]}" "$WORKER" "$@"; }
remote_sudo() {
  if [[ "$SUDO_PASSWORD" == 1 ]]; then
    ssh_run "sudo -S -p '' $*" < "$WORK/password"
  else
    ssh_run "sudo -n $*"
  fi
}
cleanup() {
  rc=$?
  trap - EXIT
  if [[ -n "$REMOTE_DIR" ]]; then
    ssh_run "rm -f '$REMOTE_DIR/image.tar'; rmdir '$REMOTE_DIR'" </dev/null >/dev/null 2>&1 || true
  fi
  rm -rf -- "$WORK"
  if [[ "$rc" != 0 ]]; then
    echo 'Auto deployment failed. Read the error above; no success is assumed.' >&2
  fi
  exit "$rc"
}
trap cleanup EXIT

COMMIT="$(git rev-parse HEAD)"
TAG="auto-$(date -u +%Y%m%dT%H%M%SZ)-${COMMIT:0:12}"
IMAGE="$REGISTRY/$REPOSITORY:$TAG"
STATE="$ROOT/.manager-state/$TAG"
mkdir -p "$STATE" "$WORK/build"
# Deployment snapshots can contain private configuration. Keep them outside the
# Docker build context and in this private release directory.
kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o json > "$STATE/before.json"
kubectl get node "$WORKER_NODE" -o json > "$WORK/node.json"
python3 - "$ROOT" "$WORK/build" "$STATE" "$WORK/node.json" "$IMAGE" "${AUTO_DEPLOY_FULL_BUILD:-0}" "${AUTO_DEPLOY_REPLACE_SETTINGS:-0}" <<'PY'
import copy, json, shutil, subprocess, sys
from pathlib import Path, PurePosixPath
root, context, state, node_file = map(Path, sys.argv[1:5]); image = sys.argv[5]
full_build = sys.argv[6] == '1'; replace_settings = sys.argv[7] == '1'
before = json.loads((state/'before.json').read_text())
node = json.loads(node_file.read_text())
if node.get('spec', {}).get('unschedulable') or not any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in node.get('status', {}).get('conditions', [])):
    raise SystemExit('worker-99 is not Ready/schedulable. Stop before building.')
if node.get('status', {}).get('nodeInfo', {}).get('architecture') != 'amd64':
    raise SystemExit('This deployment expects an amd64 worker.')
original = before['spec']['template']; updated = copy.deepcopy(original)
spec = updated['spec']; container = next(c for c in spec['containers'] if c['name'] == 'django')
(state/'base-image.txt').write_text(container['image'])
volumes = {v['name']: v for v in spec.get('volumes', [])}
# Preserve the known live fixes. Only the obsolete orchestrator override is removed.
legacy = {
    '/app/agentic_ai/planner.py': ('agentic-live-code', 'planner.py', 'cloud-live-test-agentic-'),
    '/app/agentic_ai/tools/nifi_tool.py': ('dashboard-nifi-fix', 'nifi_tool.py', 'dashboard-nifi-url-'),
}
kept = []
tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
app_dirs = {'agentic_ai','agentic_rag_api','buro_first','chat','config','customer_support','email_process_data','hr_notice_rag','openai_policy','static','workflows','templates','locale','scripts'}
# New application packages are included without maintaining a release file list.
app_dirs.update(PurePosixPath(n).parts[0] for n in tracked if n.endswith('.py') and len(PurePosixPath(n).parts)>1 and PurePosixPath(n).parts[0] not in {'deploy','tests','work','scratch'})
root_files = {'Dockerfile','.dockerignore','manage.py','requirements.txt','requirements.docker.txt','website_rag_tool.py'}
excluded_dirs = {'__pycache__','tests','media','uploads','logs','staticfiles','node_modules','.git','.aws','.codex','.agents','work'}
extensions = {'.py','.html','.css','.js','.json','.txt','.csv','.md','.svg','.png','.jpg','.jpeg','.gif','.ico','.woff','.woff2','.ttf','.eot','.otf','.webp','.avif','.mjs','.cjs','.po','.mo','.map','.yaml','.yml'}
files = []
for name in tracked:
    if not name: continue
    if name == 'buro_first/settings.py' and not (full_build or replace_settings): continue
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or any(p in excluded_dirs for p in path.parts): continue
    if any('.env' in p.lower() for p in path.parts) or path.suffix.lower() in {'.pem','.key','.crt','.sqlite3'}: continue
    if name not in root_files and (path.parts[0] not in app_dirs or path.suffix.lower() not in extensions): continue
    source = root/name
    if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()): raise SystemExit('Review source symlink before deploying: '+name)
    if not source.is_file(): raise SystemExit('Tracked source file is missing: '+name)
    files.append(name)
for mount in container.get('volumeMounts', []):
    path = mount['mountPath']; volume = volumes.get(mount['name'], {})
    cm = volume.get('configMap', {}).get('name', '')
    if (path == '/app/agentic_ai/orchestrator.py' and mount['name'] == 'dashboard-mcp-code'
            and mount.get('subPath') == 'orchestrator.py' and cm.startswith('dashboard-mcp-display-')):
        print('Replacing the old orchestrator.py ConfigMap override with image code.')
        continue
    p = PurePosixPath(path)
    if any(p == PurePosixPath('/app')/f or p in (PurePosixPath('/app')/f).parents for f in files):
        expected = legacy.get(path)
        if not expected or (mount['name'], mount.get('subPath')) != expected[:2] or not cm.startswith(expected[2]):
            raise SystemExit('Source mount would hide this release; review it first: '+path)
        print('Keeping existing live ConfigMap override: '+path)
    kept.append(mount)
container['volumeMounts'] = kept
container['image'] = image; container['imagePullPolicy'] = 'IfNotPresent'
spec.setdefault('nodeSelector', {})['kubernetes.io/hostname'] = 'worker-99'
labels = node.get('metadata', {}).get('labels', {})
if any(labels.get(k) != v for k,v in spec['nodeSelector'].items()):
    raise SystemExit('Existing node selectors do not match worker-99.')
# Atomic precondition: refuse a concurrent deployment/configuration change.
(state/'deploy.json').write_text(json.dumps([
    {'op':'test','path':'/spec/template','value':original},
    {'op':'replace','path':'/spec/template','value':updated},
]))
(state/'rollback.json').write_text(json.dumps([
    {'op':'test','path':'/spec/template','value':updated},
    {'op':'replace','path':'/spec/template','value':original},
]))
for name in files:
    target=context/name; target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(root/name,target); target.chmod(0o644)
for directory in context.rglob('*'):
    if directory.is_dir(): directory.chmod(0o755)
for required in ['Dockerfile','requirements.docker.txt','manage.py']:
    if not (context/required).is_file(): raise SystemExit('Missing build file: '+required)
print('Prepared tracked application source; environment files, private keys and runtime data excluded.')
if not (full_build or replace_settings): print('Keeping the deployed Django settings.py from the existing image.')
PY

echo '[1/7] Checking Docker and worker SSH/sudo access...'
docker info >/dev/null
KEY_AUTH=0
if ssh -o BatchMode=yes "${SSH_OPTIONS[@]}" "$WORKER" true </dev/null >/dev/null 2>&1; then KEY_AUTH=1; fi
NEED_PASSWORD=0
if [[ "$KEY_AUTH" == 0 ]]; then NEED_PASSWORD=1
elif ! ssh_run 'sudo -n true' </dev/null >/dev/null 2>&1; then NEED_PASSWORD=1; fi
if [[ "$NEED_PASSWORD" == 1 ]]; then
  PASSWORD="${AUTO_DEPLOY_PASSWORD:-}"
  unset AUTO_DEPLOY_PASSWORD
  if [[ -z "$PASSWORD" ]]; then
    if ! read -r -s -p "SSH/sudo password for $WORKER: " PASSWORD </dev/tty; then
      echo 'A terminal is required for the password prompt. Use SSH keys or secured AUTO_DEPLOY_PASSWORD automation.' >&2
      exit 1
    fi
    echo
  fi
  [[ -n "$PASSWORD" ]] || { echo 'An SSH/sudo password is required.' >&2; exit 1; }
  printf '%s\n' "$PASSWORD" > "$WORK/password"
  unset PASSWORD
  if [[ "$KEY_AUTH" == 0 ]]; then
    command -v sshpass >/dev/null || { echo 'Install sshpass on the master: sudo apt-get install sshpass' >&2; exit 1; }
    SSH_PREFIX=(sshpass -f "$WORK/password")
  fi
  SUDO_PASSWORD=1
  remote_sudo '-v'
fi
REMOTE_DIR="$(ssh_run 'mktemp -d /tmp/buro-auto.XXXXXXXX' </dev/null)"
[[ "$REMOTE_DIR" =~ ^/tmp/buro-auto\.[A-Za-z0-9_-]+$ ]] || { echo 'Unexpected worker temporary directory.' >&2; exit 1; }

echo '[2/7] Building the application image...'
BUILD_ARGS=(--build-arg "WSGI_MODULE=$WSGI_MODULE")
if [[ "${AUTO_DEPLOY_FULL_BUILD:-0}" != 1 ]]; then
  BASE="${BASE_IMAGE:-$(cat "$STATE/base-image.txt")}"
  docker image inspect "$BASE" >/dev/null 2>&1 || docker pull "$BASE"
  OWNER="$(docker image inspect --format '{{.Config.User}}' "$BASE")"
  OWNER="${OWNER:-0}"
  [[ "$OWNER" =~ ^[A-Za-z0-9_.:-]+$ ]] || { echo 'Unexpected image user; review the base image.' >&2; exit 1; }
  # Use a private generated context; keep deployed dependencies and settings.
  printf 'ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nCOPY --chown=%s . /app/\n' "$OWNER" > "$WORK/build/Dockerfile"
  BUILD_ARGS+=(--build-arg "BASE_IMAGE=$BASE")
fi
docker build --platform linux/amd64 "${BUILD_ARGS[@]}" \
  --label "org.opencontainers.image.revision=$COMMIT" -t "$IMAGE" "$WORK/build"

echo '[3/7] Pushing to the private registry...'
docker push "$IMAGE"

echo '[4/7] Saving on the master and copying to worker-99...'
docker save -o "$WORK/image.tar" "$IMAGE"
ARCHIVE_HASH="$(sha256sum "$WORK/image.tar")"; ARCHIVE_HASH="${ARCHIVE_HASH%% *}"
"${SSH_PREFIX[@]}" scp "${SSH_OPTIONS[@]}" "$WORK/image.tar" "$WORKER:$REMOTE_DIR/image.tar"
REMOTE_HASH="$(ssh_run "sha256sum '$REMOTE_DIR/image.tar'" </dev/null)"; REMOTE_HASH="${REMOTE_HASH%% *}"
[[ "$ARCHIVE_HASH" == "$REMOTE_HASH" ]] || { echo 'Transferred image checksum mismatch.' >&2; exit 1; }

echo '[5/7] Importing into the worker Kubernetes image store...'
remote_sudo "ctr -n k8s.io images import '$REMOTE_DIR/image.tar'"

echo '[6/7] Applying the image and scheduling on worker-99...'
kubectl -n "$NAMESPACE" patch deployment "$DEPLOYMENT" --type=json --patch-file "$STATE/deploy.json"
printf 'Image: %s\nCommit: %s\nNamespace: %s\nDeployment: %s\n' "$IMAGE" "$COMMIT" "$NAMESPACE" "$DEPLOYMENT" > "$STATE/release.txt"

echo '[7/7] Waiting for the rollout...'
if ! kubectl -n "$NAMESPACE" rollout status "deployment/$DEPLOYMENT" --timeout=300s; then
  echo 'Rollout failed. Restoring the previous pod template (image, mounts and scheduling).' >&2
  kubectl -n "$NAMESPACE" patch deployment "$DEPLOYMENT" --type=json --patch-file "$STATE/rollback.json" || {
    echo "Rollback refused or failed. Another change may have occurred. Review $STATE manually." >&2; exit 1;
  }
  kubectl -n "$NAMESPACE" rollout status "deployment/$DEPLOYMENT" --timeout=300s
  exit 1
fi
ACTUAL_IMAGE="$(kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[?(@.name=="django")].image}')"
[[ "$ACTUAL_IMAGE" == "$IMAGE" ]] || { echo 'The deployment image changed during rollout; verify its owner/controller before retrying.' >&2; exit 1; }
printf 'DEPLOYED: %s\nRelease record: %s\n' "$IMAGE" "$STATE/release.txt"
echo 'Django settings, MCP token references, init containers, dependency/data mounts are preserved.'
echo 'Refresh your browser. Persist the image in GitOps/Helm if it manages this workload.'
