# Native NiFi loan query — snake_case MCP contract

This release adds compatibility with the already working QueryLoanDue NAR and
SCOTT.GET_LOAN_DUE_QUERY procedure. The existing loan-due-report tool name stays
unchanged. Legacy branch reports and operation=rank_overdue_members remain
supported on their original camelCase contracts.

## Why ranking was blocked

The gateway advertised report_date, branch_code, level, sort_by, top_n and
loan-disbursement filters. Django previously recognized only operation-based
zone ranking, so it returned the generic zone-wide ranking refusal even for
branch-level member rankings. The new adapter activates only when the allowed,
discovered tool explicitly declares the native snake_case query schema.

## Gateway settings

Keep the existing server endpoint:
http://192.168.1.99:30444/servers/3af4c69246314595a30043f0b8d8b654/mcp

Tool: loan-due-report (preserve the existing identifier and Django role grants).
REST POST endpoint: http://192.168.1.139:8001/api/loan-due/query
Use deploy/contracts/loan-due-query.input.schema.json as its input schema.
Do not send operation, branchCode, reportDate, or authorized branch lists.
After changing the gateway, discover and save the tool in Django's assistant
setup page for the required groups. Existing role and approval checks are retained.

Native tool arguments for the reported failing question:

```json
{
  "report_date": "2026-09-30",
  "branch_code": "0253",
  "level": "MEMBER",
  "sort_by": "DUEAMOUNT",
  "sort_order": "DESC",
  "top_n": 10,
  "disbursed_from": "2025-01-01",
  "disbursed_to": "2025-12-31"
}
```

Question:
Rank members in branch 0253 by total overdue for loans disbursed between
January 1 and December 31, 2025, measured as of September 30, 2026.

The selected project model still chooses the tool/arguments. Dates and identifiers
are checked against the question. The shared year in a same-year range is
recognized. Unknown tools, undeclared schemas, mismatched dates/scope, role
denials and approval requirements are not bypassed.

## Verified display

Oracle performs aggregation and top-N before results reach Django. Django does
not calculate member totals from a limited branch report. It checks the response
success, date, level, sorting, result count, branch/zone scope, sequential ranks,
finite numeric values, grouped identities and metric ordering before rendering.
UI output is capped at 100 rows with explicit truncation, even when the backend
limit is higher. Personal names and phone fields are not included in this table.
Backend numeric text values are displayed without an LLM recalculating amounts.
Zone results are always described as limited to backend-authorized branches,
not as proof of complete zone coverage.

NiFi must derive auth.branch_codes from the authenticated account mapping in
production. A fixed ["0253"] setting is only a single-scope test configuration;
this Django compatibility change does not manufacture or broaden branch access.
The current backend response confirms requested scope and authorized branch
count, not the full identity list or independently proven whole-zone completeness.

## Release

Run the existing checked-in on-prem workflow from a clean GitHub checkout:

```bash
git pull --ff-only origin main
bash deploy-agentic-updated.sh --check-source
bash deploy-agentic-updated.sh
```

Set ENV_FILE to the absolute path of the existing protected production.env and
PUBLIC_CA_FILE to the existing trusted public TLS certificate if needed. Do not
replace either with generated credentials. See ONPREM.md for prerequisites.
The launcher builds/tests the reviewed commit, pushes images to 192.168.1.224:80,
copies/imports on dw@192.168.1.99, and deploys context mamun / namespace rag.
Existing database, data volumes, role permissions, Secrets and TLS are preserved.

For end-to-end verification, provide the member-ranking question above when the
launcher prompts for a report question. It verifies a real authenticated call,
tool dispatch, audit request ID and structured table, without automatically
retrying a potentially running report. Inspect any retained release lock before
retrying after migration dispatch.

Cloud tests use mocked model/MCP transport and synthetic report data. They do not
establish private-network reachability, real-model selection, live Oracle/NiFi
health or successful Kubernetes rollout. On-prem execution is still required.
