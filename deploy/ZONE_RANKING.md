# Zone ranking through the existing loan report

Use the existing `loan-due-report` MCP registration and the existing NiFi URL.
A second MCP tool, tool ID, endpoint or Django permission record is not required.
The code also understands the previously published separate tool for backward
compatibility; it prefers the extended existing report when both are present.

Application support is implemented. Production NiFi/MCP configuration is NOT
changed from this workspace. The ranking operation is advertised only when the
backend input schema declares it. Existing branch/date reports and exact member
inquiries continue using their current execution and permission checks.

## Extend the existing input contract

Replace the existing tool's input schema with
`contracts/loan-due-report.input.schema.json` after implementing its NiFi handler.
This is also `agentic_ai.loan_report_contract.INPUT_SCHEMA`. Keep the same tool
name, URL, headers, authentication and allowed roles. Add the operation description
from `agentic_ai.loan_report_contract.DESCRIPTION` to the existing description.

Ordinary requests remain valid without `operation`:

```json
{"branchCode":"0007","reportDate":"2026-09-30"}
```

The new operation uses the same tool:

```json
{"operation":"rank_overdue_members","zoneName":"Example Zone","reportDate":"2026-09-30","limit":10}
```

Use `zoneCode` instead of `zoneName` if the employee supplies an exact code.
Resolve a name uniquely to its actual code; ambiguous names must return
`{"status":"ambiguous_zone"}`. Preserve leading zeros. Require an explicit ISO
date and numeric top count 1–100 (default 10). Do not combine this operation with
branch/member filters. It ranks highest overdue AMOUNTS, not days, principal,
branches or zones, and not ten members per branch.

## NiFi behavior

Route missing `operation` or `operation=report` to the existing report flow.
For `operation=rank_overdue_members`, choose either response path below. Changing
only the MCP schema is insufficient: the NiFi handler must implement the contract.
The procedure signature and physical table names are not assumed here.

Before returning either response:

- Enforce the authenticated employee's authorized branch scope on the server.
  The shared MCP service token alone does not transmit employee entitlements.
  Do not enable broader reporting until trusted principal propagation or an
  equivalent approved scoped service boundary exists. No new access is granted
  by this update; existing tool activation, roles, approvals and audit remain.
- Confirm DUEAMOUNT means overdue debt, the snapshot date is correct, and the zone
  lookup is unique. Never infer these properties from the presence of columns.
- Read the complete authorized zone dataset. Do not mark a partial, truncated,
  paginated or timed-out extraction complete. Flags below are assertions made by
  the trusted backend, not proof of a database ACL or completeness by themselves.
- Serialize money as exact decimal strings or integers, never float values. The
  app accepts nonnegative values with up to 30 integer digits and 8 decimal places;
  invalid or out-of-range totals fail closed rather than round silently.

### Path A: rank in Oracle

Preferred for large datasets. Adapt `contracts/zone_overdue_ranking.sql` to the
actual schema. This is an integration template, not an executable migration.
The declared view must first be implemented against the real warehouse.
Aggregate all loans per `(BRANCHCODE, MEMBERID)` BEFORE sorting/limiting. Include
principal from all those loans, maximum reported overdue days, and only members
whose combined overdue amount is positive. Ties sort by branch code then member
ID as strings (binary order).

Return as MCP structuredContent or one JSON text result:

```json
{
  "status":"ok", "zoneCode":"07", "zoneName":"Example Zone",
  "reportDate":"2026-09-30", "limit":10,
  "ranking":"member_due_desc", "aggregation":"branch_member",
  "scopeComplete":true, "authorizationApplied":true,
  "eligibleMemberCount":1,
  "rows":[{"RANK":1,"ZONECODE":"07","ZONENAME":"Example Zone",
    "BRANCHCODE":"0007","BRANCHNAME":"Example Branch","MEMBERID":"000042",
    "DUEAMOUNT":"100.30","OUTSTANDINGP":"250.50","DUE_DAYS":72}]
}
```

