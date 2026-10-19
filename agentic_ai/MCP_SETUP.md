# MCP gateway integration and deployment

## Simple setup in Django

1. Sign in as an administrator and open `/agentic/setup/`.
2. Save the full gateway endpoint and optional token, then click **Find tools**.
3. Click **Check model**. It tests the existing project model without running a gateway tool; native calls and structured JSON plans are supported.
4. Select tools, tick their allowed roles, and click **Save and enable tools**.
5. Open `/agentic/` and click **Try a loan aging question**, then **Run query**.

No shell commands or role JSON are needed for this workflow. Changing the saved
connection pauses MCP execution until setup is completed again. The setup page
requires staff access plus permission to add/change Agent Tools and change System
Settings. Ordinary signed-in users can use the question page; run history is at
`/agentic/dashboard/` and requires the relevant view permissions.

## What selects the tool

For `/agentic/api/query/` (and the query form on `/agentic/`), when MCP mode is
enabled, Django uses the project's existing dynamic `OLLAMA_MODEL`/`GEN_MODEL`
resolver and `OLLAMA_URL`. The MCP gateway supplies tool descriptions and schemas.
The model chooses one permitted tool and arguments; Django validates them,
checks current roles again, calls the gateway, and returns the actual result.

This does not replace the separate legacy chat/policy/HR routes. MCP mode is
explicitly enabled on the agent endpoint. `MCP_GATEWAY_ENABLED=false` retains
the previous local keyword router. No tool-selection fallback is used after an
MCP error because it could accidentally run a different tool.

The project model can select tools through native function calls or validated
structured JSON. When Ollama explicitly reports that a model does not support
native tools, Django asks the **same model** for a schema-constrained JSON plan,
then applies the same tool allowlist, role checks and argument validation.
No tool has executed when this inference fallback occurs. Other upstream errors
do not trigger fallback. The project fallback is `aya-23-8b:q5`; its live selection
quality still needs verification. The integration never silently changes models.
Use **Check model** in the setup page or the readiness command before enabling tools.

## Gateway and project settings

In `/admin-tools/settings/`, open **MCP Gateway**. Set the full Streamable HTTP
endpoint and, if required, a bearer token. The supplied base address is
`http://192.168.1.99:30444`; `/mcp` is an assumed endpoint path and still needs
verification. Redirects are deliberately rejected: enter the exact path,
including a trailing slash if the server requires one. Legacy SSE/stdio
transports are not supported by this adapter.

Database System Settings override environment variables and use the project's
existing setting-cache invalidation. Environment deployment settings:

```dotenv
MCP_GATEWAY_ENABLED=true
MCP_GATEWAY_URL=http://192.168.1.99:30444/mcp
MCP_TIMEOUT=30
MCP_REQUEST_TIMEOUT=90
# Only when required; keep secrets outside version control:
# MCP_GATEWAY_TOKEN=...
```

`MCP_TIMEOUT` is per network operation; `MCP_REQUEST_TIMEOUT` is the overall
operation deadline including discovery, inference and execution, followed by
SDK cancellation/cleanup. Keep worker/ingress timeouts above this deadline plus
cleanup grace. The existing Gunicorn timeout is 120 seconds.

Leave model overrides unset to use the project's existing model. Advanced
OpenAI-compatible configuration is supported with `MCP_MODEL_PROVIDER=openai`,
`MCP_MODEL_BASE_URL` (API base, usually ending in `/v1`), `MCP_MODEL`, and optional
`MCP_MODEL_API_KEY`. These are runtime overrides, not a second gateway model.

## Discover, register and verify

From the deployment network:

```sh
python manage.py list_mcp_tools
python manage.py list_mcp_tools --check-model
```

The second command performs a synthetic tool-selection probe; it does
not execute a gateway tool. Neither command changes permissions or grants roles.
At most 64 tools, 20 listing pages, 64 KiB per schema and a 256 KiB catalog are
accepted. Narrow the gateway catalog if the discovery command reaches a limit.

