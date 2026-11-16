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

From your on-prem release directory after pulling main:

```bash
bash source/deploy/superset/install.sh
```

Context defaults to mamun. Unless EXISTING_SUPERSET_IMAGE is set explicitly to
the version/digest matching the existing metadata, the installer reads the
current image from namespace superset, deployment superset. It rejects latest
or untagged images. It renders both image placeholders in deploy.yaml using
that same image before server validation/application. Do not apply the template
directly or choose a new Superset version as part of this database correction.

The installer checks node identity/Ready status and NodePort availability.
It refuses to switch databases if the earlier superset-onprem/superset-db
Deployment is present, and refuses to replace a Secret targeting another
backend. No existing Deployment, database, local data directory or Secret is
deleted. If the earlier local-database manifest was already installed, export
and plan a metadata migration before switching it to this database.

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

Requests: 100m CPU / 1Gi RAM. Limits: 500m CPU / 1536Mi RAM. One Gunicorn
worker with two threads, a small local cache, no Redis/Celery/scheduled reports.
This is a small interactive frontend; large queries/concurrency may need more
resources. The external PostgreSQL cluster is provisioned separately.

An init container installs psycopg2-binary 2.9.10 into an emptyDir using the same
image as the frontend. It uses uv when available and otherwise Python pip.
The node needs access to the image registry and Python package index; bake the
driver into a matching reviewed image for disconnected environments.

URL: http://192.168.1.176:32086 (internal HTTP). Agentic API origin:
http://superset.superset-onprem.svc.cluster.local:8088. Use TLS ingress for
outside-LAN access. In agentic Reporting setup, explicitly allow internal HTTP,
configure the publishing account and existing analytics datasource ID, configure
the snapshot writer/schema and user mappings, verify private access, then test.
The metadata database is not automatically registered as an analytics datasource.

Check progress:

```bash
kubectl --context mamun -n superset-onprem get pods -o wide
kubectl --context mamun -n superset-onprem logs deployment/superset -c postgres-driver
kubectl --context mamun -n superset-onprem logs -f deployment/superset -c superset
```

YAML, Python and shell syntax were checked locally. No connection to the
on-prem database, cluster apply or live resource measurement was performed.
