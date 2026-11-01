# Oracle branch directory API and Django sync

**Recommended:** use the compiled [OracleBranchDirectory NAR](../../../nifi/branch-directory/README.md) for configurable Oracle URL, username, sensitive password, schema/table and column fields. The NAR bundles its JDBC driver. The Groovy flow below remains an alternative and is not needed with the NAR.

This is a separate, read-only master-data endpoint. It does not replace the loan
report flow, grant access to users, or implement financial query authorization.
The repository does not contain your live NiFi controller-service IDs, Oracle
credentials or schema owner. These instructions configure those on-prem.

## NiFi setup

Uses stock **HandleHttpRequest**, **RouteOnAttribute**, **ExecuteScript** (Groovy)
and **HandleHttpResponse**, plus an Oracle **DBCPConnectionPool** and a shared
**StandardHttpContextMap**. Script contract checks use the NiFi 2.4.0 API and
Groovy 4; the live installed version has not been verified. ExecuteScript needs
the Groovy engine enabled and permission to execute restricted scripts.

1. Use a database account with only SELECT on the real owner's
   `DW_GC_BRANCHLIST2`. Configure Oracle JDBC driver, URL, username and sensitive
   password in a DBCPConnectionPool. Use a dedicated bounded pool, maximum wait
   about 5 seconds. Install the Oracle JDBC jar according to your NiFi version.
   If the account is in another schema, have the DBA create a **private synonym**
   `DW_GC_BRANCHLIST2` pointing at that owner's table and grant SELECT. No owner
   is guessed and no HTTP parameter becomes SQL.
2. Generate a dedicated random directory credential (at least 32 bytes). Store it
   as `LOAN_DIRECTORY_TOKEN` in the NiFi process/container secret environment and
   restart that process as appropriate. Set the same credential in Django's
   deployment secret. Do not reuse the financial `LOAN_ACCESS_SIGNING_KEY`.
3. Reuse the existing API listener on port **8001** and its context map. Do not
   start a second listener on the same port or overwrite existing loan routes.
   Add the exact path `/api/reference/branches` to the listener's Allowed Paths
   expression, preserving its existing paths/methods. Permit GET. For a new
   dedicated listener, Allowed Paths is `^/api/reference/branches$`.
   Require HTTPS using the listener's SSL context or your existing TLS proxy.
4. Add a RouteOnAttribute route before the existing dispatch:

   | Property | Value |
   | --- | --- |
   | Routing Strategy | Route to Property name |
   | `branch_directory` | `${http.request.uri:equals('/api/reference/branches')}` |

   Connect `branch_directory` to the new ExecuteScript. Preserve the unmatched
   route to the existing dispatch. Configure request/queue limits and a context
   expiration longer than the DB timeout, for example 60 seconds. Use 1 concurrent
   task initially. In a NiFi cluster keep the request/response on the same node;
   do not load-balance these connections between nodes.
5. Configure ExecuteScript:

   | Property | Value |
   | --- | --- |
   | Script Engine | Groovy |
   | Script File | absolute path of installed `branch-directory.groovy` |
   | Script Body | empty |
   | Dynamic property `dbcpServiceId` | UUID of the enabled DBCPConnectionPool |

   The pool must be visible in this processor's process group or a parent. Copy
   the script to the same path on every NiFi node that can execute the flow.
6. Connect ExecuteScript `success` to HandleHttpResponse. Use the **same**
   StandardHttpContextMap as the listener and HTTP Status Code
   `${http.status.code}`. Add response header `Content-Type = application/json`
   and `Cache-Control = no-store`. Connect HandleHttpResponse relationships to
   your normal completion/error handling. Unexpected ExecuteScript `failure`
   should go through ReplaceText (replace whole content with
   `{"success":false,"error":"directory_unavailable"}`), then UpdateAttribute
   (`http.status.code=503`, `mime.type=application/json`) and the same response
   handler. Do not retry requests after their HTTP context expires.
7. Protect the input queues and provenance. Authorization is initially an HTTP
   request attribute; the script removes it before transfer, but earlier
   provenance events can retain it. Never route this flow through LogAttribute.
   Restrict NiFi UI/provenance permissions, use short request-queue expiry and
   configure your platform's sensitive-attribute/provenance controls. Keep this
   internal endpoint reachable only by the backend and administrators.

The SELECT is fixed, DISTINCT, ordered, and trims the eight supplied columns.
Codes are read as strings. Timeout is 30 seconds, fetch size 256, maximum 5000
unique branches and 4 MB. An extra row detects truncation: oversized snapshots
fail instead of pretending to be complete. There is no paging/filtering/SQL
parameter. Null branch/zone codes or names, conflicting hierarchy, duplicate
branch codes and overlong codes cause a whole-request 503. Optional division or
area must have both code and name present, or both empty. Oracle VARCHAR2(500)
is storage capacity: actual access codes currently must fit the application's
32-character identifier format. Nothing is truncated or converted to numbers.
Duplicate **branch names** are valid; branch **codes** identify records.

