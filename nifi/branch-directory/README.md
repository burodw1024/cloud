# OracleBranchDirectory — NiFi 2.4 NAR

A compiled Java 21 processor with the Oracle JDBC driver included. Oracle
connection, schema, table, column mappings and API credential are configured
in the NiFi **Properties** panel. No Groovy script, DBCP service, environment
credential or extra JDBC driver installation is required for this processor.

## Install

Download `branch-directory-nar-1.0.0.nar` from the repository's `downloads/`
directory and verify its adjacent `.sha256` file. The complete release ZIP also
includes the source, build report and this guide.

1. Stop NiFi using your normal service/container procedure.
2. Copy the NAR into the configured NiFi NAR library directory (normally
   `$NIFI_HOME/lib/`) on **every NiFi node**. Make the file readable by the NiFi
   service account. Keep only one version of this custom NAR installed. Do not
   remove or replace your existing loan-report NARs.
3. Start NiFi. Add processor **OracleBranchDirectory**, bundle
   `org.buro.nifi:branch-directory-nar:1.0.0`.
4. Configure its Properties below. Use a SELECT-only Oracle account; the processor
   executes only its own SELECT. Configure password and API token through sensitive
   NiFi parameters rather than copying credentials into documentation or Git.
5. Wire the HTTP flow described below. Start the processor and test an authorized
   GET and an unauthorized GET before syncing the application directory.

## Processor properties

| Display name | Value / purpose |
| --- | --- |
| Oracle JDBC URL | `jdbc:oracle:thin:@//YOUR-ORACLE-HOST:1521/YOUR-SERVICE` |
| Oracle Username | Account with SELECT on the source table |
| Oracle Password | Sensitive database password |
| Oracle Schema | Optional table owner, for example `DW_OWNER`; unset uses current schema/synonym |
| Oracle Table | `DW_GC_BRANCHLIST2` by default; configurable |
| Directory API Token | Sensitive dedicated random token, at least 32 bytes; match Django `LOAN_DIRECTORY_TOKEN` |
| API Path | `/api/reference/branches` by default; configurable |
| Query Timeout Seconds | `30`, allowed 1–120 |
| Connect Timeout Seconds | `10`, allowed 1–30 |
| Maximum Branches | `5000`, allowed 1–5000; overflow fails the entire request |
| Fetch Size | `256`, allowed 1–2000 |
| DIVISION Code Column | `DIVISIONCODE` |
| DIVISION Name Column | `DIVISIONNAME` |
| ZONE Code Column | `ZONECODE` |
| ZONE Name Column | `ZONENAME` |
| AREA Code Column | `AREACODE` |
| AREA Name Column | `AREANAME` |
| BRANCH Code Column | `BRANCHCODE` |
| BRANCH Name Column | `BRANCHNAME` |

Schema, table and column names accept **one unquoted Oracle identifier** each
(up to 128 characters). Put owner in Oracle Schema, not `OWNER.TABLE` in Oracle
Table. SQL fragments, quoted identifiers and HTTP-supplied identifiers are
rejected. Input columns can be mapped from differently named tables, but must
represent the same branch hierarchy. The JSON field names and logical
`source: DW_GC_BRANCHLIST2` remain stable for compatibility with Django.

The driver is bundled `ojdbc11` 23.6.0.24.10. Validate connectivity against your
Oracle server version during installation; no live database was available in
the cloud build. JDBC URLs cannot embed username/password; use their separate
fields. For encrypted Oracle connections, configure the appropriate TCPS URL
and your Oracle/JVM trust settings using your existing operational procedure.

## HTTP flow

```text
Existing HandleHttpRequest on port 8001
  → RouteOnAttribute (new directory path, preserve other routes)
  → OracleBranchDirectory
  → response → HandleHttpResponse
```

Reuse the existing listener and **same StandardHttpContextMap**. Do not start
a second listener on port 8001 or replace existing loan dispatch.

- Add `/api/reference/branches` to the listener's allowed paths, preserving
  existing paths. Allow GET. For a dedicated listener the path regex is
  `^/api/reference/branches$`.
- RouteOnAttribute dynamic property `branch_directory`:
  `${http.request.uri:equals('/api/reference/branches')}`. Route that match to
  this processor; keep unmatched requests on their existing route.
