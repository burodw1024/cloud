# ContextForge v1.0.10: 600-second read-timeout ceiling

The production image ghcr.io/ibm/mcp-context-forge:v1.0.10 validates
httpx_read_timeout with le=600.0. Setting HTTPX_READ_TIMEOUT=1500 makes its settings
fail to load and causes CrashLoopBackOff. Restoring 300 recovers the gateway.
600 is the highest supported value for the unchanged upstream image; it is not
enough to wait for a 1200-second Oracle query through a synchronous REST tool.

The Django release now tests HTTPX_READ_TIMEOUT=1500 in a temporary process inside
the currently running gateway BEFORE changing its Deployment. A rejected setting
stops the release before gateway mutation or Django downtime. It cannot safely
raise this ceiling with an environment variable alone.

## Interim setting on the original image

```bash
kubectl --context mamun -n buro-agentic set env deployment/contextforge \
  --containers=gateway HTTPX_READ_TIMEOUT=600
kubectl --context mamun -n buro-agentic rollout status deployment/contextforge --timeout=300s
```

Keep 300 if you do not need the interim increase. Do not repeat 1500 against the
original image. A ten-minute ceiling still cannot accommodate a twenty-minute
query. Other tool/client deadlines may be shorter and must also be checked.

## Derived image for the 1500/1800-second release

The reviewed Dockerfile at deploy/contextforge-timeout/Dockerfile inherits the
same upstream v1.0.10 image and changes only the httpx_read_timeout upper bound
from 600 to 1800. It does not change authentication, credentials, roles, other
field limits, entrypoint or command. It restores the upstream non-root UID/GID
10001:10001. The patch refuses an unexpected source declaration.

The image build checks the effective settings wrapper with 1500. This check uses
random ephemeral test secrets only in process memory; no production secrets are
read and no generated credentials are persisted. Production continues to use its
existing Kubernetes environment/Secrets and encryption key. Do not rotate those
keys as part of this timeout change.

On your on-prem Docker build host, from the reviewed release checkout:

```bash
export GATEWAY_IMAGE=192.168.1.224:80/contextforge:v1.0.10-long-timeout-1

docker build --platform linux/amd64 \
  -f deploy/contextforge-timeout/Dockerfile \
  -t "$GATEWAY_IMAGE" .

docker push "$GATEWAY_IMAGE"
```

Before switching the Deployment, ensure Kubernetes can use that image on every
eligible gateway node. Use the existing registry pull configuration or the same
save/copy/containerd-import workflow used for application images. Determine the
actual gateway node first; do not assume it runs on worker-99:

```bash
kubectl --context mamun -n buro-agentic get pods -o wide
```

If importing manually, save the image, copy to the appropriate node, and import
into containerd namespace k8s.io. Verify the exact image reference there before
deployment. Preserve the existing storage and scheduling configuration.

After image availability is verified, update image and timeout in ONE strategic
patch, retaining other environment variables, Secret references and init containers:

```bash
kubectl --context mamun -n buro-agentic patch deployment contextforge \
  --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"gateway","image":"192.168.1.224:80/contextforge:v1.0.10-long-timeout-1","imagePullPolicy":"IfNotPresent","env":[{"name":"HTTPX_READ_TIMEOUT","value":"1500"}]}]}}}}'

kubectl --context mamun -n buro-agentic rollout status deployment/contextforge --timeout=300s

kubectl --context mamun -n buro-agentic exec deployment/contextforge -c gateway -- \
  python -c "from mcpgateway.config import settings; print(settings.httpx_read_timeout)"
```

Expected: 1500.0. If this rollout fails, inspect the new pod's startup logs and
restore the immediately previous known-good Deployment revision using kubectl
rollout undo. Do not remove existing production Secrets or data volumes.

Only after the compatible gateway is healthy, run deploy-agentic-updated.sh for
the matching Django 1500/1800 profile. The launcher re-checks the gateway before
mutating it. See LONG_REPORT_TIMEOUTS.md and ONPREM.md for application rollout.
Keep NiFi Query Timeout Seconds at 1200 and its HTTP Context Map expiration at
30 minutes (with allowance for queue time). Verify HandleHttpResponse delivery.

A custom timeout bound permits a longer request; it does not optimize Oracle
or guarantee live end-to-end completion. The original gateway image cannot be
made compatible by retrying the rejected environment value.
