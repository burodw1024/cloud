# Small Superset frontend with the existing PostgreSQL database

This manifest runs only Superset, pinned to node `superset` (192.168.1.176).
It connects to the existing metadata database:

- Host: 192.168.1.223
- Port: 5432
- Database: superset
- Database user: supersetadmin

Passwords and the original SUPERSET_SECRET_KEY are not stored in Git. On first
installation `install.sh` prompts for both, without echo, and creates a Kubernetes
Secret. It preserves that Secret on subsequent runs. The Superset key must be
from the existing installation; generating a replacement would make encrypted
saved connection credentials unreadable. The database password is not this key
and database credentials are not the Superset web login.

## Deploy

The observed existing version is Apache Superset 4.1.2. Build that same version
with its PostgreSQL driver on the build host, then push it to your registry.
This removes all pod-startup package downloads. From your on-prem release
directory after pulling main:

```bash
export EXISTING_SUPERSET_IMAGE="192.168.1.224:80/superset:4.1.2-pg-$(date -u +%Y%m%d%H%M%S)"
docker build --network=host --platform=linux/amd64 \
  --tag "$EXISTING_SUPERSET_IMAGE" source/deploy/superset
docker run --rm --network=none --entrypoint python \
  "$EXISTING_SUPERSET_IMAGE" -c 'import psycopg2; from importlib.metadata import version; assert version("apache-superset") == "4.1.2"'
docker push "$EXISTING_SUPERSET_IMAGE"
bash source/deploy/superset/install.sh
```

Context defaults to mamun. EXISTING_SUPERSET_IMAGE must identify your prepared
4.1.2 image; the installer rejects a missing image, latest or an untagged image.
It renders the image placeholder before server validation/application. Do not
apply the template directly or choose a new Superset version as part of this
database correction. The Dockerfile verifies the app version and driver import.

The installer checks node identity/Ready status and NodePort availability.
By default it refuses to switch if the earlier superset-onprem/superset-db
Deployment is present. For the reported Pending frontend with an empty local
database, run:

```bash
bash source/deploy/superset/install.sh --switch-empty-local
```

This creates a separate superset-external-db-secrets Secret, leaving the original
superset-secrets untouched. It checks all non-system schemas for user tables,
stops/drains the local frontend, checks emptiness again, copies local credentials
to superset-local-credentials-retained, then stops/drains the local database.
Its Deployment, Service and hostPath data are retained. A ConfigMap records the
verified database UID so subsequent installs can recognize this completed switch.
A nonempty database blocks switching; export/migrate its metadata separately.
No existing database, local data directory or Secret is deleted. Partial failures
retain state; inspect the error rather than deleting the guard or force-switching.

Startup verifies PostgreSQL connectivity and an existing Alembic metadata
version, then starts Gunicorn. It does not run migrations, initialize the
metadata database or create/reset administrator accounts. Use the existing
Superset web login. Plan version upgrades and metadata backups separately.
The original deployment/Service in namespace superset is left as-is; this
frontend runs in superset-onprem on port 32086 instead of existing port 32088.

The PostgreSQL connection defaults to sslmode=require, suitable for encrypted
connections when no private CA bundle was supplied. Set POSTGRES_SSLMODE before
first installation only when matching your known database TLS configuration.
Passwords are assembled with SQLAlchemy URL.create, so special characters are
encoded correctly. Credentials do not appear in shell arguments or manifests
saved on disk. Protect/back up the Kubernetes Secret along with the external
metadata database; do not regenerate its key during reinstalls.

## Confirmed empty database: initialize once

If a read-only check confirms zero tables in every user schema and startup
reports a missing alembic_version table, the database was provisioned but has
not been initialized for Superset. Use the already imported image:

```bash
python3 source/deploy/superset/initialize_empty_db.py --context mamun
```

Choose a new Superset web-admin username/password at the prompts. They are
different from the PostgreSQL role/password. The helper creates an initialization
lock and temporary admin Secret, stops/drains the CrashLooping frontend, and
runs a one-shot Job on the fixed node with imagePullPolicy Never. The Job checks
the image version and exact database destination, takes a PostgreSQL advisory
lock, and refuses any non-system tables/views before migration. It runs Superset
4.1.2 metadata migrations, creates the initial web admin without resetting an
existing account, and initializes permissions. No package/image download or
database deletion occurs. The encryption key and DB password remain unchanged.

After Job success, the helper starts the frontend and waits for readiness. Only
then are its temporary Secret and lock removed. The completed Job is retained.
Any failure retains the lock and Job; inspect logs before further action.
An interrupted/partial initialization is not automatically retried or reset.
Do not run this helper on a populated database or a healthy frontend.

