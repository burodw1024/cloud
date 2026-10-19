# Member-specific loan inquiries

The configured report projection tools (MCP_REPORT_PROJECTION_TOOLS, default
loan-due-report) can answer a member inquiry when their input schema declares
string branchCode and reportDate parameters. Existing roles, activation and
approval requirements still apply to the same tool. This does not add account
ownership restrictions or grant a user a new branch permission.

Example:

> Show loan products, disbursed amounts, maturity dates, outstanding principal
> and overdue amounts for member <MEMBER_ID> in branch <BRANCH_CODE> as of 2026-09-30.

The application advertises an optional typed memberId to its selection model.
The exact ID must occur in the user's question; branch and ISO report date are
required for member inquiries. IDs are strings and preserve leading zeros.

For the current NiFi schema, which only accepts branchCode and reportDate,
the application removes the local memberId parameter before the MCP call. It
filters returned Oracle rows by exact MEMBERID equality, verifies BRANCHCODE,
then projects only the requested approved columns. It never treats a numeric
or prefix ID match as a member match. Missing scope fields fail closed. No
matching rows means no match in this report, not zero balances or no loans.

This is application filtering of the authorized report, not SQL generation or
an Oracle procedure change. The full branch report must still complete and
fit the existing response-size limit. Existing backend timeouts are unaffected.
For larger workloads, extend the Oracle procedure and NiFi parameter binding
first, then declare memberId in the actual ContextForge tool schema. The app
will pass a natively declared memberId through and reject out-of-scope results.
Do not register a backend parameter until the backend really supports it.

A successful member-ID listing in the UI offers a member button when the
executed plan contains branch and report date. Selecting it prepares an
editable member inquiry; it does not automatically execute another request.

Optional model-written introductions receive report metadata only, never IDs,
financial values or original rows. Exact projected values are rendered directly.
No sums, averages, ranking or financial analysis are added by this upgrade.