In `/admin/`, add each exact discovered name under Agent Tools, enable Is active,
and set Allowed roles, for example:

```json
["manager", "branch_staff", "cashflow", "core"]
```

Groups match case exactly. Users without groups resolve to `staff`; superusers
resolve to `admin` but still require an active registered tool with a nonempty
role list. Only authenticated active users may call the private API. Empty role
lists deny access. Keep write-capable tools behind Requires approval. An approval
record blocks execution: **an approval execution/resume workflow is not included**,
and changing its status in admin does not run the tool.

Role authorization is at tool level. The backend tool must separately enforce
branch/data entitlements for the authenticated upstream identity. This adapter
uses a server bearer credential; it does not impersonate individual users at
the gateway. Do not grant a broad report tool to users lacking the corresponding
backend data access.

## Request and reliability contract

The question page and dashboard include a query form that sends a session CSRF token and
an idempotency key. API clients need a valid Django login session and CSRF token:

```http
POST /agentic/api/query/
Content-Type: application/json
X-CSRFToken: <session token>
Idempotency-Key: <unique key for this logical request>

{"message":"Show the loan aging report for branch 0253 as of 2026-09-30."}
```

Expected arguments for the supplied aging schema:

```json
{"branchCode":"0253","reportDate":"2026-09-30"}
```

Leading zeros are preserved. Both fields in the supplied schema are optional;
add `required` in the gateway schema if omission must never run an unfiltered
report. Add `"format":"date"` to `reportDate` to enforce calendar-date validation.
Arbitrary input fields, invalid declared types/formats and external schema
references are rejected. Schemas with custom `$id` reference bases are currently
unsupported. One tool is allowed per request; there is no multi-step agent loop
or conversation-memory planner in this change.

Private requests are limited to 16,000 query characters, 64 KiB bodies and 20
new requests per user per minute. Use the **shared SQLite cache** for the single-node deployment;
local memory cannot coordinate multiple workers. Idempotency keys are scoped to
the user and retained for 15 minutes. A repeated key with the same request returns
the previous response; a pending or mismatched request returns 409. Keys provide
bounded duplicate protection, not permanent exactly-once execution. Backends for
state-changing tools must implement durable idempotency themselves.

An audit attempt is persisted before external calls and its chosen arguments
before dispatch. If audit storage is unavailable, the tool does not execute.
Audit history is read-only in Django admin. Viewing logs/approval lists requires
the corresponding Django view permissions in addition to staff access.

Failures have a `code`, a request ID (`X-Request-ID`) and appropriate HTTP status:
400 invalid request, 401 login required, 403 permissions/CSRF, 409 duplicate in
progress/conflict, 422 invalid model arguments, 429 rate limit, 502 upstream/tool
failure, 503 configuration/coordination unavailable, and 504 timeout. Approval
requests return 202. Results carry `tool_executed` on completed calls or
clarifications, so prose is distinguishable from a retrieved report.

No tool call is automatically retried. If a connection fails after dispatch,
`outcome_unknown=true` tells the client to check the backend before retrying.
Upstream bodies and credentials are excluded from error messages/logs. Audit
records include user queries, arguments and a bounded answer: protect access and
set a retention policy appropriate to the report data.

Model response reads are limited to 1 MiB; formatted tool results to 2 MiB;
gateway transport responses to 8 MiB. Request narrower reports for larger data.

## Production profile

The development base settings are **not production settings**: they include
`DEBUG=True`, a development secret and SQLite. A separate opt-in profile is now
provided; it does not migrate or replace an existing database automatically.

The project now uses **Django 5.2.18 LTS**. Production dependencies are verified
on Python 3.11, matching the Docker image. `requirements.production.lock.txt`
records the tested complete environment and is used by the Docker build. The
image now defaults to the production settings and `buro_first.wsgi` entry point.
Use `deploy/production.env.example` as the configuration template; placeholder
secrets are rejected at startup. Install the lockfile (or the production
requirements for a fresh resolution). No PostgreSQL or Redis server is required.
The current production profile uses SQLite and an atomic shared SQLite cache
on the persistent `/data` volume. It supports one replica on worker-99.

