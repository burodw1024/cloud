# Loan question guide

In `/agentic/`, ask “ঋণ ও বকেয়া রিপোর্ট নিয়ে কী কী প্রশ্ন করতে পারি?” or
“What questions can I ask about loans?”. The loan-help shortcut prepares the
Bangla help request; press Ask assistant to retrieve the guide.

The MCP query flow builds Bangla examples from fresh, role-filtered gateway
discovery for the configured `MCP_REPORT_PROJECTION_TOOLS`. Legacy branch/date
reports offer report/member examples. Native snake_case reports offer only their
declared grouping levels, sorting metrics and filters. The legacy zone-ranking
operation is advertised only when its schema declares it. The guide does not
call a model, execute a financial tool or grant report access. Normal report
queries continue through existing selection, permissions and approval checks.

Click an example to copy it into the query box, change codes/dates/product names,
then press Ask assistant. Clicking never submits a query. Examples contain sample
identifiers, not customer data. Extra legacy report columns depend on the actual
returned report. Declared backend capabilities are not a live-health guarantee.

Guides are not shareable financial results and are not replayed from stale
idempotency caches; a repeated request key receives a refresh instruction.
Local execution mode is unchanged; this guide uses MCP discovery.

Validation:

```
python -m django test agentic_ai.tests.test_loan_question_help agentic_ai.tests.test_mcp_gateway agentic_ai.tests.test_api agentic_ai.tests.test_workspace --settings=agentic_ai.tests.settings
node --test agentic_ai/tests/js/loan_question_help.test.cjs
```
