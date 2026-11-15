# Simple user and module access

Application-managed access is supported by the native loan query endpoint. No
per-user signing key or new NiFi processor authorization mode is required. The
existing endpoint must apply the supplied branch_code before aggregation/ranking
and return its native scope evidence. This is not post-query filtering.

## One-time integration setting

After deployment, open `/agentic/setup/`, Find tools, and Check model. For
`loan-due-report` select **User Access permissions (application-managed)** and
save. The tool must advertise the native branch_code/report_date contract.
Incompatible schemas are rejected before any settings are saved. Existing SIGNED
and FIXED settings are deliberately preserved; the release does not silently
change an integration's trust contract. Publish its business module as usual.

## Grant an employee access

Open **User access** (`/agentic/data-access/`), search the employee code (for
example `24123`), and click Manage. Select **Choose tools for this user**, check
the required tools listed by module, select the permitted branches or zones,
enter a justification, and click **Save user access**. No Django group changes
are needed for explicit selections. Unselected tools are denied even if a group
would allow them. Empty selections deny every tool. Disabled tools and paused
modules remain unavailable. Choose **Keep group-based access** with an empty
selection to remove an explicit policy and return to existing group rules.

A superuser can manage this directly. Delegated administrators need Staff,
`agentic_ai.manage_loan_access` and `agentic_ai.change_usertoolaccess` for the
combined form. Granting all branches additionally needs `grant_all_loan_access`.
A scope-only administrator cannot change tool assignments. Delegated admins
cannot change their own access. Both policy changes are saved atomically with
their audit record, stale edits are rejected, and explicit tool expiry follows
the saved expiry. No existing assignments are rewritten by deployment.

With a native single-branch endpoint, ask for one permitted branch per report.
Multi-branch scope requires an endpoint explicitly supporting app_branch_codes
and the application-scope response contract. Do not use an unrestricted endpoint
or remove backend response validation to make a report pass.

## On-prem deployment

From the existing release directory (the folder containing `source`):

```bash
git -C source pull --ff-only origin main
unset ENV_FILE
export PUBLIC_URL=https://192.168.1.99:31135
bash source/deploy-agentic-updated.sh --check-source
bash source/deploy-agentic-updated.sh
```

The existing launcher builds the reviewed image, runs application and employee
sync tests, pushes the registry images, copies/imports them to worker
`dw@192.168.1.99`, backs up SQLite with writers stopped, migrates, deploys to
Kubernetes, and checks HTTPS health and an authenticated report. The URL defaults
to the supplied worker address and remains overridable with PUBLIC_URL. Existing
host/CSRF settings and the TLS certificate must accept this address. No TLS,
source compatibility, backup or report checks are bypassed.

Complete the one-time tool-mode setup before the launcher's final authenticated
report check if the old live mode is SIGNED; otherwise it may report a report
configuration failure after the application rollout. Fix the setting and rerun
the report verifier rather than disabling the check. Keep the launcher's printed
release snapshots and results for diagnosis.
