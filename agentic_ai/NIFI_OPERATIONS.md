# AI-selected Oracle/NiFi operations

## Architecture

The agent asks the configured on-prem Ollama model for a structured decision
using descriptions and input schemas from the executable Python tool registry.
There are no keyword routing rules or keyword fallbacks. One request selects one
operation; missing inputs, multiple operations, and unsupported requests should
produce a clarification. Conversation history/automatic identity resolution and
multi-step orchestration are not implemented by this change.

Only role-permitted tools are advertised. The server validates the selected tool,
argument names, types, ID format and verbatim occurrence in the original question.
It rechecks roles before execution, and approval requirements come from server
configuration, never model output. Invalid plans and model failures execute no
business tool and are audited. Model classification can still be wrong; mocked
unit tests validate enforcement, not real-model accuracy.

The generic `nifi_tool` is deliberately excluded from AI selection because it has
no approved report/aggregation argument contract. Add specific reporting tools
with backend-approved queries before supporting totals, highest-overdue branches,
or portfolio reports. Knowledge tools must not substitute for live financial data.

## Supported business contracts

| Tool | Required input | Oracle output columns |
|---|---|---|
| get_loan_balance | member_id | OUTSTANDINGP, Outstanding(P+I) |
| get_loan_due_details | member_id | DUEAMOUNT, DUE_DAYS, DUEINSTNO_U |
| get_loan_installment | member_id | INSTALLMENT_AMOUNT, PAYMENTFREQUENCY |
| get_savings_balance | member_id | GS_BALANCE, CS_BALANCE |

Example question: `For member M1, how much principal remains unpaid?`
Example validated arguments: `{"member_id": "M1"}`.
`My balance` must ask for the member ID; ownership is never guessed.

The NiFi POST contract remains `query`, `params`, `language`, `requested_by`.
`params.member_id` is the required filter for focused operations. The trusted
requesting username is supplied by Django, not by the model. Confirm that the
existing NiFi endpoint binds this parameter to the procedure's actual IN/INOUT
parameter before deployment. This repository does not contain that NiFi flow;
its parameter binding cannot be verified here.

NiFi may return a single row, a row list, or envelopes under `rows`, `data`,
`results`, or `result`. Each row MUST include `MEMBERID` matching the requested
member, plus the tool's required columns. Other members, missing columns,
unsuccessful envelopes, nonnumeric financial values, and more than 100 rows are
rejected. An empty row list is no records; null is unavailable, never zero.
Returned amounts preserve their original representation. No currency, date,
financial aggregate or total across multiple loans is inferred.

Projection happens before result/answer/audit output, so unrelated names and
phone numbers are removed. Financial answers use deterministic English/Bengali
labels and exact returned amounts; the model neither calculates nor rewrites
financial figures. RAG tools keep their existing answer generation.

## Configuration

Use the existing restricted settings dashboard at `/admin-tools/settings/`:

- `AGENT_PLANNER_MODEL`: Ollama model for planning; blank uses the configured
  default model (`OLLAMA_MODEL` / `GEN_MODEL`).
- `AGENT_PLANNER_TIMEOUT`: read timeout, 1–180 seconds; default 60.
- Existing `OLLAMA_URL` and `NIFI_CORE_URL` continue to apply.

These settings also support environment variables via the existing runtime
resolver. Use an Ollama version/model that supports JSON-schema structured output
on `/api/chat`. An incompatible model/server fails explicitly; it does not fall
back to keyword routing or an unrestricted SQL tool. The planner is Ollama-based
regardless of the separate RAG provider setting, keeping this selection on-prem.

Explicit tool schemas and approved column mappings are intentional API contracts.
Adding a business capability changes its contract, registry description and tests;
adding another wording of an existing question does not require code changes.
No database migration or new application dependency is required.

## Security and deployment boundaries

Member ID grounding and response-scope checks do NOT establish account ownership.
NiFi/Oracle must enforce the authenticated staff/customer's permitted scope using
trusted service authentication and authorization; `requested_by` alone is not an
authentication mechanism. Existing role checks remain in place. Secure service
transport, endpoint credentials and database authorization are deployment concerns
that this change does not provision. Financial outputs remain in existing audit
logs, subject to the deployment's access/retention policy.

This registry is internal Python registration, not an MCP server. A separately
hosted MCP server must register equivalent validated handlers; it is not changed
by editing this repository. Oracle procedure and NiFi flow definitions are not
modified, and Kubernetes deployment has not been performed.

## Validation before rollout

Run `python -m unittest discover -s agentic_ai/tests -v` with Django and requests
installed. Tests isolate model responses/HTTP and audit writes; no production
Oracle, NiFi or LLM call is performed.

Before rollout, test the configured model with English/Bengali paraphrases for
all four tools, missing IDs, unsupported reports, complaints and instruction
injection. Verify correct selection and clarification on real model outputs.
Then validate the NiFi input contract and masked real response, including a
cross-member request that must be denied by the backend. Unit tests alone do not
establish production model accuracy or backend authorization.
