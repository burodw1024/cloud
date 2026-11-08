# Signed user branch access for NiFi 2.4 — cloud workspace implementation

This package connects Django's per-user loan authorization to an existing
QueryLoanDue processor configured with `Authorized Branches JSON = ${auth.branch_codes}`.
An `ALL` policy is expanded by Django into concrete active branch IDs. A request
for branch `0003` narrows that list to `0003`; NiFi never needs a manually maintained
per-user branch list. The signed token is issued by Django, not the model.

## What is implemented and what still needs verification

Implemented locally: Groovy HS256 token validation, query/identity/scope/time
binding, verified branch attributes, credential-free downstream JSON, receipt
attachment, out-of-scope row rejection, HTTP failure routing, a disabled flow draft
builder, schema preparation, admin diagnostics and a Bash-friendly management
command. JVM tests use actual Python/PyJWT-issued tokens and NiFi callback stubs.

The supplied flow export contains configuration, not custom QueryLoanDue SQL
implementation. The scripts **rely on QueryLoanDue enforcing Authorized Branches
JSON before aggregation**. A receipt attests passage through the verifier/response
path; it cannot prove an arbitrary custom processor's SQL is correct. Verify that
behavior in staging or inspect the NAR/source before activating the new flow.
Neither the live NiFi service nor Oracle was reached from the cloud workspace.

The exported savings processor has literal `allowed-branches=0250`. Its support
for a per-request attribute and its SQL enforcement are unknown. This tool does
not rewrite that processor or claim savings migration is complete. Keep savings
out of the signed rollout until its implementation is checked. The common token
verifier can be reused with expectedTool=savings-report once a compliant savings
processor/adapter exists.

## Prepare a review draft in the cloud workspace

```sh
python deploy/nifi/signed-loan/build_scripts.py
python deploy/nifi/signed-loan/prepare_flow.py web-service.json work/web-service-signed-loan-draft.json
```

The original export is never overwritten. Only the connected QueryLoanDue 1.1.0
loan path is changed; the unconnected 1.0.0 processor, other flows and controller
services are preserved. The changed/new loan processors are DISABLED in the draft.
The draft retains existing controller-service references but does not contain a
signing key. It is a review artifact, not an instruction to replace the live group.
Do not import a full copy of a group with active HTTP listeners alongside the live
group: ports and services may conflict. Apply the reviewed loan-path changes in a
staging group with separate ports first. Keep the original export for rollback.

Target path:

    HandleHttpRequest -> Verify user loan access -> QueryLoanDue
        -> Attach verified loan receipt -> HandleHttpResponse

Verifier failure -> HandleHttpResponse
QueryLoanDue failure -> Return loan backend error -> HandleHttpResponse
Receipt validation failure -> HandleHttpResponse

All response paths preserve `http.context.identifier`. HandleHttpResponse uses
`${http.status.code}`, rather than unconditional 200. Custom SQL failures return
a generic 503 response, not SQL details. Invalid/missing authorization is rejected
before QueryLoanDue. No automatic retries are added.

## One-time NiFi setup

1. Make the same dedicated signing key (at least 32 UTF-8 bytes) available as
   `LOAN_ACCESS_SIGNING_KEY` in Django and the NiFi service environment through
   their secret management. Never put it in Script Body, flow JSON, source code,
   command-line arguments or a chat. Restart processes as appropriate to load the
   environment. Do not reuse Django SECRET_KEY.
2. Ensure NiFi 2.4 ExecuteScript with the Groovy engine is installed. Add the
   generated verifier and receipt scripts using Script Body or Script File.
   The generated scripts include their shared code; no additional JWT JAR or
   Python runtime is required in NiFi. The cloud tests use Groovy 4.0.26/JDK 21.
3. On Verify user loan access, add the non-sensitive dynamic property
   `expectedTool=loan-due-report`. This is a fixed admin-owned value; never derive
   it from request attributes. Preserve the existing HTTP context map.
4. Require POST JSON on `/api/loan-due/query`. The verifier rejects URL query
   strings and rewrites the body to the verified report parameters, removing
   loan_access_token. Duplicate JSON members cannot reach QueryLoanDue because
   a single normalized object is written after query comparison.
5. Preserve QueryLoanDue's DBCP service and business configuration; its branch
   property must remain `${auth.branch_codes}`. Remove the old UpdateAttribute
   assignment `auth.branch_codes=[]`. No route may bypass the verifier.
6. Add Attach verified loan receipt on QueryLoanDue's success path only. Add
   backend-error.groovy on its failure path. Route verifier/receipt failures to
   the HTTP response. Keep failure payloads out of report response logs.
7. Configure authenticated TLS or an appropriate protected service network for
   gateway-to-NiFi traffic. Keep token-bearing request content out of logging and
   restrict access to NiFi content/provenance storage.

Tokens are short lived. A long Oracle query can finish after the token expires
if it was verified on receipt; Django checks current permissions before releasing
results. The receipt step deliberately does not reject an already running report
just because the original admission token expired.

## ContextForge setup — after NiFi is prepared

Keep the report's actual parameter contract. Add required string
`loan_access_token` and forward it unchanged in the HTTP POST JSON body to the
loan endpoint. Do not use GET/query-string forwarding for this verifier.

For a simple schema, prepare a copy with:

```sh
python deploy/nifi/signed-loan/patch_schema.py current-input-schema.json work/scoped-input-schema.json
```

This preserves report parameters, additionalProperties and existing requirements;
it refuses composed/property-limited schemas that need manual review. The supplied
`deploy/contracts/*scoped.input.schema.json` contracts may be used only when they
match the actual custom processor's supported parameters. Adding the token to the
schema without installing backend verification is incomplete.

## Django setup and diagnostics

In Integration settings, rediscover the exact tool, use SIGNED mode, enable loan
scope, and leave fixed branch codes empty. In User access, select tools and the
person's branches/zones or ALL. A user cannot supply their own authorization.

The root-only **Check backend setup** button checks discovery, required token
schema, signing-key presence, module activation and fixed-vs-signed mode. It never
executes a report. It labels successful discovery as 'schema detected', not a
claim that Oracle enforcement was verified.

From the usual admin machine after the new Django code is deployed:

```sh
kubectl --context mamun -n rag exec deployment/buro-django -c django -- python manage.py check_loan_backend --tool loan-due-report
```

This command prints no credentials or signing key. It returns nonzero for missing
setup and does not change configuration. The admin page's assigned-access preview
is deliberately separate from backend setup status.

## Staging acceptance before production

- Grant a test user only 0003. A supported 0003 report succeeds with a matching
  receipt; 0250 requests are blocked before SQL dispatch.
- Reject missing, expired, altered and wrong-tool tokens and tampered queries.
- Verify every SQL source/aggregate/ranking is constrained to verified branches.
  Inspect source or run a controlled dataset containing both authorized and
  unauthorized branch rows. Checking returned branch IDs alone cannot establish
  correctness of cross-branch sums.
- Verify ALL expands to the active directory; it does not mean a wildcard.
- Change/revoke a grant while a report runs; Django must withhold its result.
- Verify NiFi failures return 4xx/5xx and never a successful report envelope.
- Verify ContextForge preserves the response JSON object including authorization.

No deployment or production data mutation is performed by preparing this package.
