# Query performance dashboard

After deployment open `/agentic/performance/` or **Query performance** in the
assistant navigation. Access requires an active Staff account with
`agentic_ai.view_agentrunlog`, the same permission as cross-user run history.
Ordinary employees cannot open it. Responses are no-store.

## Find what to improve

1. Select the last hour, 24 hours or 7 days; optionally filter employee, exact
   tool name and status. Click Apply filters or Refresh measurements.
2. Review average, P50 and P95 MCP server execution time, failed/blocked attempts,
   clarifications and completed reports. P95 uses the nearest-rank percentile.
3. Start with the stage consuming the largest measured share. The dashboard
   suggests what to inspect, without claiming that timing proves a root cause.
4. Expand an individual query to inspect its ordered processing stages, total,
   model call count, request ID, outcome and backend dispatch/return state.
5. Compare similar questions and scopes; larger financial reports naturally take
   longer. Tool rankings show whole-request time, not just database execution.

Stages cover durable request audit, permission lookup, settings, connection and
MCP initialization, tool discovery, model selection (including selection retries),
access authorization, pre-dispatch audit, remote execution, response scope checks,
report presentation/summary and final access recheck. Other application work is
elapsed time outside these stages. Failed/time-limited stages close as interrupted
and are retained even when no report is returned. No query arguments, signing
tokens, endpoint URLs or exception bodies are added to the timing trace.

The backend stage includes ContextForge transport, NiFi queues/processors and
Oracle together. Separating them requires trusted backend telemetry, which this
release does not fabricate. Browser transfer/render time, HTTP worker queueing,
final log write, separately requested Bangla summary and WhatsApp calls are not
included. HTTP idempotency replays reuse the original log, rather than presenting
its old duration as a newly executed query. Legacy local paths are unmeasured.

Metrics aggregate at most the newest 1000 filtered attempts, with the sample and
unmeasured counts displayed. Attempts are independently paginated, 25 per page.
Only current-version traces enter latency statistics. Old logs, in-progress
requests and uninstrumented paths show Not measured. Refresh is manual; dashboard
views never call models, MCP tools, NiFi or Oracle or retry financial queries.

## Deploy

The additive `0010_agentrunlog_performance_summary` migration adds an empty JSON
field to existing run logs; no historical timings are invented. The existing
on-prem launcher runs migrations, builds/copies/imports images to the worker and
deploys Kubernetes as usual. It recognizes the exact previously merged gateway
source for its compatibility check; no compatibility verification is bypassed.

From the existing release folder containing source:

```bash
git -C source pull --ff-only origin main
unset ENV_FILE
export PUBLIC_URL=https://192.168.1.99:31135
bash source/deploy-agentic-updated.sh --check-source
bash source/deploy-agentic-updated.sh
```

Then run new queries in the workspace and refresh the dashboard. Existing
permissions, signed/application-managed tool modes and NiFi configuration remain
unchanged. Timings are observational and do not change report execution or limits.
