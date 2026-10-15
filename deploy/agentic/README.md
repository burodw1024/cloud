# Agentic AI and Policy UI release

This release includes the main chat's **Agentic AI** mode, model-driven MCP
execution through ContextForge, role checks, approval review, and standalone
Policy AI chat/studio layouts. It requires no new Python dependencies or database
schema changes. Existing `agentic_ai` migrations must already be applied.

## Deployment from the master

Fetch branch `release/buro-ui-20261015`, then from the repository root:

```bash
bash deploy/agentic/deploy.sh
```

The script builds from the Deployment's current image and copies only the files
listed in `files.txt`. It pushes a unique `cloud:agentic-...` tag to
`192.168.1.224:80` and rolls out `test/cloud-live-test` / `django` with
`IfNotPresent`. It retains the current scheduling constraints. Existing registry
reachability and HTTP trust on the assigned node remain prerequisites. If that
node cannot pull, rollout fails and the script restores the prior image.

If the desired old image is no longer pullable, explicitly supply an available
base matching the running application, for example:

```bash
BASE_IMAGE=192.168.1.224:80/cloud:model-20261014t104535z bash deploy/agentic/deploy.sh
```

Review `release-images.txt` after release and update any owning GitOps/Helm source.
If image import is used instead, import this newly built image on the scheduled
node and retain `IfNotPresent`; a cached earlier UI image does not contain this
backend release.

## Connect ContextForge

Gateway UI/base address: `http://192.168.1.99:30444`.

1. In ContextForge, select the Virtual Server exposing your tools. Copy its
   **Streamable HTTP** connection URL (commonly
   `http://192.168.1.99:30444/servers/<server-id>/mcp`). The gateway homepage is not
   itself proof of an MCP endpoint. Legacy SSE `/sse` transport is not supported.
2. Configure `MCP_SERVER_URL` using the Django URL Settings dashboard or a
   Deployment environment variable. Never include a token in the URL.
3. Set `AGENTIC_MODEL` to an installed Ollama model that supports native tools.
   If blank, the existing `OLLAMA_MODEL` is used, which must support tools.
   Ollama URL continues to use the application's dynamic `OLLAMA_URL` setting.
4. If ContextForge requires authentication, provide `MCP_BEARER_TOKEN` through a
   Kubernetes Secret and container `env.valueFrom.secretKeyRef`. Do not commit or
   paste the token into chat. The Django backend sends it as a Bearer header.
5. Discover and register tools inside the running Django container:

   ```bash
   python manage.py mcp_tools --register
   ```

   From a management terminal with working kubelet access:

   ```bash
   kubectl exec -n test deploy/cloud-live-test -c django -- python manage.py mcp_tools --register
   ```

6. In Django admin → Agent Tools, review each exact discovered tool name, set
   `allowed_roles` to the relevant Django group names (JSON list), then enable it.
   New tools are **disabled**, have no allowed roles, and require approval.
   Never enable blanket access to the gateway catalog. For reviewed read-only
   searches you may uncheck `requires_approval`; retain it for writes, sends,
   deletes, and other consequential actions. Remote MCP annotations never grant
   permission. Superusers can use registered tools with a nonempty role policy;
   ordinary users need a matching group. The explicit `authenticated` role can be
   used for tools intended for all signed-in users without groups.
7. Sign in to the chat, select **Agentic AI**, and submit a question. The model
   sees only enabled tools it may use. Successful tool results are fed back to the
   model for another call or a final answer. Tool-call and time limits prevent an
   unbounded loop. Generic unsupported questions may receive a direct answer or
   clarification without tools. This is not the old keyword router.
8. Pending actions appear in **Agent Operations** (`/agentic/`). Expand **Review
   action**, inspect the saved arguments, then approve or reject. Review requires
   staff status and `agentic_ai.change_agentapprovalrequest`. Approval executes
   exactly the saved tool/arguments and resumes the model; each subsequent action
   requiring approval creates a separate request. The requester uses **Check
   approval** in chat to retrieve the answer. An ordinary admin status edit does
   not execute an action.

## Behaviour and limits

- Login and CSRF protection are required for agentic queries and review actions.
- Tool authorization is checked at discovery and again before execution. Unknown
  or newly discovered tools are never automatically permitted.
- Follow-up history comes from the Django session, not client-provided messages.
  **Clear Chat** resets agent history on the next agent request.
- Tool arguments must be JSON objects. MCP servers validate their advertised
  JSON schemas. Gateway identity and upstream permissions remain enforced by
  ContextForge; Django additionally applies the local tool role policy.
- Approval continuation rediscovers tools and refuses a changed endpoint, schema,
  permission, or pending call. Only Streamable HTTP JSON/SSE response bodies are
  supported, including MCP initialization and session headers.
- Stopping a browser response does not cancel a tool already running. After a
  timeout or interrupted approved action, check the upstream result before retrying.
  Approval execution is claimed once in the database. A process crash after that
  claim requires manual investigation, never an automatic replay.
- Each request has a bounded tool loop. Allow at least 180 seconds in Gunicorn and
  ingress timeouts if your environment uses slow models; approval continuations
  have their own bounded request. No background queue is introduced.
- File uploads remain with the existing document/workflow modules; Agentic AI
  explicitly reports that uploaded-file integration is unavailable.
- Existing Outlook/Teams placeholder wrappers are not made functional by this
  release. They must be real upstream tools registered in ContextForge.
- `/policy-ai/` and `/policy-ai/studio/` now have their own document shell and
  responsive styles, instead of nesting into Django admin. Their view permission
  decorators remain in place.

## Validation

```bash
DJANGO_SETTINGS_MODULE=agentic_ai.tests.settings python -m django test agentic_ai.tests
```

Tests isolate external services and use Django's test database. A live gateway
and model check is still required after configuring the actual virtual-server
connection URL, credentials, and tool registrations.
