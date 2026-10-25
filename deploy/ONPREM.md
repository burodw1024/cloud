# Cloud source → GitHub pull → image build → registry → worker → Kubernetes

The cloud repository contains the actual application code and deployment helpers.
The on-prem machine pulls reviewed GitHub code, builds/tests the image locally,
pushes it to the registry and copies/imports it on worker-99 before deployment.
Gitea is not contacted. Local files are not committed, and Python source is not
rewritten from a live pod.

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


## One-file reviewed deployment package

`deploy-buro-portable.sh` is generated for an exact reviewed Git tree. It creates
an isolated checkout, pulls GitHub main, locates that published tree and runs the
checked-in launcher. It refuses missing/unpublished source. Existing user
checkouts are never reset. The script itself contains no credentials. Use the
published immutable URL and externally supplied SHA-256 to verify it first.

The deployment does not contact Gitea or request Gitea credentials. Docker and SSH
use their normal credential handling; Django report-test credentials are read
privately and never written to a file. Existing production.env is mandatory.
The older Gitea mirroring helper is retained for manual use, outside deployment.

After verified 2/2 readiness and TLS checks, the launcher prompts for an existing
Django login and a report question (a branch/date example is offered). It sends
one CSRF-protected HTTPS request with a unique idempotency key, with no automatic
report retry. It requires actual loan tool execution and a structured report
response. Private response and sanitized exact results are retained in the
release snapshot directory. HTTP success without tool execution is not enough.
A legitimate empty report is explicitly recorded; it does not prove financial
correctness. Financial reconciliation and an actual browser visual check remain
separate from the HTTPS API verification.

Exit 0 means application checks and the real report request passed. Exit 2 after
`deployment_verified: true` means the application was deployed but the report
check failed; inspect the retained audit/results before retrying. A successful
application rollout releases its own completed migration lock even if report
verification fails. Failures after migration dispatch and before rollout checks
retain their lock. Existing locks are inspected and never automatically cleared.
The script cannot fix SCOTT.GET_LOAN_DUE_REPORT SQLTimeoutException/ORA-01013.

Zone ranking is packaged but stays inactive until its Oracle/NiFi endpoint and
employee scope enforcement are supplied. See ZONE_RANKING.md. The ordinary
branch loan-report presentation/table and model metadata introduction already
exist and are preserved.
