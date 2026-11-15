# Production Superset reports and fixed templates

## Admin: one setup screen

Open **Reporting setup** at `/agentic/reporting-settings/` (also linked from
Integration settings). A superuser can manage it. Delegated setup administrators
need Staff plus `add_agenttool`, `change_agenttool` and
`chat.change_systemsetting`, matching integration administration.

1. **Connect Superset:** enter its API address, browser address if different, and
   publishing service username/password. HTTPS is the default. Internal HTTP
   needs the explicit trusted-service checkbox.
2. **Report storage:** enter the existing Superset analytics database ID and the
   dedicated PostgreSQL writer connection/schema. Superset must read that same
   schema through its registered connection. TLS defaults to certificate and
   hostname verification. The writer needs CREATE in this schema; use a separate
   read account for Superset. This feature does not provision PostgreSQL or
   Superset. Reuse your separately deployed analytics service.
3. **Users and template:** map employees to their own Superset users, one per
   line, such as `24123=27`. Select Source table, Table + comparisons or Management
   report pack. Confirm private Superset access only after testing it with an
   owner and an unrelated account. Unpublished status alone is not a security
   boundary. Ordinary broad SQL Lab/database roles must not expose snapshots.
4. Click **Save reporting setup**, then **Test saved connection**. The test logs
   into Superset, checks the registered database and performs a read-only
   PostgreSQL connectivity/schema-privilege check. It creates no report tables.
   It does not prove Superset's complete cross-user access policy.
5. Grant trusted publishing users Staff and `agentic_ai.publish_dashboard` through
   existing Django Users/Groups. Tool and branch/zone access are still required
   separately. Each publishing employee needs a different Superset account.

Passwords are encrypted with a key derived from Django's SECRET_KEY, never
returned to the browser or stored in the export journal. Blank password inputs
retain saved passwords. Changing service/database destinations requires renewed
credentials. Keep SECRET_KEY stable and use SECRET_KEY_FALLBACKS during rotation.
Existing deployment-managed AGENTIC_SUPERSET settings remain a fallback until an
administrator saves configuration. Private CA trust remains deployment-managed.

## User workflow

Run a query in the workspace. Structured results offer three fixed local layouts:
Source table, Table + comparisons and Management report pack. Templates preserve
returned rows, values and order; comparisons use at most the first 20 rows.
Numeric identifiers remain labels. No totals or historical trends are invented.
Text-only answers stay as text. Layout selection and printing make no model or
report calls and do not publish anything.

When a Superset service is configured, a separate **Production reporting /
Superset** section appears. Its service link opens your existing Superset UI.
Authorized publishers can open **Choose a template & preview**. The studio uses
the latest server-held report projection, lets you choose a fixed template and
numeric fields, and offers explicit **Create private Superset dashboard** only
when the connection and user binding are configured. Configured is not a live
health assertion; use the saved-connection test and a real staging report.

Publishing copies up to 100 already authorized, privacy-projected displayed rows
to the dedicated analytics database, registers a Superset dataset and creates a
source table plus up to four per-row comparison charts. These are immutable
snapshots, not live datasets or scheduled query refreshes. Names, phone numbers
and member/employee identifiers omitted by the existing projection are not
exported. Generic unsupported report fields may be excluded; local templates
can still show the existing returned table.

The publisher rechecks current authorization before staging, during creation and
before opening. Each export has a durable journal and duplicate protection. No
browser SQL/report rows or automatic publication retries are accepted. Failed
or interrupted publication may leave remote objects; review the export journal
and Superset audit logs before retrying. Test chart compatibility against the
actual installed Superset version before production use.

## Retention and revocation

Superset owns access to durable copies. Revoking a Django grant cannot recall a
copy already created in another system. Schedule the cleanup command with your
existing job mechanism, and include Superset account access in your employee
lifecycle. For example, review then apply retained/revoked export cleanup:

```bash
python manage.py cleanup_dashboard_exports --days 7
python manage.py cleanup_dashboard_exports --days 7 --apply
```

Cleanup never removes exports still marked creating; administrators must
reconcile an interrupted publication first. Direct dashboard URLs remain subject
to Superset's permissions and cleanup latency. Existing source authorizations,
NiFi contracts and reports are unchanged.

## Deployment

The existing on-prem launcher builds the image (now including pinned psycopg
3.2.9), tests it, copies/imports images to the worker, backs up the app database,
runs additive migration 0011 and deploys Kubernetes. After pulling main:

```bash
unset ENV_FILE
export PUBLIC_URL=https://192.168.1.99:31135
bash source/deploy-agentic-updated.sh --check-source
bash source/deploy-agentic-updated.sh
```

Then configure the service through Reporting setup. No initial service binding or
publishing permissions are granted by migration. Test an actual authorized
report, table/chart values, unrelated-user denial and cleanup on premises.
