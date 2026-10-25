# Zone overdue member ranking

Application support is implemented; it is NOT enabled until the data warehouse,
NiFi and MCP contracts below are installed and verified. No existing branch tool
is widened and no permissions are granted automatically.

## Supported request

“Show the top 10 overdue members in zone code 07 as of 2026-09-30.”
Or use an exact zone name. One zone, one explicit date, top 1–100 (default 10).
This ranks branch/member aggregates across the zone, not ten per branch and not
branch-level totals. A member appearing in two branches has a separate position
in each branch. Other ranking metrics are not part of this tool.

## Backend prerequisites

1. DBA confirms DUEAMOUNT represents overdue debt and all amounts use the same
   currency. Agree snapshot timing, null handling and unique loan identifiers.
2. Implement an authenticated zone lookup (code or unique exact name) and an
   authorized loan snapshot source. Resolve names before ranking, never guess.
3. Adapt `contracts/zone_overdue_ranking.sql` to the actual schema. The view is an
   integration contract, not an existing database object or executable migration.
   Aggregate all loans per branch/member before ranking/limiting. Verify freshness
   and completeness; never mark partial/stale extraction complete.
4. Expose a synchronous read-only NiFi POST endpoint. Its JSON input schema and
   description are `agentic_ai.zone_ranking.INPUT_SCHEMA` and `DESCRIPTION`.
   Export with `python -c 'import json; from agentic_ai.zone_ranking import INPUT_SCHEMA; print(json.dumps(INPUT_SCHEMA))'`.
5. Register it in ContextForge as exactly `zone-overdue-ranking`, then register
   that exact name in Django Agent Tools with explicit allowed roles. Suggested
   category: Audit planning; display name: Zone overdue priorities. Use the sample
   above as example_question. Existing workspace cards and result tables render it.
6. Do NOT register/enable it if NiFi only has shared service credentials and no
   trusted employee authorization scope. Current MCP connection uses a shared
   service token; it does not itself convey employee branch entitlements. Build
   trusted principal propagation or an equivalent scoped server boundary first.
   `authorizationApplied: true` is a backend assertion, not proof of an ACL.

## Required successful response envelope

Return as MCP structuredContent (or one JSON text item):

```json
{
  "status": "ok", "zoneCode": "07", "zoneName": "Example Zone",
  "reportDate": "2026-09-30", "limit": 10,
  "ranking": "member_due_desc", "aggregation": "branch_member",
  "scopeComplete": true, "authorizationApplied": true,
  "eligibleMemberCount": 1,
  "rows": [{"RANK": 1, "ZONECODE": "07", "ZONENAME": "Example Zone",
    "BRANCHCODE": "0007", "BRANCHNAME": "Example Branch", "MEMBERID": "000042",
    "DUEAMOUNT": "125.50", "OUTSTANDINGP": "800.00", "DUE_DAYS": 35}]
}
```

Zero eligible members => count 0 and rows []. Ambiguous zone name =>
`{"status":"ambiguous_zone"}`; UI asks for the unique code. Do not return an
unranked/raw loan report. Missing fields, duplicates, wrong scope/date/limit,
incorrect ordering, partial results and floating-point money fail closed.
Names/phone numbers and arbitrary extra fields are stripped. Model receives no
report values; output explains the deterministic ranking without misconduct claims.

## Release / acceptance

The source-integrity gate now recognizes the exact published usage gateway from
commit 3137b0f in addition to the earlier reviewed member gateway and known legacy
baseline. Unknown edits remain rejected. A fixture verifies the published source
and rejection of modified copies. The explicit-field completeness fix is included in this release and tested. Never remove the gate or
overwrite live code.

On premises, test a synthetic zone: two loans for one member, ties, duplicate
zone names, zero rows, leading zeros, >100 request, unauthorized branches and
slow/failed NiFi. Reconcile ranked totals and count against independent Oracle
queries, then test browser → MCP → NiFi → Oracle → UI with an authorized employee.
Do not label the feature live until these checks pass. No schema-specific Oracle
procedure or production NiFi flow was changed from this cloud workspace.

## On-premises application rollout

After the source update is published, run from your existing checkout:

```bash
cd /home/datawarehouse/rag/cloud-onprem
git status --short
git pull --ff-only origin main
bash deploy-agentic-updated.sh --check-source
bash deploy-agentic-updated.sh
```

Stop if `git status` shows local changes; preserve them before pulling. The
launcher retains the successful existing build/push/import/backup/migrate/rollout
flow, production environment and single-node SQLite constraints. App deployment
alone does not activate this tool. Keep its Agent Tools entry inactive until the
backend and authorization acceptance checks above pass.
