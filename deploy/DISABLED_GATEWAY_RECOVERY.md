# Recover a release stopped by a paused MCP gateway

If the Job logged a completed SQLite backup and migrations followed by
`AssertionError: Expected the existing MCP gateway to be enabled; configuration was preserved.`,
use the saved release stage and its imported image:

```bash
git -C source pull --ff-only origin main
python3 source/deploy/resume_release.py \
  /home/datawarehouse/cloud-release.sYkjvDuW/agentic-release.DvwKa2RS/private \
  --context mamun --url https://192.168.1.99:31135 \
  --preserve-disabled-gateway
```

This verifies the failed Job, release lock, stopped writers, saved image source
hash, backup integrity, and absence of pending migrations. It runs only the
post-migration phase with the saved image, preserving the disabled gateway.
External gateway/model probes run only when the gateway is enabled; skipped
probes are recorded as not passed. Django deployment checks remain required.
The helper then rolls out the saved manifests, checks application readiness,
runtime configuration, gateway source, Nginx, and HTTPS before releasing the lock.
It retains the original backup and Jobs and never restores the database.

Do not rerun the full deployment launcher for this failed release. If a recovery
Job already exists, inspect it rather than creating another. Once recovery
completes, finish tool discovery, model checks and access configuration through
Integration settings to enable gateway reporting. A recovered paused gateway
does not mean a live report was verified.
