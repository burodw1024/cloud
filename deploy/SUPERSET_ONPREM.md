# Fresh Superset on the existing Kubernetes cluster

This package installs Superset web and an initialization Job on
`etlreg-virtual-machine` (10.50.1.29), in namespace `superset`.
It reuses the existing HA PostgreSQL primary service in `postgres-ha-test`.
It creates no PostgreSQL pods or volumes and leaves the old standalone
`superset-postgres` instance untouched.

The cloud workspace cannot access the private cluster. Run the commands below
on your on-premises administration/build machine with its authorized kubectl
context. The source is a reviewed deployment package, not evidence of a live
rollout. The image build and real database/network checks happen on premises.

## 1. Build and publish the Superset image

Superset's lean image needs its PostgreSQL driver added. The Dockerfile pins
upstream Superset 6.1.0 by digest and psycopg2-binary 2.9.11. Run on a machine with
Docker build access and access to your registry:

```bash
docker build --platform linux/amd64 \
  -t 192.168.1.224:80/agentic-superset:6.1.0 - < deploy/superset.Dockerfile
docker push 192.168.1.224:80/agentic-superset:6.1.0
SUPERSET_IMAGE=$(docker image inspect \
  --format '{{index .RepoDigests 0}}' \
  192.168.1.224:80/agentic-superset:6.1.0)
```

Use your actual reachable registry if it differs. Containerd on the selected
node must already trust and be able to pull from that registry. If registry
authentication is needed, create its pull Secret in namespace `superset` using
your established credential process and pass `--pull-secret NAME` below. The
installer does not modify registry trust or node containerd configuration.

The resulting `SUPERSET_IMAGE` must contain an `@sha256:` digest. If building on
a separate machine, transfer that non-secret image reference to the kubectl
administration machine; the installer does not require Docker there.

## 2. Inspect and install

The `superset` namespace is already present in the supplied inventory. Select
the correct kubectl context locally. Add `--context NAME` if needed.

```bash
python3 deploy/install_superset.py --image "$SUPERSET_IMAGE"
python3 deploy/install_superset.py --image "$SUPERSET_IMAGE" --apply
```

The first command is read-only preflight. It checks node readiness, taints,
architecture, current resource reservations, existing Superset objects, a ready
PostgreSQL service endpoint and a single primary pod. It runs read-only SQL
using `psql -U postgres` through `kubectl exec` in the existing Spilo primary.
Your administration account must be allowed to exec that pod, and local
PostgreSQL administrator access must work there. This is for provisioning only;
the deployed application does not receive PostgreSQL superuser credentials.

### Use ContextForge's connection reference (recommended after an exec timeout)

The confirmed `buro-agentic/contextforge` Deployment loads `contextforge-config`
and `contextforge-secrets`. Use the existing image and this mode:

```bash
python3 deploy/install_superset.py --image "$SUPERSET_IMAGE" \
  --allow-existing-superset --use-contextforge
python3 deploy/install_superset.py --image "$SUPERSET_IMAGE" \
  --allow-existing-superset --use-contextforge --apply
```

Reference discovery reads only the Deployment. It does not retrieve Secret or
ConfigMap data. Optional references for standard database URI keys are resolved
by Kubernetes inside a short-lived Job in namespace `buro-agentic`. Later Secret
sources take precedence over earlier ConfigMap sources, matching the gateway's
environment. Explicit URI references override envFrom sources. Other ContextForge
credentials (Redis, signing keys, API tokens) are not injected into the Job.

In this mode, default dry run checks references and scheduling only. On `--apply`,
a small Job first validates PostgreSQL connectivity, the primary role, sufficient
provisioning privileges and unused destination names. This happens before the
administrator password prompt or permanent Superset Secrets are created. It
needs a PostgreSQL administrator credential; ordinary ContextForge application
credentials are not silently promoted or granted more privileges.

The Job uses the existing URI's account, password, database and TLS settings,
with its host redirected to the explicitly selected HA primary service. It
does not write ContextForge's application tables. Superset receives fresh,
separate metadata and analytics accounts. No PostgreSQL pods are changed.

The installer reads Job status and sanitized pod termination messages through
the Kubernetes API. It does not use `kubectl exec` or `kubectl logs` for database
provisioning. Successful temporary Jobs, scripts and generated-password payloads
are deleted; failed Jobs are retained for diagnosis. Initial image pull may take
several minutes. If `--pull-secret` is used, the referenced Secret must exist in
both namespaces; otherwise the Job uses ContextForge's existing pull references.

The separate Superset initialization Job still runs normally afterward. No image
rebuild is required for this mode because the database helper script is supplied
through a ConfigMap and the already built image includes psycopg2.

### Image pull and Job deadline troubleshooting

An observed check Job was scheduled on `etlreg-virtual-machine` but could not
pull `192.168.1.224:80/agentic-superset`: containerd's request to
`https://192.168.1.224:80/v2/` timed out. The 600-second Job deadline then deleted
the pending pod. This was an image distribution failure, before database code
ran. Increasing the database timeout does not address it.

On the administration machine, inspect registry Services:

```bash
kubectl -n registry get svc -o wide
```

On the selected node, inspect HTTP reachability and routing:

```bash
curl -I --connect-timeout 5 --max-time 10 http://192.168.1.224:80/v2/
ip route get 192.168.1.224
```

HTTP 200 or an authentication response establishes reachability; a TCP timeout
requires a route/firewall or reachable-endpoint fix. If the registry serves plain
HTTP, containerd must also be explicitly configured for that endpoint. Do not
assume disabling certificate verification will fix either a TCP timeout or an
HTTP/HTTPS mismatch. Inspect existing containerd configuration before changes;
the installer does not overwrite it or restart containerd.

