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
transfer it to your registry. The Kubernetes pod only needs the local registry
and PostgreSQL; it has no init container or PyPI/Docker Hub download at startup.
Configure the node container runtime for your internal HTTP registry if not
already configured; image-pull failures are separate from application startup.

For the reported HTTP-registry/HTTPS-client mismatch, copy
`configure_registry.py` onto 192.168.1.176 and execute it with sudo/Python 3.11+.
It refuses any other node IP, checks the current CRI endpoint, identifies the
active containerd configuration, backs up changed files, and configures HTTP
only for 192.168.1.224:80. Existing config_path is reused. If config_path is
unset, it edits containerd 1.x/version-2 or 2.x/version-3 syntax without replacing
other runtime settings; imported/unsupported layouts stop for inspection.
The script validates the candidate, restarts containerd only if config_path must
change, checks service/CRI readiness and restores the original config if that
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
