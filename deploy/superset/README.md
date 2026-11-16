# Small on-prem Superset deployment

`deploy.yaml` deploys Superset 5.0.0 and PostgreSQL 16 to the node named
`superset` (192.168.1.176). Namespace `superset-onprem` and NodePort 32086 are
separate from the existing `superset` namespace and port 32088. Do not apply this
manifest over the older installation or assume it migrates that installation's
dashboards. The installer verifies the node's IP, Ready condition and port use.

Run `bash source/deploy/superset/install.sh` from your release folder after
pulling GitHub main. Kubernetes access defaults to context mamun; KUBE_CONTEXT
can select another context explicitly. It generates an initial Secret only
when absent and retains it on subsequent runs. Keep that Secret and the
PostgreSQL data directory together across reinstallation; recreating passwords
does not change an existing PostgreSQL database or existing Superset user.

Both Deployments use one replica and Recreate. Node selection is required for
the local PostgreSQL hostPath `/opt/buro-ai/superset-onprem/postgres`. Back up
the metadata database and Secret; a node/disk failure does not move local data
elsewhere. Superset is served over internal HTTP at
`http://192.168.1.176:32086`; use a TLS ingress for access outside the LAN.

Resource requests total 200m CPU and 1152Mi RAM; limits total 1 CPU and 1920Mi
RAM. The application uses one Gunicorn worker, two threads and a bounded local
cache. Redis, Celery, scheduled reports and asynchronous queries are omitted.
This is a small interactive installation, not high availability; large SQL
results/concurrent dashboards may require more memory and CPU.

The official lean Superset image lacks its PostgreSQL driver. An init container
using the identical image installs psycopg2-binary 2.9.10 into a shared emptyDir;
both the main app and init container use Python 3.11. The node must be able to
pull Docker Hub images and the init container must reach the Python package
index at each new pod creation. For disconnected environments, bake the driver
into a reviewed image and push it to the local registry instead. Do not remove
the driver init step without including the dependency in your app image.

The app waits for PostgreSQL, applies metadata migrations, creates the initial
administrator if absent, initializes Superset, then starts Gunicorn. A startup
probe allows 15 minutes and rollout allows 20 minutes. View progress with:

```bash
kubectl --context mamun -n superset-onprem get pods -o wide
kubectl --context mamun -n superset-onprem logs deployment/superset -c postgres-driver
kubectl --context mamun -n superset-onprem logs -f deployment/superset -c superset
```

For the agentic integration, use API origin
`http://superset.superset-onprem.svc.cluster.local:8088` and browser origin
`http://192.168.1.176:32086`. In Reporting setup explicitly allow trusted
internal HTTP. Register the intended analytics PostgreSQL database in Superset,
configure its dedicated snapshot writer/schema and employee user mappings,
grant publishing permission, verify private access, then test the connection.
Superset's own metadata database is not automatically an analytics datasource.
This deployment does not populate agentic credentials or user mappings.

Manifest/Python/shell syntax was checked locally. No on-prem installation was
executed or measured, and these version pins are not a claim of latest release.