Return exactly `min(limit, eligibleMemberCount)` rows; zero members means count
0 and rows []. Django verifies ordering, ranks, duplicate members, scope, count,
amount types and fields, and strips all unrelated/private fields before display.

### Path B: return complete existing loan rows and rank in Django

Useful when the existing procedure already returns the full loan-level dataset.
The existing report columns can be reused; no new financial column is necessary.
Do NOT apply `limit` to these source rows. Attach the metadata below only after
validating completeness, authorization, zone resolution, currency and grain.

```json
{
  "status":"ok", "resultType":"complete_loan_report",
  "zoneCode":"07", "zoneName":"Example Zone", "reportDate":"2026-09-30",
  "limit":10, "scopeComplete":true, "authorizationApplied":true,
  "currency":"BDT", "rowGrain":"loan", "sourceRowCount":2,
  "loanKeyColumns":["BRANCHCODE","MEMBERID","LOANCYCLE"],
  "rows":[
    {"ZONENAME":"Example Zone","BRANCHCODE":"0007","BRANCHNAME":"Example Branch",
     "MEMBERID":"000042","LOANCYCLE":1,"DUEAMOUNT":"60.10","OUTSTANDINGP":"100.00","DUE_DAYS":20},
    {"ZONENAME":"Example Zone","BRANCHCODE":"0007","BRANCHNAME":"Example Branch",
     "MEMBERID":"000042","LOANCYCLE":2,"DUEAMOUNT":"40.20","OUTSTANDINGP":"150.50","DUE_DAYS":72}
  ]
}
```

The example loan key is illustrative. The DBA must identify a genuinely unique,
stable loan key for the actual report; branch/member/cycle is NOT assumed unique
for your warehouse. A real loan ID or a verified combination of existing columns
can be declared. Duplicate keys, inconsistent key types, conflicting branch
names, nulls, floats, mixed currencies, wrong zones/dates and row-count mismatch
are rejected. Do not merely set completeness flags to make a response pass.

Django uses Decimal arithmetic over ALL rows, combines loans per branch/member,
filters positive combined dues, sorts, then selects top N. A member in multiple
branches has one aggregate per branch, as stated in the result. This is a report
ranking, not a complete branch member directory. Large reports that exceed the
existing MCP response limit must use Path A or a future bounded paging contract.
The existing 100-row UI preview is never used as a ranking source.

## Presentation and routing

Explicit zone ranking questions select only tools that actually declare the
ranking operation (or an already authorized legacy ranking tool). No new inputs
are invented for the old branch-only schema. If unsupported, the UI explains
that the existing report operation needs enabling; it never displays 100 ordinary
rows as a supposed top 10. Explicit ranking against a projection-only tool is
blocked before dispatch, and the ordinary presentation layer rejects that
fallback as a second check. Recognition of explicit ranking language does not
claim perfect intent understanding across every phrasing or language.

The normal report table displays the ranked fields and exact values. An optional
model introduction receives field names and record counts ONLY, never IDs or
financial values. It cannot calculate/reorder financial results. Rankings show
review priorities, not findings of misconduct.

## Release and production acceptance

No production NiFi flow, MCP registration, Oracle procedure, data or credentials
were changed from cloud. App deployment alone does not enable the new operation.
The source integrity gate continues recognizing exact previously published
sources and rejecting unknown edits. Authentication, CSRF, atomic SQLite cache,
permissions, approvals, model selection, audit and the deployment flow remain.

Before activation, reconcile a synthetic zone against independent Oracle totals:
multiple loans/member, ties, duplicate zone names, zero rows, leading zeros,
>100 limits, unauthorized branches, duplicate loans, mixed currency, backend
failures and records whose rank lies beyond the first 100 source rows. Then test
browser -> existing MCP tool -> existing NiFi endpoint -> Oracle -> result table
with an authorized employee. Do not label the feature live until those checks
pass. This update does not fix Oracle/JDBC cancellation or NiFi HTTP timeouts.
