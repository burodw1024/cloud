# Recover a read-only SQLite backup failure

The launcher stops Django before backup and migration. If the Job fails at
`src.backup(dst)` with `sqlite3.OperationalError: attempt to write a readonly database`,
Django remains at zero replicas and the release lock remains. Starting a new
release cannot pass its initial Django runtime check while the app is stopped.

An interrupted SQLite writer can leave a hot rollback journal. The old backup
script opened its source `mode=ro`, preventing SQLite from recovering that journal.
The normal backup script now opens existing sources with `mode=rw` (never create).
Actual filesystem permissions or a read-only mount can also cause failures; the
recovery Job prints file owner/mode diagnostics without changing permissions.

Pull the latest helper into the source checkout. Locate the failed release's
`private/migration.json`, and use its containing directory as STAGE:

```sh
python3 source/deploy/resume_release.py "$STAGE" \
  --retry-backup --context mamun --url https://192.168.1.144:31135
```

This checks the stopped Deployment UID, failed/inactive original Job, exact lock
ownership, original error log, absence of other active pods mounting app data,
saved source hash, and absence of an earlier recovery Job. It creates one recovery
Job with the imported image, existing environment/Secrets, and original mounts.
The Job retains the failed backup, writes a new `RELEASE-backup-retry` directory,
allows SQLite's own journal recovery, verifies the new backup, then runs migrations
and the original post-migration checks. It never restores a backup or deletes WAL,
journal or SHM files. Once successful, it applies the saved release manifests,
verifies all expected app/worker/HTTPS containers and HTTPS readiness, and removes
the release lock. Original failed Job and both backup directories are retained.

If recovery fails, inspect its logs and retain the lock. Do not rerun blindly,
scale Django up, overwrite backups, chmod recursively, or delete SQLite journals.