## API contract

`GET /api/reference/branches` with `Authorization: Bearer <directory token>`.
Success response (codes below are examples, not seeded directory records):

```json
{
  "success": true,
  "complete": true,
  "version": 1,
  "source": "DW_GC_BRANCHLIST2",
  "row_count": 1,
  "rows": [{
    "DIVISIONCODE": "01", "DIVISIONNAME": "Example division",
    "ZONECODE": "007", "ZONENAME": "Example zone",
    "AREACODE": "0004", "AREANAME": "Example area",
    "BRANCHCODE": "00253", "BRANCHNAME": "Example branch"
  }]
}
```

401 means missing/invalid credential; 404 wrong path; 405 wrong method;
400 unsupported query parameters; 503 unavailable/invalid snapshot. Errors have
`success:false`, a generic error code and no partial `rows` or SQL details.
Listener-level path/method rejection may use the listener's standard response.

## Django deployment configuration

Set the following in the backend deployment's environment/Secret using your
existing deployment procedure, keeping the credential out of Git and browser UI:

```text
LOAN_DIRECTORY_URL=https://YOUR-INTERNAL-API/api/reference/branches
LOAN_DIRECTORY_TOKEN=<same dedicated secret as NiFi>
# Optional for a private CA; mount the CA file into the backend container:
LOAN_DIRECTORY_CA_BUNDLE=/path/to/internal-ca.pem
```

If you deliberately use the existing private plaintext listener, the exact
configuration is:

```text
LOAN_DIRECTORY_URL=http://192.168.1.139:8001/api/reference/branches
LOAN_DIRECTORY_ALLOW_HTTP=1
LOAN_DIRECTORY_TOKEN=<same dedicated secret as NiFi>
```

HTTP opt-in accepts only literal RFC1918 IPv4 hosts. Prefer TLS for production.
The backend connects directly, does not follow redirects, does not use ambient
HTTP proxy settings for this internal credential, and verifies TLS. URL and token
are deployment-controlled; an admin cannot redirect the credential through a
submitted form. Apply Django migrations, including migration 0006, then restart
backend workers with the environment configured.

Open **Data access → Branch & zone directory** at
`/agentic/data-access/directory/` as an active staff administrator with
`agentic_ai.manage_loan_directory` (or a superuser).

1. Choose whether newly discovered entries should be active. The default is
   paused. Explicit activation can expand existing **All** and **selected zone**
   policies to include new branches. Existing paused records never reactivate.
2. Click **Fetch sync preview**. Review source counts, new branch count and the
   first 20 new/renamed/moved branches. Division, zone and area metadata also sync.
3. Enter a justification and choose **Apply reviewed snapshot** within 10 minutes.
   The backend fetches Oracle's snapshot again and checks it and the local catalog
   against the signed preview. A changed source/catalog requires a new preview.
   Any invalid row or failed audit rolls back the entire apply transaction.
4. Review the directory and audit events. Configure users separately under
   **Data access → User access**: selected branches, selected zones or All. No
   policies/groups/grants are created by a directory sync.

Sync upserts all rows by exact codes, updates names and hierarchy, preserves
existing active/paused flags, and **never deletes or pauses missing entries**.
The table contains no active flag; absence may mean a temporary source omission.
Pause retired branches explicitly after review. Division and area are metadata,
not additional permission tiers. Existing report authorization still requires
the separate signed-scope backend contract described in `deploy/LOAN_DATA_ACCESS.md`.

## Verification

Python tests cover preview/apply permissions, stale snapshots, exact codes,
atomic audits and preservation of grants/pauses. `test-script.groovy` runs the
actual NiFi script with mock JDBC/session and the real NiFi API jar; it is not a
live Oracle/NiFi integration test. Run it with Groovy 4, groovy-json and nifi-api
2.4.0 jars on the classpath and a disposable test environment credential:

```bash
LOAN_DIRECTORY_TOKEN=directory-test-token-at-least-32-bytes \
  java -cp '/path/to/test-jars/*' groovy.ui.GroovyMain \
  deploy/nifi/branch-directory/test-script.groovy \
  deploy/nifi/branch-directory/branch-directory.groovy
```

Before enabling production sync, validate GET success/counts against a DBA's
`SELECT COUNT(DISTINCT BRANCHCODE) FROM DW_GC_BRANCHLIST2`, unauthorized rejection,
and a preview with the activation checkbox left off. A DBA should investigate
null/conflicting codes rather than filtering them away to make the API succeed.
