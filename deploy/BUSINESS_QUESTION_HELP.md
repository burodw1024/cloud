# Savings and business question examples

The workspace now shows separate loan and savings question-help buttons when
those authorized capabilities exist in the selected module. A general business
help button uses the selected module name, or all permitted business capabilities
from the main workspace. Buttons prepare editable input; they never submit a
question automatically. Returned examples are also click-to-edit.

Savings question requests are handled before loan guidance and model selection.
Examples are generated only for the operations declared in the current authorized
savings tool schema: period-summary, branch-summary, zone-summary, zone-branches,
branch-product-summary and zone-product-summary. Single-branch and zone examples
require the corresponding schema fields. No balances, maturity reports, forecasts,
sorting or top-N inputs are invented. Date examples cover the previous complete
calendar month in Asia/Dhaka with an exclusive end date. Branch/zone identifiers
use declared enum examples when present, otherwise explicit editable placeholders.
Loan permissions are not reused as savings entitlements.

Published module starter questions are included under that module's name for
permitted users. Generic guides also retain supported loan examples; explicit
savings requests never fall back to loan guidance. An absent capability produces
an explanatory empty guide. Fresh discovery and account/group/module checks are
required; cached guides are not replayed after configuration changes. No report
tool or inference call is made to produce the guide. Tool execution permissions
and backend branch restrictions still apply when users submit a real report.

## On-prem verification

1. Keep savings-report enabled for the intended user group in /agentic/setup/.
2. Publish the Savings reports module with savings-report assigned. Add optional
   business starter questions under /agentic/modules/.
3. Open the Savings module and click “সঞ্চয় নিয়ে কী কী প্রশ্ন করতে পারি?”.
4. Submit the prepared help request; expect savings examples, not loan examples.
5. Click an example, replace bracketed identifiers and adjust dates, then submit
   the actual business report question separately.
6. Verify a user without tool permission and a paused module cannot see examples.

The release recognizes the exact previously published fixed-scope MCP source in
its upgrade compatibility check; unreviewed live changes remain blocked. Cloud
checks use synthetic tools and do not verify live NiFi/Oracle reporting.

## Model argument validation

Normal report requests still use the configured project model and strict tool
schemas. Model selection now includes an Asia/Dhaka calendar reference, including
the previous complete month's inclusive start and exclusive end. The reference
is frozen across a repair attempt. It also describes declared savings operations
and instructs the model to omit unspecified optional identifiers instead of
sending blank/null values. No branch, zone or authorization values are invented.

If a completed selection violates its schema, selection alone is retried once
with schema-defined field names and validation rule feedback. Provider/network
errors, malformed responses and tool execution are not retried. Report dispatch
only occurs after successful validation and the existing permission/approval
checks. Both inference attempts use the same usage meter and overall request
deadline. If repair also fails, the UI reports the field and rule; Django logs
`MCP argument validation failed` with schema-only details, never argument values,
unknown extra key names, report data or tokens.

This addresses invalid selection recovery and diagnostics. The on-prem failing
model response was not captured in cloud, so live report validation remains
required. A successful discovery-only single-tool probe is not proof that the
app's mixed-tool selection or NiFi execution has succeeded.

## Keep savings reporting separate from message delivery

For an explicit savings transaction report request (deposits, withdrawals, net
movement or the named savings report), the model now receives only tools from
its already-authorized catalog whose declared savings report schema includes
operation, from_date and to_date_exclusive plus a supported report operation.
The same contract predicate is used by the question guide. Loan tools, generic
DB tools and WhatsApp delivery tools are not candidates for that savings request.
Descriptions mentioning savings alone do not qualify a messaging tool.

If no authorized savings report contract is available, return an explanatory
clarification without inference or dispatch. Do not fall back to a messaging tool
or grant access to another tool. Tool aliases are rebuilt from the narrowed
catalog, and responses naming removed aliases are rejected. Savings balance columns in existing loan-member inquiries and mixed-domain
requests retain the existing catalog and validation behavior. This is relevance
filtering, not a substitute for roles, approvals or backend branch restrictions.

The separate WhatsApp preview/confirmation API and its permissions are unchanged.
After deployment, wait for `kubectl -n rag rollout status deployment/buro-django`
to complete before testing a new report. If logs still show messaging selection,
verify which pod/image produced them and that the request explicitly named savings.
