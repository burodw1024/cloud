# Cloud source → GitHub pull → image build → registry → worker → Kubernetes

The cloud repository contains the actual application code and deployment helpers.
The on-prem machine only pulls, builds and deploys that committed code. It does
not commit, push, contact Gitea, restore an embedded source archive, or rewrite
Python code from a live pod.

First use a clean GitHub checkout:

```
git clone https://github.com/burodw1024/cloud.git cloud-onprem
cd cloud-onprem
```

For each release:

```
git pull --ff-only origin main
bash deploy-agentic-updated.sh --check-source
bash deploy-agentic-updated.sh
```

Preserve older generated checkouts as-is. They may have orphan release branches
from the previous launcher and are not suitable for a fast-forward pull. The
launcher refuses dirty checkouts or an origin other than this GitHub repository;
it never resets local work. Set ENV_FILE or TLS_CERT only if the confirmed paths
differ. Existing production.env is verified but never sourced, overwritten,
printed, staged, or substituted for live Kubernetes Secret references.

The launcher exports the pulled commit into a fresh build directory and labels
the linux/amd64 app image with that source commit. It runs the app tests inside
the actual image, pushes the immutable app and pinned Nginx images to
192.168.1.224:80, saves/checksums/copies them to dw@192.168.1.99, and imports them
into containerd at /run/containerd/containerd.sock, namespace k8s.io. Interactive
worker sudo is used without passing a password as an argument. Exact imported
tags are verified before imagePullPolicy Never is used.

The existing Deployment is cloned to preserve its Secret references, TLS,
volumes and other runtime configuration. Target: context mamun, namespace rag,
Deployment buro-django, worker-99. Open WebUI is not modified. SQLite stays on
/opt/buro-ai/buro-django with one replica and Recreate. Deployment-owned pods are
identified through ReplicaSet/controller UIDs, so completed Jobs cannot affect
preflight, draining or readiness checks. Another active writer to this HostPath
causes a stop rather than an unsafe migration.

The known live Ollama hotfix is already in the cloud MCP code: think=False,
num_predict=256, keep_alive=30m. The launcher snapshots live code and checks its
normalized structure against the reviewed baseline. Unrecognized live changes
stop deployment so they can be reviewed and integrated in the cloud. No pulled
code is modified on-prem. The old gateway ConfigMap remains for recovery; its
mount is removed only in the new replacement Deployment. Running code hashes are
verified after rollout.

The project-selected model and gateway credentials are preserved. The
server-scoped MCP endpoint, 300/600-second MCP timeouts and /data/policy_rag are
verified. Gunicorn uses one gthread worker/four threads and timeout 660; Nginx
read/send timeouts are 670s. ContextForge's effective HTTPX read timeout is checked
at 300 before Django downtime; an unsupported configuration interface fails
explicitly. This does not resolve Oracle/JDBC ORA-01013 or NiFi HTTP-context errors.

MCP loan-due-report output uses the same project model to select approved report
columns from column names, never customer values. Django validates that selection
and presents exact financial values, excludes personal/contact fields, and bounds
output to 100 rows with explicit truncation. Unsupported shapes fail without
returning unfiltered data. Ranking, aggregation, arbitrary SQL, comparisons and
new member-filtering capabilities are not invented. Existing AgentTool roles and
approval flags are preserved; write-tool approval is not removed. Direct NiFi
member tools remain available when MCP mode is disabled and require their approved
member_id contract. See agentic_ai/NIFI_OPERATIONS.md.

All old app pods must terminate before the migration Job. The Job backs up
SQLite and its existing shared cache with SQLite's backup API, verifies integrity,
migrates, preserves root_123, and probes MCP/model selection and synthetic report
presentation without executing a real report. Backups are at
/opt/buro-ai/buro-django/deployment-backups/<release>/. A release-specific cache
prefix prevents replaying old formatted responses. Other data is not deleted.

Existing locks are inspected with their matching Job/pods and never blindly
removed. Before migration dispatch, a failed release can resume the old app.
After dispatch, failure retains the lock; inspect Job/schema state before retrying
or restoring. There is no automatic database rollback. Private runtime snapshots
remain in the printed release directory outside the source checkout.

Validate the actual backend response and account authorization on staging before
production. Unit/synthetic tests do not establish real-model accuracy or Oracle,
JDBC and NiFi health. The cloud cannot reach this private cluster; a published
source commit is not a completed on-prem deployment.
