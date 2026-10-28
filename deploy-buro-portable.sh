#!/usr/bin/env bash
# BURO Insight AI: reviewed GitHub -> image -> registry -> worker -> Kubernetes.
set -Eeuo pipefail
set +x
umask 077
REVIEWED_TREE='fc05ffc0b34109418d6e543c1a9af1877d9925df'
SOURCE_URL=https://github.com/burodw1024/cloud.git
if [[ "${1:-}" == --describe ]]; then
  printf 'BURO Insight AI reviewed source tree: %s\n' "$REVIEWED_TREE"
  exit 0
fi
[[ "$#" == 0 || ( "$#" == 1 && "$1" == --check-source ) ]] || {
  echo 'Usage: bash deploy-buro-portable.sh [--check-source|--describe]' >&2; exit 1;
}
for tool in git python3 mktemp; do
  command -v "$tool" >/dev/null || { echo "Missing required admin tool: $tool" >&2; exit 1; }
done
if [[ "${1:-}" != --check-source ]]; then
  [[ -t 0 && -t 1 ]] || { echo 'Use an interactive terminal for private credential prompts.' >&2; exit 1; }
  ENV_FILE="${ENV_FILE:-/actual/full/path/production.env}"
  PUBLIC_CA_FILE="${PUBLIC_CA_FILE:-$HOME/.config/agentic/tls/tls.crt}"
  [[ -r "$ENV_FILE" ]] || { echo 'Existing production.env is missing; no replacement is generated.' >&2; exit 1; }
  [[ -r "$PUBLIC_CA_FILE" ]] || { echo 'Existing public TLS certificate is missing.' >&2; exit 1; }
  export ENV_FILE PUBLIC_CA_FILE
fi
RUN_ROOT="$(mktemp -d "$PWD/buro-deploy.XXXXXXXX")"
echo "Private deployment checkout and records: $RUN_ROOT"
git clone --single-branch --branch main "$SOURCE_URL" "$RUN_ROOT/cloud"
git -C "$RUN_ROOT/cloud" pull --ff-only origin main
SOURCE_COMMIT="$(python3 - "$RUN_ROOT/cloud" "$REVIEWED_TREE" <<'RESOLVE'
import subprocess,sys
root,expected=sys.argv[1:]
def git(*args):
    return subprocess.check_output(['git','-C',root,*args],text=True).strip()
# The source tree is immutable even though GitHub's commit metadata differs from
# the local reviewed commit. Packaging adds only this entrypoint in a later commit.
for commit in git('rev-list','--max-count=100','origin/main').splitlines():
    if git('rev-parse',commit+'^{tree}')==expected:
        print(commit)
        break
else:
    raise SystemExit('The exact reviewed source tree is not published in GitHub main. Nothing was deployed.')
RESOLVE
)"
git -C "$RUN_ROOT/cloud" switch --detach "$SOURCE_COMMIT"
[[ "$(git -C "$RUN_ROOT/cloud" rev-parse 'HEAD^{tree}')" == "$REVIEWED_TREE" ]] || exit 1
printf 'Verified published source commit: %s\n' "$SOURCE_COMMIT"
cd "$RUN_ROOT"
exec bash "$RUN_ROOT/cloud/deploy-agentic-updated.sh" "$@"