- Set this processor's concurrent tasks to **1** initially. Use bounded queues,
  short expiration and an HTTP context expiration greater than the configured
  connect plus query/read timeouts, for example **90 seconds** with defaults.
- `response` is the only relationship. It carries every normal HTTP outcome,
  including 401 and 503; connect it to HandleHttpResponse. The original
  `http.context.identifier` is preserved. Do not load-balance these connections
  to another node in a NiFi cluster.
- HandleHttpResponse HTTP Status Code: `${http.status.code}`. Add response
  headers `Content-Type = application/json` and `Cache-Control = no-store`.
  Connect its success/failure relationships to your existing completion handling.
- Use HTTPS on the listener or your internal TLS proxy. Restrict endpoint access
  to the backend/administrators. Do not log request attributes. The processor
  removes Authorization attributes before transferring its response, but earlier
  request provenance can retain them: restrict provenance/queue access and use
  your platform's sensitive-attribute controls.

If changing API Path, update the listener path, routing expression and Django URL
together. No request SQL, filter, pagination or table parameter is accepted.

## API and Django backend

`GET /api/reference/branches` with `Authorization: Bearer <dedicated token>`
returns the same complete snapshot contract as the previous Groovy option:

```json
{"success":true,"complete":true,"version":1,"source":"DW_GC_BRANCHLIST2","row_count":1,"rows":[{"DIVISIONCODE":"01","DIVISIONNAME":"Example division","ZONECODE":"007","ZONENAME":"Example zone","AREACODE":"0004","AREANAME":"Example area","BRANCHCODE":"00253","BRANCHNAME":"Example branch"}]}
```

These are illustrative codes, never seeded data. Required zone/branch code/name
must be present. Division and area can both be empty code/name pairs. Leading
zeros are preserved. Duplicate names are valid; duplicate/conflicting branch
codes or inconsistent hierarchy fail. Actual codes must fit the application's
32-character identifier format even though Oracle storage allows 500 bytes.
Empty, oversized (5000 rows / 4 MB), invalid or failed snapshots never return
partial rows or `complete:true`. Responses expose no SQL exception details.

Status codes: **200** complete directory, **400** unsupported query string,
**401** invalid/missing token, **404** wrong path, **405** wrong method,
**503** database/validation/unavailable/oversized snapshot. Listener-level
rejections use that listener's own response.

Set deployment environment/Secrets in the Django backend:

```text
LOAN_DIRECTORY_URL=https://YOUR-INTERNAL-API/api/reference/branches
LOAN_DIRECTORY_TOKEN=<same value as the processor's Directory API Token>
# Optional mounted private CA:
LOAN_DIRECTORY_CA_BUNDLE=/path/to/internal-ca.pem
```

For the existing private plaintext listener, use:

```text
LOAN_DIRECTORY_URL=http://192.168.1.139:8001/api/reference/branches
LOAN_DIRECTORY_ALLOW_HTTP=1
LOAN_DIRECTORY_TOKEN=<same value as the processor's Directory API Token>
```

The **NAR** takes the token from its sensitive property, not a NiFi environment
variable. Django continues using its deployment secret. Prefer TLS in production.

Apply the latest Django migrations and restart workers after environment changes.
Go to **Data access → Branch & zone directory → Fetch sync preview**, then enter
a reason and apply. Staff administrators require `manage_loan_directory`.
Existing user policies and paused entries are preserved. New entries default to
paused unless the administrator explicitly chooses activation. Division/area
are metadata; existing branch/zone permission tiers continue to apply. Directory
sync does not implement or replace the signed financial-report authorization
contract in `deploy/LOAN_DATA_ACCESS.md`.

## Build and checks

Requires Java 21 and Maven 3.9+:

```bash
mvn -B -ntp -f nifi/branch-directory/pom.xml clean package
```

Output: `nifi/branch-directory/nar/target/branch-directory-nar-1.0.0.nar`.
Tests use real NiFi 2.4 TestRunner and mocked JDBC. The build generates the
extension manifest used by NiFi to list the processor and its properties.
There has been no live NiFi restart, Oracle connection or on-prem deployment.
