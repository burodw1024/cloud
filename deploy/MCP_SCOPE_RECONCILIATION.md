# Reconcile the on-prem MCP permission hotfix

The on-prem gateway had explicit per-user tool permissions and application-owned
loan scope that were missing from GitHub main. A normal deployment stopped at the
MCP compatibility check before building an image. This release preserves that
reviewed gateway, adds its permission model and application-scope dependencies,
and retains the savings-only report catalog selection.

The new migration creates UserToolAccess on a fresh database. If the live hotfix
already created the permission tables under another migration name, it validates
their columns, types, nullability, uniqueness and foreign keys and adopts them
without clearing assignments. Incompatible schemas stop migration. Reversal is
blocked while permission rows exist. Existing tool access modes are unchanged;
new tools keep the GitHub SIGNED default to avoid changing existing provisioning.

The compatibility list adds only the exact reconstructed, reviewed live gateway
SHA256, with a fixture and a test rejecting further edits. No compatibility check,
image test, database backup, or deployment verification is disabled. APP-mode
cached reports also require current scope metadata.

## Deploy after merging into main

Run from the existing on-prem release directory, with source on main and clean:

```bash
set -Eeuo pipefail
cd /home/datawarehouse/cloud-release.sYkjvDuW
git -C source -c pull.rebase=false pull --ff-only origin main
unset ENV_FILE
bash source/deploy-agentic-updated.sh --check-source
bash source/deploy-agentic-updated.sh
```

The launcher builds the pulled commit, runs isolated tests in the image, pushes to
the registry, copies/imports on worker-99, migrates with its existing backup and
writer-stop procedure, and verifies the Kubernetes rollout and HTTPS report flow.
It still checks both the running gateway and any retained router ConfigMap. An
unrecognized additional edit remains blocked. Do not add arbitrary hashes to the
compatibility list to force a release through.

## Validation

366 agentic_ai tests pass, including savings-versus-delivery tool selection,
explicit access expiry, application-scope dispatch and revocation, adoption of
populated permission tables, rejection of incompatible schema, and refusal to
reverse away populated grants. Django reports no missing model migrations.

These are isolated local checks. No live NiFi/Oracle query or on-prem image build
was performed in the development environment. Savings tool activation, declared
schema, group/user assignment, endpoint bearer token and NiFi branch scope still
must be configured correctly on-prem.
