# User access console

Root administrators can open **User access** in the application navigation at
`/agentic/user-access/`. Ordinary staff and delegated directory administrators
cannot use this console. Existing delegated data-access screens remain available.

## Give a person branch access

1. Register a new branch using **Manage branches** if it is not in the directory.
   Select its zone, enter the exact branch ID (including leading zeros), name,
   and activate it. Directory registration alone grants nobody access.
2. Find the person by name or username and select them.
3. Check the tools they need. Only active tools in active modules are offered.
4. Select **Choose branches or zones** and check the branch. Select an entire
   zone only when the person needs all its active branches, including future ones.
5. Use **Preview access** to inspect tools, branch IDs, example questions and
   approval requirements. Preview does not save or execute a report.
6. Optionally set an expiry in Bangladesh time. Click a reason shortcut or enter
   your own reason, then **Save access**.

For example, selecting Loan due report and branch `0012` permits supported loan
report questions within that branch. Branch `0013` remains outside the user's
scope. Example questions come from tool configuration and do not extend access;
backend setup and parameter validation still apply.

To revoke, open **Revoke this person's access**, enter or click a reason and
select **Revoke all agent access**. The explicit empty policy blocks inherited
group tool permissions too. The account and its group memberships stay intact.
Root accounts cannot be targeted in this console.

## Deployment

Apply the included migration using the application's normal deployment process:

```sh
python manage.py migrate
```

Migration `0009_usertoolaccess` adds the per-user tool policy. It does not change
any existing users' assignments. Run the migration before serving the new code.
No production permissions are changed by installing this feature.

Users without this policy continue to use existing role rules. Saving a person
in this console creates an explicit allowlist that **replaces**, rather than
adds to, group-derived tool access. This applies to registered MCP tools and
matching built-in local tools. Loan tools continue to require the scoped MCP
backend. Tool/module activation, account activation, financial branch scope,
backend authorization and approval requirements are still enforced.

Expiry applies to the explicit tool policy and the saved financial scope.
Expired policies deny tools instead of falling back to groups. Financial and
individual tool grants are saved in one transaction with their audit entry;
an audit failure rolls the grant back. Stale form versions are rejected.
Permission checks are repeated for execution and cached result replay.
Previously dispatched backend requests may already be in progress when an
administrator changes access.

The old Data access page can still update financial scope independently; it
does not replace the explicit tool selection. Review both when using that page.

## Backend setup versus assigned access

The console now includes a root-only Check backend setup action. It discovers MCP
tools without running reports and explains missing token schemas, a missing
Django signing key, paused tools/modules, and fixed scope. A detected schema is
not a claim that the NiFi verifier or Oracle scope was tested. See
`deploy/nifi/signed-loan/README.md` for the local integration implementation and
staging acceptance steps. The assigned-access preview does not certify backend
readiness.
