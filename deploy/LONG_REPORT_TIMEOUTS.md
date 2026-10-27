# Twenty-minute NiFi queries through MCP and Django

This release is prepared for the existing QueryLoanDue processor configured with
Query Timeout Seconds = 1200. It does not change NiFi or contact the private cluster
from the cloud.

## Effective timeout profile

| Layer | Seconds |
| --- | ---: |
| NiFi JDBC query (set in NiFi) | 1200 |
| ContextForge HTTPX_READ_TIMEOUT | 1500 |
| Django MCP_TIMEOUT | 1500 |
| Django MCP_REQUEST_TIMEOUT | 1800 |
| Gunicorn timeout and graceful timeout | 1860 |
| Nginx proxy read/send timeout | 1900 |
| Pod termination grace | 1920 |
| Authenticated deployment verification HTTP client | 1920 |
| Duplicate-request pending/result cache | 2400 |

Set the shared NiFi StandardHttpContextMap Request Expiration to at least
30 minutes, with allowance for queue time. QueryLoanDue producing rows does not
prove HandleHttpResponse delivered them. Inspect both processors' provenance.
Any other ingress, client or load balancer must permit the report duration.
The browser form uses fetch without a shorter application abort timer.

## Why Kubernetes env changes alone failed

The old Django validator rejected values above 300/600. Its dynamic configuration
reads active database SystemSetting records before environment values. The old
release migration also wrote 300/600 back into those records. This release raises
the permitted bounds and updates both Kubernetes environment and database values
in the backed-up migration step, clearing the setting cache afterward.

The launcher updates and verifies ContextForge's effective HTTPX read timeout
before Django downtime. It renders and validates the existing Nginx ConfigMap and
Gunicorn command with the longer profile. Fresh production templates and recovery
checks use the same profile. Idempotency records now survive the whole request
window, so an identical request key cannot expire while its report is pending.
Credentials, selected model, tool role grants, approvals, TLS and persistent
storage are retained.

## On-prem launch

The reviewed source is on release/native-loan-query-20261027 in
https://github.com/burodw1024/cloud.git, including the native ranking fix.
Use a fresh checkout if your existing one has local changes:

```bash
git clone --branch release/native-loan-query-20261027 \
  https://github.com/burodw1024/cloud.git cloud-timeout-fix
cd cloud-timeout-fix
bash deploy-agentic-updated.sh --check-source
bash deploy-agentic-updated.sh
```

The launcher requires the existing readable absolute ENV_FILE and PUBLIC_CA_FILE
paths, admin kubectl context mamun, registry access, and dw@192.168.1.99 SSH access.
If those variables are not already configured, set them to the actual existing
files. Do not create replacement secrets or paste credentials into chat.

It builds/tests the reviewed source, pushes the images to 192.168.1.224:80,
copies/checksums/imports them into worker-99 containerd, backs up the existing
SQLite data, updates settings, and deploys namespace rag. It validates effective
settings and runs an authenticated report test. Follow ONPREM.md for retained
release locks; do not delete a lock or retry a migration blindly.

## Post-rollout verification

```bash
kubectl --context mamun -n rag exec deployment/buro-django -c django -- \
  python manage.py shell -c \
  "from agentic_ai.mcp_gateway import load_gateway_config; c=load_gateway_config(); print({'MCP_TIMEOUT':c['timeout'],'MCP_REQUEST_TIMEOUT':c['request_timeout']})"

kubectl --context mamun -n buro-agentic exec deployment/contextforge -- \
  python -c "from mcpgateway.config import settings; print('HTTPX_READ_TIMEOUT:',settings.httpx_read_timeout)"
```

Expected: 1500 / 1800 for Django; 1500 for ContextForge. Add the appropriate
container name to the second command if ContextForge has multiple containers.
The launcher identifies that container automatically.

Test one report after the previous execution has finished. Keep its request ID
and compare Django audit, gateway logs and NiFi provenance. Do not retry just
because the UI disconnected: Oracle may still be executing. The longer profile
permits more execution time; it does not optimize the Oracle query.

Unit and synthetic deployment tests are not a production rollout or a live
Oracle/MCP performance test. The on-prem launcher must complete successfully.
