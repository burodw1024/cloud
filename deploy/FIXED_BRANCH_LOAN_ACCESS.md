# Fixed NiFi branch access

Use this explicitly selected mode for an existing NiFi QueryLoanDue endpoint
that enforces a fixed administrator-owned branch allowlist. It does not require
loan_access_token, a signing key, or a signed response receipt. Existing tools
remain in SIGNED mode after migration; there is no automatic fallback.

## Configure

1. Back up the application database and deploy this release with migration 0008.
2. In NiFi QueryLoanDue, set Authorized Branches JSON to the entire endpoint
   scope, for example `["0250"]`. Do not derive it from request input. Restrict
   the endpoint to the authenticated gateway/service and trusted administrators.
3. In ContextForge retain the actual loan report schema and endpoint. A backend
   that requires loan_access_token must continue using SIGNED mode.
4. In Django /agentic/setup/, Find tools and Check model. For loan-due-report,
   choose Fixed NiFi branch scope and enter `0250` in the full branch allowlist.
   Enable the tool, choose an existing dedicated group, and save.
5. In Django Admin, add the intended active users to that group.
6. In /agentic/data-access/directory/, ensure branch 0250 and its zone are active.
   In /agentic/data-access/, grant each user branch 0250 or a zone containing it.
   Existing expiry, denial and audit rules still apply.
7. Publish the Loan reports module with this tool. Savings remains separate.

All users of a fixed endpoint must be entitled to its **entire** configured
branch scope, even when their question names just one branch. A filter is not
an authorization mechanism. For users with narrower grants, configure separate
restricted endpoints/tools or retain signed per-user access. Do not configure
Django with a smaller allowlist than NiFi: Django cannot inspect the live NiFi
configuration or prove the scope of aggregated results without signed receipts.

The model cannot supply authorization attributes. Explicit out-of-scope branch
and zone requests are blocked. Returned explicit branch codes and scope counts
are checked. Tool roles, module pause, approvals, policy expiry and active-user
checks remain in force. Branch-policy and fixed-tool configuration changes
invalidate cached reports and in-flight responses. This mode is intended for
MCP tool execution; legacy local financial tools remain blocked.

Example question: “2026-09-30 তারিখ অনুযায়ী 0250 শাখায় ঋণভিত্তিক বকেয়া কম থেকে বেশি সাজিয়ে প্রথম ১০টি ফল দেখাও।”

Verify a group member, a nonmember, an expired/revoked grant, and an out-of-scope
branch. Complete an actual NiFi/Oracle report test before production acceptance.
Cloud tests use synthetic gateway responses, not the live financial database.

## Validation

325 isolated agentic regression tests passed, including 12 new fixed-scope and
setup tests. Migration drift checks and setup JavaScript syntax checks passed.
No live database, NiFi deployment, user grants, or gateway settings were changed.
