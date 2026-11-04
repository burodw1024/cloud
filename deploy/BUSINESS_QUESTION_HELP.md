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
