# Business modules and production navigation

`/agentic/` is the end-user workspace. Its navigation shows role-authorized
business modules and personal activity. Integration settings, module management
and cross-user attempt history require Staff status and their respective
permissions; hiding menu links is backed by permission checks on the routes.
Unconfigured WhatsApp readiness/configuration instructions are no longer rendered
in the workspace. Authorized result sharing remains available when configured.
Model, gateway and WhatsApp configuration remain under `/agentic/setup/`.

## Manage modules

Open `/agentic/modules/`. This is a business administration screen independent of
connection credentials. Module managers need Staff status and:

- `agentic_ai.add_businessmodule`
- `agentic_ai.change_businessmodule`
- `agentic_ai.change_agenttool`

Superusers already have these permissions. Delegate them through Django Groups
when appropriate. Module management does not grant `chat.change_systemsetting`
or permission to access integration settings.

Create a module with a name, stable URL name, description, starter questions and
navigation order. Select registered tools and publish it. New forms default to
paused. Tools assigned to another module must be detached there first; saves
never silently take them from a different module. Tool roles, activation and
approval requirements are preserved. Removing a tool from a module returns it
to the general directory under its existing permissions.

The module list distinguishes published and paused configuration. Published is
not a live-health assertion. A module appears in user navigation only if it is
published and contains an active tool authorized for that user. Starter questions
prepare editable input; they never execute by clicking. The module URL filters
the capability directory; the assistant retains access to the user's full
permitted catalog when selecting a tool.

Pausing a module blocks its assigned MCP tools during discovery, execution
permission checks and report replay. Do not delete paused modules to suspend
access: deletion through other database/admin mechanisms would detach tools.
Business modules apply to the MCP gateway workflow; local registry mode is
unchanged and does not display module navigation.

## Add a new business capability

1. Implement/configure the real backend API and gateway tool.
2. An integration administrator discovers it in Tool setup, configures roles,
   activation and approvals, checks the model and saves.
3. A module manager assigns it to a module, supplies supported starter questions
   and publishes it.
4. Verify an authorized user, an unauthorized user and the real business flow.

This screen organizes working integrations; it does not generate backend APIs,
Oracle procedures, connectors, new permissions or special report renderers.
The existing loan-specific report contracts, Bangla analysis, charts and question
guide are not automatically applied to a tool in another business domain.

## Deployment

Migration `0004_businessmodule_agenttool_module` adds the module table and a
nullable tool association. Existing tools stay unassigned, with their activation,
roles, approvals and existing categories intact. Run the project's normal
migration step before serving the updated application; no users or roles are
created automatically. No production configuration was changed from cloud.

Verified with isolated Django tests and synthetic browser fixtures, including
mobile layout, hidden admin controls, direct-route authorization, module pause,
cached-report denial, CSRF, assignment preservation and click-to-edit behavior.

## User-specific loan scope

The subsequent loan data access implementation adds an explicit per-user policy
and a signed backend contract. See [LOAN_DATA_ACCESS.md](LOAN_DATA_ACCESS.md)
for administration permissions, directory import, default-deny behavior and the
required NiFi/Oracle integration. Tool roles and business modules alone do not
grant branch or zone entitlements.
