# ContextForge agent

When AGENT_MCP_ENABLED=true, /agentic/api/query/ uses Ollama tool calling rather
than the legacy keyword planner. Staff can open /agentic/ to ask a question.
MCP access defaults to superusers/admins; other staff still need an allowed role.
The public website-only endpoint remains separate.

The agent initializes a Streamable HTTP MCP session, discovers tools, filters the
catalog to the configured tool allowlist, and sends their descriptions and JSON
schemas to Ollama. Ollama chooses tool names and arguments. Django validates each
call before sending it through ContextForge. Results go back to Ollama for the
final answer. Missing inputs can produce a clarification without executing a tool.
Each question is independent; this mode does not yet carry conversation history.
At most three tool calls run per question, with bounded request/session timeouts.
The existing dashboard records the run outcome and successful tool names.

## Configure the business tool first

The current ContextForge nifi-db-read has a placeholder URL and GET method.
Replace them with the working NiFi API's exact method, URL, parameter mapping,
and input schema; test it in ContextForge. Do not expose unrestricted SQL to the
model. Use approved operations and server-side branch/customer authorization.
The application has different request contracts in its legacy modules, so the
actual NiFi contract must be verified rather than assumed.

Only include vetted read-only tools in the allowlist. The adapter excludes tools
marked destructive, but labels and names cannot enforce read-only behavior:
NiFi and Oracle must enforce it. ContextForge must restrict its token to the
virtual server and required tools. This initial integration has no write/approval
execution path. The shared gateway token does not enforce individual Django
users' branch permissions; access defaults to admins for that reason.

## Kubernetes configuration

Install requirements.txt for local development or build the existing Dockerfile
using requirements.docker.txt. Both include the MCP client and OpenAI dependency.
Do not copy tokens into images, repository files, or command-line literals.

Create a Kubernetes Secret on your kubectl machine using a hidden prompt:

```bash
umask 077
read -r -s -p 'ContextForge API token: ' contextforge_token
printf '\n'
printf '%s' "$contextforge_token" > /tmp/contextforge-agent-token
unset contextforge_token
kubectl create secret generic contextforge-agent -n test \
  --from-file=token=/tmp/contextforge-agent-token
rm /tmp/contextforge-agent-token
```

Add these entries to the Django container's env list (not the ContextForge pod):

```yaml
- name: AGENT_MCP_ENABLED
  value: "true"
- name: AGENT_MCP_URL
  value: http://contextforge.buro-agentic.svc.cluster.local:4444/servers/e9b436c5af1844a78a3d710e4c25bac5/mcp
- name: AGENT_MCP_TOKEN
  valueFrom:
    secretKeyRef:
      name: contextforge-agent
      key: token
- name: AGENT_OLLAMA_URL
  value: http://ollama.rag.svc.cluster.local:11434
- name: AGENT_OLLAMA_MODEL
  value: qwen3.5:9b
- name: AGENT_MCP_ALLOWED_TOOLS
  value: nifi-db-read
- name: AGENT_MCP_ALLOWED_ROLES
  value: admin
```

Use the exact tool name discovered from ContextForge if it differs. Restarting
with new images can erase the live-test deployment's emptyDir database/users.
Use a new image tag per build when importing into containerd; imagePullPolicy
Never requires the exact image on the scheduled node. The repository Dockerfile creates writable /app/logs and defaults
to buro_first.wsgi, incorporating the startup fix diagnosed on Ubuntu. Set Gunicorn timeout at least 210 seconds if using Gunicorn.

Open /agentic/ as a superuser and ask a read-only question with known test inputs.
Verify the displayed tool, real returned data, and the run log. Also verify that
an unauthorized user cannot execute the MCP agent. Live tests require private
cluster connectivity and a real token; mocked/local transport tests do not prove
NiFi or Oracle business results. Set AGENT_MCP_ENABLED=false to use legacy routing.
HTTP cluster endpoints transmit tokens/data without TLS; use HTTPS or a secured
service mesh before use outside your trusted test network.

## Diagnose the current ContextForge timeout

The direct HTTP POST from ContextForge to NiFi was verified by the operator:
SELECT 1 AS MCP_TEST FROM DUAL returned [{"MCP_TEST":"1"}]. ContextForge's UI
/rpc tool test still times out, so end-to-end MCP execution is not verified.
Do not substitute a direct NiFi call and describe it as MCP success.

After deploying this code and binding the token Secret, check the actual virtual
MCP endpoint independently of the model:

```bash
kubectl exec -n test deployment/cloud-live-test -- python manage.py check_mcp
kubectl exec -n test deployment/cloud-live-test -- python manage.py check_mcp --test-read
```

The first command checks initialization and discovery. The second sends only
the fixed Oracle constant query, verifies the response marker, and has a bounded
timeout. Credentials and error bodies are not printed. A discovery failure
points to MCP access/authentication/transport; discovery success with a read-test
failure narrows the issue to gateway tool execution or the backend mapping.

Direct work from Codex requires private network access through environment VPN
settings, explicit destination grants, and securely configured authentication.
At the current checkpoint no VPN, private destination grants, or credentials
are configured in Codex. The token Secret exists in Kubernetes only. ContextForge,
NiFi and Kubernetes have not been edited directly from Codex.
