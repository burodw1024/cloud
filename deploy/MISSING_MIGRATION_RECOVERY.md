# Release stopped before its migration Job appeared

Use `resume_release.py --start-missing-job` only for a stopped release whose
original migration Job is missing and whose release-specific backup directory
does not exist. The original launcher must no longer be running. Do not remove
the release lock or use the ordinary launcher to restart this release.

The helper verifies the saved deployment identity, zero replicas, matching lock,
absence of an original migration Job or status snapshot, no original migration
pods, and no active namespace pods mounting the application's hostPath. It
rejects any previously dispatched recovery attempt. The new Job uses the saved
imported image, verifies the migration script hash against the saved build
source, and refuses to execute if the backup directory exists. The original
script creates that directory exclusively before backup or migration.

After migration, the helper applies the saved deployment and Nginx configuration
and checks readiness, runtime settings, gateway code, Nginx timeouts, and HTTPS.
Only then does it release the lock. It never restores or deletes the database.

For the reported release:

```bash
set -Eeuo pipefail
cd /home/datawarehouse/cloud-release.sYkjvDuW
git -C source -c pull.rebase=false pull --ff-only origin main
unset ENV_FILE
python3 source/deploy/resume_release.py \
  "$PWD/agentic-release.lhVR39cf/private" \
  --context mamun --url https://192.168.1.99:31135 \
  --start-missing-job
```

This resumes the saved release; it does not deploy newer source code pulled by
the first command. Run a normal release after recovery if a newer image is
needed. If the helper stops, retain the lock and inspect the reported Job or
condition rather than repeating or force-unlocking the release.
