# Per-user loan data access

This release adds explicit data entitlements to the assistant loan module. Tool
roles still control which capabilities a user may run. The new policy controls
which data the loan backend may read. It does not modify permissions in unrelated
legacy chat routes, Oracle accounts, or other applications.

## Rollout boundary

The new policy is enforced, not an optional browser filter. Existing accounts,
including superusers, receive no automatic loan grant. Missing policies, missing
signing configuration, and backends without the signed-access contract cannot
execute protected loan requests. Legacy local NiFi financial registry operations
are blocked to prevent bypass through local execution mode.

The active native QueryLoanDue flow and its Oracle implementation are not in
this source checkout and were not changed from cloud. The bundled older
loan-due-report 1.1.0 archive contains a fixed-scope report processor; it does not
implement the current native query contract and is not replaced by this change. Application deployment alone does not complete the
backend integration. Do not activate the new financial flow until its signed
scope is applied to Oracle source rows before aggregation, sorting or top-N.
A response receipt acknowledges the backend's enforced scope; it does not prove
SQL correctness by itself. Reconcile financial results independently on-prem.

## Administrator workflow

1. Deploy the application with migration
   `0005_loanzone_agenttool_requires_loan_scope_and_more`.
2. Open `/agentic/data-access/directory/`. Import the authoritative branch/zone
   directory, or add records individually. Codes are strings; preserve zeros.
3. Open `/agentic/data-access/`. Choose a user and access mode:
   - **No access**: deny loan data.
   - **Selected scope**: union of explicit branches and active branches in chosen
     zones. Future branches added to a granted zone are included automatically.
   - **All access**: every active branch under an active zone, including future
     additions. This is an explicit privilege; no username/group implies it.
4. Optionally set expiry, expressed in Asia/Dhaka time. Supply a justification or
   approval reference for every change. Policies cannot overwrite a concurrent
   edit; reload when the version changed.
5. Give the user the existing loan tool role (for example `loan_users`) separately
   in Users/Groups and Tool setup. Ordinary users do not need Staff status.
6. Configure the signed backend contract described below, then test an authorized
   user and denied branch/zone queries before financial production acceptance.

The CSV template contains headers only:
`zone_code,zone_name,branch_code,branch_name,active`. Existing branch codes are
updated; new codes are added. Existing zone names must match; update a zone name
explicitly first. Active is 1 or 0. Import is atomic and audited, with a 2 MB /
5000-branch limit. Paused zones disable all their branches. There are no seeded
business codes, global grants, or inferred grants from old UserProfile values.
The app currently supports up to 5000 effective branches per user.

## Delegated administration

Staff status plus these Django permissions is required:

| Permission | Purpose |
| --- | --- |
| `agentic_ai.manage_loan_access` | Review and change other users' loan scopes |
| `agentic_ai.grant_all_loan_access` | Additionally allow the All access option |
| `agentic_ai.manage_loan_directory` | Maintain zones/branches and import CSV |
| `agentic_ai.view_loanaccessevent` | Browse full change history in Django Admin |

Delegated administrators cannot edit their own scope. A superuser can explicitly
bootstrap their own policy, with a justification and audit entry. Module
management, integration settings and auth user administration remain separate
permissions. Granting scope does not create users or grant tool roles.

Scope changes and directory changes are recorded in the same database transaction
as their audit event. Failure to save an event rolls back the change. Audit
records retain actor/subject usernames after account deletion, previous/new
values, reason and timestamp. Records are read-only through Django Admin. This
is an application audit trail, not cryptographic protection from a database
administrator. Cross-user run-log permissions separately expose stored report
answers and should be granted only to trusted reviewers.

## Signed service-to-service contract

Store a dedicated random signing key of at least 32 bytes in the deployment's
secret management, exposed to Django as `LOAN_ACCESS_SIGNING_KEY`. Configure the
same key in the trusted loan adapter's secret store. Do not reuse Django's
SECRET_KEY, paste keys into the UI, or commit them. Use authenticated TLS or a
protected mTLS service mesh between services, synchronize clocks, and redact
`loan_access_token` from gateway/NiFi request logs. Key rotation is coordinated
between issuer and verifier; active request tokens live for at most 120 seconds.

