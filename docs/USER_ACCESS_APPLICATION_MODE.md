# User Access permissions for every module

This release makes UI-managed permissions the default for all registered tools.
Administrators assign tools, branch/zone scope and expiry in User Access.
Django checks those grants on the server; browser checkbox state is never trusted.
Explicitly unchecked tools stay blocked even when a legacy group permits them.
Module/tool activation, approvals, expiry, revocation and result replay checks
remain in force. Existing user grants are preserved.

## Changes on deployment

- Migration 0010 adds application-managed branch scope support.
- Migration 0011 configures loan-due-report for QueryLoanDueAppScope.
- Migration 0012 makes User Access (UI) the default for every tool and converts
  previous SIGNED/FIXED tool settings. It clears legacy fixed branch lists, not
  user policies. Existing financial-scope flags remain, including tools formerly
  configured with a fixed scope.
- Setup and admin forms no longer offer legacy signed/fixed modes.
- UI/APP financial execution uses no loan_access_token, signing key or JWT receipt.
- Unscoped tools use normal User Access tool permission checks. They do not
  acquire a financial branch requirement just because the default mode is UI.

Legacy signing/fixed helpers remain for backward compatibility and tests; migrated
tools and normal setup use UI/APP. Do not manually change database rows back to
legacy modes. New AgentTool records default to UI.

## Native processor without app_branch_codes

For the updated processor that accepts the same report fields but no
`app_branch_codes`, use
`deploy/contracts/loan-due-query.native-branch.input.schema.json` in ContextForge.
Keep the existing POST URL, authentication, credentials and report parameters.
Forward the entire JSON body, including `branch_code`, with leading zeros intact.
The application detects this native contract automatically; no database migration
or change to saved User Access policies is needed.

Django checks an explicit branch against the user's current branch/zone grants.
If the effective grant contains exactly one branch, Django supplies its
`branch_code` after validating the model request. The effective filter is also
used for response validation, presentation and dispatch auditing. No
`app_branch_codes` or loan token is sent to this processor.

For users with multiple permitted branches, ask for one branch explicitly.
Requests for several branches, broad zone rankings or all permitted branches are
blocked before dispatch when they cannot be represented by a single checked
branch filter. They are never implemented by filtering an already ranked result.
Use the branch-list contract below for cross-branch reporting.

The trusted NiFi endpoint must apply `branch_code` before every SQL aggregation
and top-N limit. Its unchanged response must echo the requested branch/zone,
confirm `authorized_branch_count=1`, and return the standard report metadata,
row count and data. Native mode does not require an `authorization_mode` marker.
Django rejects another branch, an unconfirmed scope, or changed/revoked grants.
Verify this filtering with a controlled dataset before serving reports.

Run `check_loan_backend --tool loan-due-report` after rediscovery. Both supported
contracts return `application_scope_detected`; this is schema discovery only,
not a live SQL filtering test.

## Branch-list transport setup (not user permission administration)

For processors that still support the branch-list contract, the loan service
must actually execute the application-resolved branch filter.
Use QueryLoanDueAppScope 1.0.1 on the trusted loan endpoint. Keep this endpoint
restricted to the application/gateway; it accepts the caller's scope and does
not authenticate users itself.

In ContextForge, edit the existing loan-due-report REST tool:

1. Keep its existing trusted NiFi URL, HTTP POST method, gateway membership,
   authentication and credentials.
2. Replace its input schema with the checked-in file:
   deploy/contracts/loan-due-query.application.input.schema.json
3. Forward the FULL JSON body to NiFi, including app_branch_codes. Keep leading
   zero branch IDs as strings. Do not replace this field with a static list.
4. Do not require loan_access_token. Do not remove report fields or the required
   application branch array from the transport schema.
5. Preserve the native JSON response, including success, scope and data.

The user/model never fills app_branch_codes. Django removes it from the model's
schema, resolves the saved policy, injects it immediately before MCP dispatch,
and validates the response and current policy before display or replay.
ALL resolves to all current active permitted branches; optional branch/zone
filters narrow the scope. Nothing defaults to unrestricted financial access.

Other financial endpoints must support their corresponding checked scope
contract before execution. This release does not change the savings processor's
SQL, old static NiFi allowlists, Oracle filtering or network accessibility.
Missing transport support produces a setup error; it does not silently execute
an unrestricted request. General nonfinancial tools need no loan token contract.

## Deploy and check

Use the established deploy-agentic-updated.sh workflow from a pinned clean
release checkout. It builds the images, imports them on worker-99, stops writers,
backs up SQLite, runs migrations and verifies the Recreate rollout. Preserve
release locks/backups on a failed release. See DEPLOYMENT_WORKFLOW.md.

After configuring the ContextForge schema and deploying:

```sh
kubectl --context mamun -n rag exec deployment/buro-django -c django -- \
  python manage.py check_loan_backend --tool loan-due-report
```

Expected: application_scope_detected and report_executed=false. This command
discovers contracts only; it does not query Oracle or prove live filtering.

For user 24123, verify Loan Due Report is selected in User Access and the intended
branch scope/expiry is saved. Then ask the original 2026-09-30 branch 0003
top-ten overdue-loan question. A grant alone is not proof of a working MCP body
mapping or Oracle query. No live on-prem deployment or financial report has been
executed from this cloud workspace.
