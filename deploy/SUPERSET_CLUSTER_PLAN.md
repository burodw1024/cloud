# Superset placement from the supplied cluster inspection

Placement selected by the user: `etlreg-virtual-machine` (`10.50.1.29`) for the
Superset web pod and initialization Job. The user selected the existing Zalando
PostgreSQL HA cluster `acid-postgres-test` in `postgres-ha-test` for database
connections. Existing PostgreSQL workloads and storage stay in place.

Status: placement selected; no cluster access is configured in this cloud
workspace and no Kubernetes resources have been applied. Metrics API is unavailable. The following
headroom is allocatable capacity minus existing requests, not observed free RAM
or measured CPU utilization.

The user subsequently confirmed that Superset is unused and authorized a fresh
installation. `install_superset.py` now performs live collision checks rather
than requiring more manual namespace inventory. It creates dedicated databases
and accounts on the selected HA cluster and preserves the standalone PostgreSQL
workload and any unrelated namespace resources.

| Node | Unreserved CPU | Unreserved memory | Placement assessment |
|---|---:|---:|---|
| etlreg-virtual-machine | 2.90 | 14.76 GiB | Selected by user; verify runtime usage before rollout |
| worker-01-node | 2.95 | 6.44 GiB | Alternate; also hosts a PostgreSQL HA member |
| datalake | 6.50 | 5.75 GiB | Less memory headroom; database member present |
| k1 | 6.85 | 5.58 GiB | Existing memory limits already approach capacity |
| superset | 4.65 | 1.34 GiB | Insufficient for a 2 GiB memory request |
| worker-99 | 0.38 | 0.87 GiB | Insufficient headroom; heavily overcommitted |

The control-plane node has a NoSchedule taint and is excluded. The single-vCPU
kamrul node cannot accommodate a 1-CPU request alongside current reservations.
All supplied nodes report no memory/disk/PID pressure, but that does not establish
their actual utilization or the load of pods without resource requests.

Initial sizing to validate: one Superset web pod requesting 1 CPU and 2 GiB,
limited to 2 CPU and 4 GiB. Run the migration/init Job before the web pod, so they
do not simultaneously consume their full startup reservations. Size workers and
Redis separately if asynchronous queries or scheduled tasks are enabled later.

## Existing PostgreSQL resources

- A PostgreSQL 15 pod already runs in namespace `superset`:
  `superset-postgres-0` on `worker-99`.
- Its service is `superset-postgres.superset.svc.cluster.local:5432`; this is
  no longer the selected connection destination. Preserve its data until any
  existing Superset installation and metadata have been inspected.
- Selected destination: the existing Zalando PostgreSQL 17 HA cluster in
  `postgres-ha-test`, as explicitly requested by the user.
- The supplied service inventory includes `acid-postgres-primary-nodeport`,
  whose selector includes `spilo-role=master`. Candidate in-cluster endpoint:
  `acid-postgres-primary-nodeport.postgres-ha-test.svc.cluster.local:5432`
  (assuming the normal cluster DNS domain). Verify ready endpoints and the
  current primary before use. Do not connect to pod IPs, the `-0-nodeport`
  pod-pinned service or the replica service for Superset metadata writes.
- Subsequent user inspection confirms `acid-postgres-test-0` currently carries
  the `master` role, `acid-postgres-test-2` carries `replica`, and service
  `acid-postgres-primary-nodeport` has endpoint `172.25.25.196:5432`.
  This confirms the service has an endpoint, not SQL authentication, database
  readiness or that the application can reach it. Continue to configure the
  service DNS name, never this transient endpoint address. The role of pod `-1`
  was absent in the supplied output; do not claim all HA members are healthy.
- No new PostgreSQL Deployment/StatefulSet is needed for Superset installation.
  Separate metadata and analytics databases/users may be provisioned on the
  selected HA cluster after its current databases and credential references are
  known. Use a dedicated metadata owner and separate analytics writer/reader
  accounts. Do not run Superset as PostgreSQL superuser.
- The existing Superset PostgreSQL pod sits on the overloaded worker-99 node.
  Check its resource settings and live usage. Do not relocate it casually: the
  only listed storage class is local storage (`kubernetes.io/no-provisioner`) and
  persistent-volume node affinity must be checked before any database move.

## Required inventory before rendering a deployment

```bash
kubectl -n superset get deploy,sts,svc,pods,pvc -o wide
kubectl -n superset get endpoints superset-postgres -o wide
kubectl -n postgres-ha-test get pods -l cluster-name=acid-postgres-test -L spilo-role
kubectl -n postgres-ha-test get endpoints acid-postgres-primary-nodeport -o wide
kubectl -n postgres-ha-test get secrets -o custom-columns=NAME:.metadata.name,TYPE:.type
python3 deploy/inspect_superset_namespace.py
```

The helper prints image versions, resource requests and configuration reference
names, with literal environment values and Secret data omitted. An existing
Superset web deployment may need repair or relocation rather than replacement.
Preserve its `SECRET_KEY`, metadata database and existing application image until
the current installation is understood and backed up. Never run metadata schema
upgrades against an existing database without a compatible version and backup.
If the existing Superset instance uses the standalone PostgreSQL server, moving
it to the selected HA cluster requires a deliberate metadata backup/restore;
changing the connection alone would not preserve its dashboards. PostgreSQL
Secrets are namespace scoped: create the application credential binding in the
Superset namespace without printing Secret values or sharing them in chat.

The next package should use the confirmed namespace, existing credential
references, an appropriate pinned Superset image with PostgreSQL driver,
node affinity, resource limits, readiness/startup probes, an internal Service,
and an explicit initialization Job. External exposure and agent integration
follow only after initialization and private-access checks pass.