Update the actual backend before registering its new input schema:

- Native snake_case query: `contracts/loan-due-query.scoped.input.schema.json`.
- Legacy camelCase report/ranking: `contracts/loan-due-report.scoped.input.schema.json`.

The actual schema must declare required string `loan_access_token`. The field is
removed from model-visible schemas; users/models cannot supply it. Django adds
it only after authenticating the user, checking tool roles and resolving current
scope. Default loan tool names always require scope. Additional financial tools
can be marked **Enforce user branch/zone access** in Tool setup; this does not
implement a new backend or report contract. Registered loan report names must
also remain in `MCP_REPORT_PROJECTION_TOOLS` for the existing report presentation.

Django sends a short-lived HS256 JWT with fixed issuer `buro-insight-ai`, audience
`buro-loan-query`, subject (Django user ID), unique request ID, tool, actual backend
query, effective branch allowlist, expiry and policy/directory fingerprint. Branch
and zone filters narrow the allowlist; they cannot expand it. A zone request by
a single-branch user covers only that user's authorized branch within the zone.
All-access requests still receive a concrete directory-derived branch list;
there is no wildcard branch value or unlimited-access input from the model.

Use `agentic_ai/loan_scope_backend.py` in a compatible Python HTTP adapter, or
implement the same JWT contract in the NiFi/Java adapter. The module is standalone
and does not depend on Django. Validate before adding backend default parameters:

```python
from loan_scope_backend import verify_loan_access_token, authorization_receipt

claims = verify_loan_access_token(
    payload['loan_access_token'], signing_key, 'loan-due-report', payload)
parameters = {k: v for k, v in payload.items() if k != 'loan_access_token'}

# Implement this in the actual adapter/procedure. Its branch allowlist must be
# applied BEFORE sums, grouping, ordering, limiting or any financial data read.
result = run_authorized_query(parameters, allowed_branches=claims['branches'])
result['authorization'] = authorization_receipt(claims)
return result
```

`run_authorized_query` is an integration placeholder, not a supplied Oracle
implementation. Derive existing `auth.branch_codes` from the verified claims;
never from a static test constant, model parameter, unverified username, or
unsigned request header. Bind branch codes safely (for example a JSON array bind
expanded with Oracle JSON_TABLE and joined to the source query); do not interpolate
them into SQL. Every aggregation/ranking must read the constrained source set.
The Oracle service account must independently have appropriate database rights.
Token verification does not grant new Oracle privileges.

An outer successful result envelope must include `authorization` matching
`authorization_receipt(claims)` exactly. Keep the original report response
contract otherwise. Do not echo the token. Django checks the receipt, explicit
returned branch identifiers and backend branch count before presentation. Missing
or mismatched evidence returns no financial result. Returning an ordinary
unscoped result as a fallback is forbidden.

A long Oracle query may finish after token expiry if the backend authenticated
and authorized it when received. Django rechecks current account, roles, policy,
expiry and directory before releasing the result. A revocation withholds results
but does not cancel an already running Oracle operation. Previously issued tokens
are not an online revocation channel to the backend; they expire quickly.

## Cached/derived output and user experience

Idempotent report replay, Bangla summaries and WhatsApp sharing recheck the saved
scope fingerprint and current tool permissions. Revocation, expiry, pausing a
branch/zone, moving a branch or changing the policy invalidates affected access.
WhatsApp rechecks just before its tool dispatch, not only at preview time.
Existing cached protected reports without scope metadata cannot be replayed.

Users with a loan capability see their own scope in the workspace. Loan question
examples use a permitted branch/zone where available; examples are not grants.
No administrator infrastructure controls or signing keys are shown to users.

Application health checks can pass while the loan backend remains unconfigured.
The deployment launcher's final real-report test requires the directory, explicit
user grant, role and signed backend integration to be ready; do not remove these
checks to make that test pass. Existing deployment backups/Secrets and timeout
settings remain unchanged. No live NiFi/Oracle/Kubernetes changes were made here.
