# On-premises Superset dashboard publisher

## Included workflow

Run an authorized structured report in the workspace, open **Analytics studio**,
name the dashboard, generate the preview, adjust its row label and numeric
measures, then create it. The publisher stages the existing server-held,
privacy-projected result in PostgreSQL, registers a Superset dataset, creates a
source table and per-row comparison charts, lays them out, verifies chart queries,
and returns a private dashboard URL. No SQL or data from the browser is accepted.

This release uses Superset's v1 REST API and the `table` and
`echarts_timeseries_bar` visualization types. Validate against your installed
Superset version in staging before enabling production publishing. Superset is
an existing separately deployed service; this feature does not install it.

The preview is a local layout/data preview, not an embedded Superset renderer.
Exports are immutable snapshots of up to 100 displayed privacy-projected rows,
not full-portfolio datasets. Sensitive personal identifiers are omitted using the
existing summary projection. Numeric measures become per-row comparisons: MAX
over a unique generated row label preserves each value and avoids double counting.
Totals, growth and historical trends are deliberately not fabricated. Current
snapshot designs support a table plus up to four bar charts. There is no model
call, unrestricted SQL generation, public sharing, Power BI adapter, natural-language
dashboard intent routing, live refresh or scheduled report re-execution in this release.

## Required deployment configuration

1. Run `python manage.py migrate`. Grant staff users the
   `agentic_ai.publish_dashboard` permission. A Django account must also be active.
2. Create a dedicated PostgreSQL analytics database/schema, separate from the
   application database. Install `psycopg[binary]>=3.1,<4` in the Django runtime
   (see `requirements.superset.txt`). Superset also needs its PostgreSQL driver.
3. Add the analytics connection to Django `DATABASES['superset_snapshots']` and
   register the same database/schema in Superset. Obtain its database ID.
4. Create a service account with the REST permissions needed to create/update/
   delete datasets, charts and dashboards, query chart data, and assign owners.
   Create a separate Superset user for each authorized Django account. Do not map
   several Django accounts to the same Superset user.
5. Supply the following Django setting through your deployment's settings module.
   Load passwords from your existing secret store; do not commit their values.

```python
DATABASES['superset_snapshots'] = {
    'ENGINE': 'django.db.backends.postgresql',
    'NAME': 'analytics',
    'USER': 'snapshot_writer',
    'PASSWORD': os.environ['ANALYTICS_DB_PASSWORD'],
    'HOST': 'analytics-db.internal',
    'PORT': '5432',
    'OPTIONS': {'sslmode': 'verify-full'},
}
AGENTIC_SUPERSET = {
    'URL': 'https://superset.internal',
    'USERNAME': os.environ['SUPERSET_SERVICE_USER'],
    'PASSWORD': os.environ['SUPERSET_SERVICE_PASSWORD'],
    'DATABASE_ID': 3,
    'DATABASE_ALIAS': 'superset_snapshots',
    'SCHEMA': 'agentic',
    'USER_IDS': {'12': 27},  # Django user PK -> dedicated Superset user ID
    'PRIVATE_ACCESS_VERIFIED': True,  # only after the checks below
    # 'CA_BUNDLE': '/etc/ssl/certs/organization-ca.pem',
    # 'ALLOW_HTTP': True,  # only if explicitly using a trusted internal HTTP origin
}
```

Prepare the schema once, as a database administrator:

```sql
CREATE SCHEMA agentic AUTHORIZATION snapshot_writer;
GRANT USAGE ON SCHEMA agentic TO superset_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE snapshot_writer IN SCHEMA agentic
  GRANT SELECT ON TABLES TO superset_reader;
```

Superset must connect as `superset_reader`, not the writer. Configure the schema
and database IDs consistently on both services. The publisher only creates tables
with generated identifiers and inserts parameters; it never runs user SQL.
The application must be able to reach Superset over HTTP(S) and PostgreSQL over
the configured private network. Standard requests proxy/CA environment settings
are respected and redirects are rejected. TLS verification cannot be disabled.

Publication currently runs within the HTTP request. Keep the application's long
report request timeouts (the supplied deployment uses 1,860 seconds for Gunicorn
and 1,900 seconds for Nginx). A killed request remains in the journal for review;
never retry it automatically. Production workers must share the application's
existing report cache so the studio can retrieve the preceding report.

## Private access and revocation

Before setting `PRIVATE_ACCESS_VERIFIED`, test with two real Superset users:

- Only the mapped owner, publishing service account and intended administrators
  can view the dashboard/dataset/charts. Dashboards are created unpublished with
  no role grants or guest tokens.
- Other users must not have all-database, schema-wide or all-datasource access to
  the snapshot database, SQL Lab access, ownership, or broad roles that expose it.
  Superset's own permissions remain authoritative; unpublished status alone is
  not a security boundary. Enable and validate dashboard RBAC where applicable.
- Confirm the mapped owner can execute chart queries and that an unrelated user
  cannot access charts or datasets directly. Test both UI and API routes.

The application rechecks current report permissions before export, during
publication, and when opening the dashboard through the studio. Direct Superset
URLs refer to durable copies: Django permission revocation does not itself remove
those copies. Schedule the cleanup command (for example, every five minutes) and
include Superset account revocation in your identity lifecycle. Cleanup latency
is the interval of that scheduled job; this is not instantaneous revocation.

```sh
python manage.py cleanup_dashboard_exports --days 7         # dry run
python manage.py cleanup_dashboard_exports --days 7 --apply
```

The studio's **Remove export** deletes recorded dashboards, charts, datasets and
the PostgreSQL table. Failed exports remain in the journal for review and cleanup.
Never automatically retry publication after a timeout. An object may have been
created before its ID was received; reconcile Superset audit logs for unrecorded
objects before retrying. A process killed in `creating` needs administrator
reconciliation before its status is changed to `failed` for cleanup.

## Deployment verification

Run the automated suite:

```sh
python -m django test agentic_ai.tests.test_dashboard_studio --settings=agentic_ai.tests.settings
```

Then use a staging Superset/PostgreSQL installation: export a report containing
leading-zero branch codes, numeric savings and null values; verify table labels,
chart values, layout, ownership, unrelated-user denial, duplicate-request
handling, revoked-access cleanup and retained-record cleanup. Test interrupted
publication. Do not enable production publishing until those live checks pass.