```dotenv
DJANGO_SETTINGS_MODULE=buro_first.settings_production
DJANGO_SECRET_KEY=<unique random secret of at least 50 characters>
DJANGO_ALLOWED_HOSTS=192.168.1.144,192.168.1.99
DJANGO_CSRF_TRUSTED_ORIGINS=https://192.168.1.144:31135
DJANGO_DATA_DIR=/data
```

The profile requires HTTPS, disables debug, uses secure cookies, and enables
HSTS for the current host. Nginx replaces forwarding headers. The release
launcher generates a private configuration file and asks for the initial login.
The release stops the application, backs up SQLite using its backup API, then
runs migrations before restarting it. Use local/block storage with SQLite file
locking; do not use an NFS/SMB volume. This is a single-node deployment with
release downtime. Existing database files are not automatically imported.

Run in staging after provisioning dependencies/services:

```sh
python manage.py check --deploy --fail-level ERROR
python manage.py migrate --plan
python manage.py list_mcp_tools --check-model
```

Review/apply migrations through your existing release process, then verify the
real loan report with authorized and unauthorized users. Deployment checks reject
local request caches and debug mode when MCP is enabled. Django has been
upgraded to 5.2.18 with explicit user approval. The main logout control now uses
a CSRF-protected POST, as required by modern Django. Existing data has not been
migrated or changed. In disposable PostgreSQL 16 and Redis 7 services, the full
project migration graph applied successfully, migration drift was absent,
production checks reported no errors, and the regression suite passed.

Two deployment warnings remain deliberately visible: HSTS across all subdomains
and browser preload are not enabled by default because the deployment's domain
and HTTPS coverage are unknown. After confirming coverage, opt in with
`DJANGO_HSTS_INCLUDE_SUBDOMAINS=true` and `DJANGO_HSTS_PRELOAD=true`. Do not enable
these merely to suppress warnings.

## Verification and outstanding limits

```sh
python -m django test agentic_ai.tests --settings=agentic_ai.tests.settings
```

Focused tests use a temporary SQLite database, mock model/gateway failures and
exercise HTTP authentication, CSRF, validation, coordination, role checks,
configuration, schemas, deadlines and audit failures. A real local MCP server
also verified initialize -> list -> project-model API -> call -> audit, with a
simulated model response. This proves transport wiring, **not actual model
selection quality**.

The cloud proxy denies `192.168.1.99` with `403 Domain forbidden`. The live
endpoint path, gateway credentials, real model capability, report output, your actual Redis
and PostgreSQL deployment therefore remain unverified (the profile was tested
against disposable local services). Broader legacy modules
have not received a full production/security audit. MCP stays disabled until
explicit deployment activation and readiness checks pass.

### Upgrade verification record

The authorized upgrade was checked with Python 3.11.17 and Django 5.2.18:

- Production dependency installation and `pip check` passed.
- Full project Django system checks and migration-drift checks passed.
- All 53 regression tests passed with the full application registry, disposable PostgreSQL 16, and Redis 7.
- Production deployment checks reported no errors; the two documented HSTS policy warnings remain visible.
- Main chat, agent question page, and guided setup rendered successfully under production settings.
- Redis atomic request coordination and Gunicorn startup configuration passed.
- Real local MCP transport was verified with simulated model responses; structured JSON selection was tested separately.

The existing user database and deployed services were not migrated or modified.
The production Dockerfile was updated to the tested dependency lock and settings;
a complete Docker image build was not performed in this session.

### Subsequent authorization recheck

The focused regression suite now passes 55 tests. Cached idempotent report
responses recheck current tool authorization before replay: revoked group
membership, deactivated tools and deleted registrations cannot expose an old
report through its original idempotency key. New regression cases cover role
revocation and tool deactivation. The previously recorded PostgreSQL/Redis
validation remains a separate 53-test run. Live gateway/data entitlements and
operational load/recovery verification are still outstanding.
