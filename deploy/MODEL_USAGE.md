# Per-request model usage and estimated cost

The MCP question path records provider-reported token counts for every direct
model HTTP call: tool selection (including inference fallback), report-field
selection and optional report introduction. Ollama uses prompt_eval_count and
eval_count. OpenAI-compatible responses use prompt_tokens/completion_tokens
(or input_tokens/output_tokens), including cached_tokens when reported.

A request sums reported calls only. Missing counts, failed calls and timeouts
are explicitly incomplete; tokens are never guessed from question length.
A failed business request can still have model usage and an estimated charge.

An administrator with the existing guided-setup permissions can save model
rates under Set up tools. MCP_MODEL_PRICING stores a JSON mapping keyed by
provider:model. Prices are explicit strings per million tokens, with a
three-letter currency code, input_per_million, output_per_million, and optional
cached_input_per_million. The application does not ship a vendor price list,
fetch current vendor prices, or convert currencies. Administrators must keep
rates current and match the actual model/deployment identifier.

Cost = ((input - cached_input) * input_rate + cached_input * cached_input_rate
        + output * output_rate) / 1,000,000.

If cached_input_rate is not configured, ordinary input_rate is used. This is a
configured-rate estimate, not the provider's invoice. Mixed currencies are
reported per call without summing them. Missing prices remain unpriced rather
than zero. For local Ollama, administrators may configure a hosting-rate
estimate; an unpriced local model does not mean infrastructure is free.

The estimate excludes NiFi/Oracle, embeddings, tool-side models, server/GPU
operating costs unless explicitly represented by custom rates, taxes, batch
discounts, subscription charges and any usage the provider did not report.
A ChatGPT subscription is not a per-request API price and is not calculated.

Usage is stored in AgentRunLog.usage_summary with the rates used at that time.
Historical estimates are not recalculated when an administrator changes rates.
Per-call records contain model/provider names and counts, never model prompts,
response bodies, API keys or credentials. Recent activity displays the current
user's stored usage. The existing API idempotency cache returns the original
usage on replay and marks replayed=true; it does not execute or meter again.
No change was made to the separate local-orchestrator integrations; responses
without metering show usage unavailable rather than a fabricated cost.

Deployment includes an additive JSON-field migration. Existing audit records
receive an empty usage summary. Existing data, roles and approvals are retained.
