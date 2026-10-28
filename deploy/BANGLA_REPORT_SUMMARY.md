# Bangla analysis of displayed reports

A successful report now exposes **বাংলায় বিশ্লেষণ**. Clicking it summarizes the
server-held display projection without invoking MCP tools or Oracle again.
The original table remains visible if the model fails. This is generated
commentary, not a new financial calculation or a complete portfolio analysis.

## Setup after deploying the updated Django image

Open `/agentic/setup/` as an authorized setup administrator. Under **Bangla report
analysis**, set:

- ContextForge model API base URL: `http://192.168.1.99:30444/v1`
- Registered model ID: `openrouter/auto` (or another exact registered model ID)

Click **Save summary settings**. The existing server-side `MCP_GATEWAY_TOKEN` is
reused; its ContextForge identity must have `llm.invoke`. Do not enter an OpenRouter
key in Django: ContextForge holds the upstream provider credentials. A successful
browser-based ContextForge test alone does not verify the Django token's role.

ContextForge v1.0.10 exposes `POST /chat/completions` under `LLM_API_PREFIX`
(default `/v1`) when `LLMCHAT_ENABLED` is enabled. If the gateway uses another
prefix or reverse-proxy path, configure that base path instead. The summary base
must have the same origin as `MCP_GATEWAY_URL` to keep the gateway token on its
configured host. These are model API requests, not requests to `/servers/.../mcp`.
Settings can also be supplied as `MCP_SUMMARY_BASE_URL` and `MCP_SUMMARY_MODEL`;
active database settings take precedence over environment values.

## Behavior and validation

The browser sends an opaque report token, never report rows. Tokens are bound to
the authenticated user, expire after 30 minutes, and require current tool access
both before inference and before returning the summary. Requests require CSRF.
Concurrent requests are locked, successful summaries are cached, and new summary
requests are limited to five per user per minute. An audit entry records successful
summary generation. Original query usage stays unchanged; summary model and
provider-reported token totals appear separately under the Bangla analysis.

Only up to 100 already displayed rows and approved financial fields are sent.
Member names, member IDs, employee IDs, phone numbers and raw tool output are
excluded. The configured provider may be external (OpenRouter in this setup).
The model is instructed to preserve figures and dates, avoid calculations and
causal or historical claims, and describe scope limitations. Numeric claims not
present in the supplied data are rejected. This check does not prove that every
sentence is correct; the UI labels the text as AI analysis for verification.

Use a shared Django cache for multi-worker production (the existing SQLite cache
or Redis), rather than a process-local LocMemCache.

## Verify on-prem

1. Run a branch 0253 report as of 2026-09-30; wait for its table.
2. Click **বাংলায় বিশ্লেষণ**; confirm Bangla text and the original table remain.
3. Confirm NiFi receives no additional report request for the summary click.
4. Click again; confirm the cached-summary label and no additional model call.
5. Test with a user who lacks access to the report tool; the summary must be denied.

Cloud tests mock inference. Real on-prem gateway authentication and model responses
must be verified after deployment; the cloud workspace has no on-prem route.
