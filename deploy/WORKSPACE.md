# Organization-wide workspace

The /agentic/ home page uses the registered capability catalog rather than
hard-coded loan examples. Gateway-mode entries are active AgentTool records
allowed by the current user's roles; the page makes no remote discovery or
business-tool execution requests. Local execution mode uses the same permitted
local catalog as the executor. Entries are labelled registered, not live or
healthy: actual availability is established when a request executes.

Administrators can configure display name, category and optional example
question in guided tool setup or Agent Tools administration. An empty display
name falls back to a readable tool name; existing records default to General.
The schema migration adds these metadata fields without changing activation,
allowed roles, approval requirements or existing descriptions. Existing users,
credentials and report data are not modified.

The browser directory searches titles, descriptions and categories. It displays
six cards per page and supports large role-filtered catalogs. Selecting a card
only prepares an editable question; it does not bypass model selection, choose a
backend directly, or dispatch an action. Do not put credentials, customer data
or unsupported claims in administrator-authored example questions.

Personal activity shows only the current user's five most recent query
summaries, never their stored result payloads. Pending approvals show only
that user's pending requests. These panels refresh when the page is loaded;
they are not live monitoring or approval-action interfaces.

The existing question API, idempotency, permissions, approval flow, tool-size
limits and single-tool-per-request execution remain unchanged. In particular,
the MCP executor's existing 64-authorized-tool discovery limit remains in
place. A UI catalog with 100+ records does not remove that execution limit or
provide multi-step planning. Larger execution catalogs require separately
reviewed routing/catalog-budget changes; the UI does not imply they exist.