### Recover the monitoring-view guard failure

The reported cluster precreates eight monitoring views, so a physically empty
schema is not required. The initializer exempts only these verified tuples:
metric_helpers.index_bloat, nearly_exhausted_sequences, pg_stat_statements and
table_bloat (ordinary views); public.pg_stat_kcache and pg_stat_kcache_detail
(owned by pg_stat_kcache); public.pg_stat_statements and pg_stat_statements_info
(owned by pg_stat_statements). Any other object, different type or unexpected
extension ownership remains blocking. No monitoring view is dropped or changed.

For the original Job that failed at the old emptiness guard before migrations:

```bash
python3 source/deploy/superset/initialize_empty_db.py \
  --context mamun --resume-monitoring-views
```

Recovery verifies the matching lock/deployment, failed/inactive original Job,
exact guard-failure log with no migration-start marker, unchanged image, stopped
frontend and existing bootstrap Secret. It reuses the original web-admin
credentials in a new single-attempt Job, keeps the original failed Job, and
records the recovery in the lock. No fresh password prompt, image build, copy,
database reset or manual lock deletion is needed. The database guard runs again
inside the Job immediately before migration. Failure after migration dispatch
requires inspection and does not qualify for another automatic recovery.

## Resources and connectivity

Requests: 100m CPU / 512Mi RAM. Limits: 500m CPU / 1536Mi RAM. One Gunicorn
worker with two threads, a small local cache, no Redis/Celery/scheduled reports.
The 512Mi request reduces the scheduler reservation; it is not a cap on actual
usage. The node still needs that much unreserved memory. This is a small
interactive frontend; large queries/concurrency may need more
resources. The external PostgreSQL cluster is provisioned separately.

The Docker build installs psycopg2-binary 2.9.10 using the build host's network.
The build host needs access to Docker Hub and the Python package index. If that
host also lacks internet/DNS, prepare the image on a connected build machine and
transfer it to the node. The Kubernetes pod uses imagePullPolicy Never and
requires the exact prepared image to be imported into 192.168.1.176 first.
Save the image on the build machine, copy its archive/checksum to the node,
verify/import it using `sudo ctr -n k8s.io images import ARCHIVE`, and label
its image reference `io.cri-containerd.image=managed`. Docker push alone does
not populate the node cache. The pod has no init container or package downloads.

For the reported HTTP-registry/HTTPS-client mismatch, copy
`configure_registry.py` onto 192.168.1.176 and execute it with sudo/Python 3.11+.
It refuses any other node IP, checks the current CRI endpoint, identifies the
active containerd configuration, backs up changed files, and configures HTTP
only for 192.168.1.224:80. Existing config_path is reused. If config_path is
unset, it edits containerd 1.x/version-2 or 2.x/version-3 syntax without replacing
other runtime settings; imported/unsupported layouts stop for inspection.
The script validates the candidate, restarts containerd so the running CRI service reloads the on-disk registry
path, checks service/CRI readiness and restores the original config if that
restart fails. A direct CRI image pull and inspection verify the result. Run it
on the actual Superset node, not on the Kubernetes management machine.

From your release directory, use your own SSH account for that node:

```bash
read -r -p 'SSH username for 192.168.1.176: ' NODE_USER
scp source/deploy/superset/configure_registry.py "${NODE_USER}@192.168.1.176:/tmp/superset-configure-registry.py"
ssh -t "${NODE_USER}@192.168.1.176" 'sudo python3 /tmp/superset-configure-registry.py'
kubectl --context mamun -n superset-onprem rollout restart deployment/superset
kubectl --context mamun -n superset-onprem rollout status deployment/superset --timeout=1200s
```

The default image is the reported 4.1.2-pg-20261116065624. Pass `--image` when
verifying a later registry tag. No database, Kubernetes Secret or other registry
configuration is changed by this node helper.

URL: http://192.168.1.176:32086 (internal HTTP). Agentic API origin:
http://superset.superset-onprem.svc.cluster.local:8088. Use TLS ingress for
outside-LAN access. In agentic Reporting setup, explicitly allow internal HTTP,
configure the publishing account and existing analytics datasource ID, configure
the snapshot writer/schema and user mappings, verify private access, then test.
The metadata database is not automatically registered as an analytics datasource.

Check progress:

```bash
kubectl --context mamun -n superset-onprem get pods -o wide
kubectl --context mamun -n superset-onprem logs -f deployment/superset -c superset
```

YAML, Python and shell syntax were checked locally. No connection to the
on-prem database, cluster apply or live resource measurement was performed.
