# Auto deployment — daily release from the master

Canonical command, from the master checkout:

```bash
sh auto-deployment.sh
```

This one script pulls the release branch with `git pull --ff-only origin release/buro-ui-20261015`, builds the
tracked application code on the current live image (retaining its installed
dependencies and deployed Django settings), pushes a unique image to `192.168.1.224:80/cloud`, saves it
on the master temporarily, copies it to `dw@192.168.1.99`, verifies its checksum,
imports it into containerd's `k8s.io` namespace, and deploys `test/cloud-live-test`
container `django` on `worker-99` with `IfNotPresent`. Worker registry access is
not required. The built Docker image stays on the master and in the registry;
temporary transfer archives are removed.

SSH keys and passwordless sudo are used when available. Otherwise enter the
worker SSH/sudo password once at the hidden prompt. The supplied password is
used for SSH and sudo, kept only in a private temporary file, and deleted on exit.
It is never hardcoded in source, written to release history, or passed as a
command-line password. Do not store passwords in this repository. For an
already-secured automation environment the script also accepts a transient
`AUTO_DEPLOY_PASSWORD` environment variable; avoid typing secrets into shell
history. Install `sshpass` on the master for password-based SSH:

```bash
sudo apt-get install sshpass
```

Host keys use `accept-new`: an existing changed host key is rejected. Verify the
worker host key on first use. The worker needs SSH, sudo, containerd and
`sha256sum`. The master needs Bash, Git with repository read access, Docker with
HTTP registry trust/push access, kubectl with cluster access, Python 3, OpenSSH,
`sha256sum`, and `flock`. Source branch, registry address, workload and worker defaults are at
the top of the script. It can be invoked with either `sh` or `bash`.

Normal daily deployments deliberately retain the live `buro_first/settings.py`
and installed dependencies. For a reviewed settings change, set
`AUTO_DEPLOY_REPLACE_SETTINGS=1`. For a fresh build using the repository Dockerfile
and requirements, set `AUTO_DEPLOY_FULL_BUILD=1`; this also deploys the repository
settings. A fresh build requires independently persisted database/static/runtime
storage; it does not copy private files from a running pod into the image. If
needed, `BASE_IMAGE` selects an explicitly chosen compatible image for normal
code builds; otherwise the Deployment's current image is used.

Commit application changes before running. Untracked files are not deployed.
Use `AUTO_DEPLOY_PULL=0 sh auto-deployment.sh` to build the current committed
checkout without pulling. This script does not push Git commits, publish source
to another repository, or change ContextForge tool permissions.

The script preserves container env/settings and MCP Secret references, init
containers, runtime dependencies, and data volumes. It removes only the known
old mounted orchestrator override. Existing planner and NiFi ConfigMap fixes stay
active; changes to those specific mounted files require updating their ConfigMaps
separately. Other conflicting source mounts stop the release. The node must be
Ready/schedulable and match the remaining existing node selectors. Affinity and
taints remain subject to normal Kubernetes scheduling rules.

A JSON Patch precondition refuses concurrent pod-template changes. On a failed
rollout, it restores the previous pod template, including image, mounts and node
selection, only if nobody changed the template in the meantime. Original replica
count, service and PVC resources are not modified. A Ready rollout is deployment
verification, not a full functional test of AI tools or databases.

Release records and private rollback snapshots are in
`.manager-state/auto-<UTC timestamp>-<commit>/`; that directory must stay private.
No environment files, private keys, database files or uploaded/runtime data are
included in the generated Docker build context. Persistent database storage remains necessary to retain database changes across
pod replacements; preserving the deployed settings does not turn an ephemeral
SQLite database into persistent storage. Database migrations and ContextForge
tool registration are separate deliberate operations; this script
does not automatically alter schemas or grant newly discovered tools access.
If GitOps/Helm controls the workload, persist the recorded image in that source.

This is the workflow meant by **auto deployment**, **daily deployment**, or
**everyday deployment** in this repository. The older UI-only and agentic-only
release scripts are historical, targeted releases.

Validation without any real deployment:

```bash
python3 deploy/automation/test_auto_deployment.py
```
