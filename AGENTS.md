# Working on this Django deployment

When the user asks for "auto deployment", "daily deployment", or "everyday
deployment", use the root `auto-deployment.sh` workflow documented in
`AUTO_DEPLOYMENT.md`. It is run on the master: build the full application, push
to `192.168.1.224:80/cloud`, copy/import to `dw@192.168.1.99` (worker-99), then
roll out `test/cloud-live-test`, container `django`. The worker has had registry
routing failures, so do not replace this with a registry-pull-only rollout.

Preserve MCP settings/token Secret references, dependency/init containers, NiFi
and planner ConfigMap fixes, and data mounts. Do not bake over source overrides
silently. The script handles its one documented obsolete orchestrator override.

Never store SSH/sudo passwords in code, documentation, release history or Git.
The script prompts privately when SSH keys/passwordless sudo are unavailable.
Use the repository instructions for durable context; do not claim that this note
provides memory in unrelated chats or projects.