The installer now observes pod startup states while waiting and retains the
last controlled diagnostic if the Job controller deletes the pod at its deadline.
Pod events may also remain available after deletion; retrieve them using
`kubectl get events --field-selector involvedObject.name=POD_NAME -n NAMESPACE`.

The read-only SQL check uses `psql --command` without streaming stdin. All psql
calls use `-w` (never prompt for a database password), a five-second connection
timeout and server statement/lock timeouts. Provisioning sends its credential
SQL only on stdin and explicitly exits with `\quit`, avoiding reliance on exec
stdin EOF. A preflight timeout occurs before credentials or databases are created.

If preflight still times out, run these read-only probes from the same machine:

```bash
timeout 15s kubectl -n postgres-ha-test exec acid-postgres-test-0 -c postgres -- /bin/echo exec-ok
timeout 20s kubectl -n postgres-ha-test exec acid-postgres-test-0 -c postgres -- \
  env PGCONNECT_TIMEOUT=5 psql -X -w -U postgres -d postgres -Atc 'SELECT pg_is_in_recovery()'
```

The first isolates Kubernetes exec transport; the second adds PostgreSQL access.
If failover has changed the primary, substitute the current pod selected by
`cluster-name=acid-postgres-test,spilo-role=master`. Do not treat a transport
timeout as a reason to recreate or restart the PostgreSQL cluster.

The apply command asks for a new Superset administrator password locally,
without echoing it. Passwords are generated or entered on premises, retained
in Kubernetes Secrets, and never printed or passed as CLI arguments. Cluster
Secret encryption/RBAC remains the administrator's responsibility.

Only fresh database/account names are accepted:

| Database | Account | Purpose |
|---|---|---|
| `agentic_superset` | `agentic_superset` | Superset metadata owner |
| `agentic_analytics` | `agentic_snapshot_writer` | Django snapshot staging |
| `agentic_analytics` | `agentic_snapshot_reader` | Superset read-only chart queries |

The analytics schema is `agentic`. The writer's future tables automatically
grant SELECT to the reader. Public database access and public schema creation
are revoked for these newly created databases. Existing database permissions
are unchanged. PostgreSQL TLS uses `sslmode=require`: transport is encrypted,
but certificate identity is not validated by that mode. Install the CA and use
`verify-full` when configuring your production trust policy.

The installer runs database migrations, initializes Superset roles, creates an
`admin` account, registers **Agentic snapshots** as a read-only analytics
connection, and checks that the service connects to a primary. Only then does
it start the web pod. Both the Job and web pod request 1 CPU/2 GiB and are
limited to 2 CPU/4 GiB. They start sequentially. No Redis/Celery is included;
this initial installation supports synchronous dashboard queries.

Existing installations, credential records, database/account name collisions,
insufficient resources, unavailable endpoints or failed initialization stop
the installer. A partial installation retains objects and Secrets for review.
It never automatically rotates credentials, drops databases or retries an
uncertain migration. Do not rerun after a partial failure without inspecting it.

If preflight reports a differently named, unused Superset deployment, the new
instance can be installed alongside it using `--allow-existing-superset` on both
preflight and apply commands. This option does not modify the older deployment,
reuse its metadata, delete workloads or bypass collisions with `agentic-superset`
resources, Secrets or database names. Existing services/controllers that would
select the new pod also cause a stop, to avoid unintended adoption or exposure.
The error identifies the resource name that blocked the installation.

## 3. Verify and open

```bash
kubectl -n superset get pods -l app.kubernetes.io/name=agentic-superset -o wide
kubectl -n superset logs job/agentic-superset-init
kubectl -n superset port-forward service/agentic-superset 8088:8088
```

Open `http://127.0.0.1:8088` on the machine running port-forward. Sign in as
`admin` with the password entered during installation. If that machine is
remote, use an SSH tunnel to its loopback port. The Service is ClusterIP;
no public ingress or NodePort is created. Configure your normal HTTPS ingress
before exposing it to other users.

The initialization log reports the analytics database ID and administrator user
ID, not passwords. Ready web probes and successful init establish startup and
database connectivity, not user authorization or chart compatibility. Verify
an actual chart and a dashboard, then test an unrelated user's access before
enabling the agent's publisher.

## 4. Connect the agent application

See `agentic_ai/SUPERSET_SETUP.md` in the agent feature source for its Django
settings. That feature is a separate application change and must also be
deployed; installing Superset alone does not update the running Django image.

- Internal URL: `http://agentic-superset.superset.svc.cluster.local:8088`
- Analytics database ID: printed by the initialization Job.
- Schema: `agentic`.
- Writer credentials: Secret `agentic-superset-analytics-writer` in namespace
  `superset`. Bind them securely to the application namespace; Kubernetes
  Secret references cannot cross namespaces.
- Configure a publisher service account and a dedicated Superset account for
  each Django user, then map their IDs. Keep the initial admin account for
  administration.
- Internal HTTP requires the agent's explicit `ALLOW_HTTP=True`; use HTTPS
  instead when your internal ingress is ready.
- Set `PRIVATE_ACCESS_VERIFIED=True` only after the access tests in the setup
  guide pass. Superset dashboard RBAC is enabled, but that alone does not prove
  the complete dataset/chart permission configuration.

The cluster domain defaults to `cluster.local`; pass `--cluster-domain DOMAIN`
if your cluster uses another domain. No service-account token is mounted in the
web pod or initialization Job. Test database backup/restore and schedule export
retention cleanup before enabling production reports.
