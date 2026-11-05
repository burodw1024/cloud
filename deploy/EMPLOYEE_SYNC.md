# Employee API registration and continuous sync

Apply migrations using the existing production settings:

```sh
python manage.py migrate
```

Open System Settings → Employee API sync settings (also in Django admin).
Create the singleton settings record, select the default Django group, configure
an HTTP/HTTPS NiFi URL, set active statuses to `Regular`, choose an interval
(default 300 seconds), and enable sync. Save, then click **Sync now**.
Only superusers and the designated `root_123` staff account can manage this module.
The directory list shows the last imported employee data; settings show the last
successful sync timestamp and worker result. Restrict this endpoint to your trusted
NiFi service; TLS verification is enabled for HTTPS. Redirects are rejected.

The on-prem `deploy-agentic-updated.sh` launcher installs an `employee-sync`
container in the Django pod, using the same image, environment and /data mount.
It stops with the app during backup and migration. The pod is now **3/3 Ready**.
Do not start a second worker when using this launcher. For other deployments,
run a supervised process using the same production settings and database:

```sh
python manage.py sync_employees --watch
```

The worker reads settings each cycle, pauses imports when disabled, retries API
failures on the configured interval, and reconnects to the database between runs.
For other deployments, cron can instead run `python manage.py sync_employees`.
Use one scheduling mechanism per deployment. Keep separate writers stopped
during database migrations.

NiFi must answer HTTP GET with HTTP 200, Content-Type `application/json`, and:

```json
[
  {
    "EMPLOYEEID": "123",
    "EMPLOYEECODE": "00123",
    "EMPLOYEENAME": "Example Employee",
    "BRANCH": "Jinarpur Branch",
    "SITECODE": "888",
    "DESIGNATIONNAME": "Assistant Programme Organiser",
    "GENDER": "Male",
    "EMP_WORKSPACE": "Branch",
    "EMPSTATUS": "Regular"
  }
]
```

An object containing a `rows` or `data` array is also accepted. Send IDs, codes
and site codes as strings to preserve leading zeroes. The response must include all
five columns per row. A malformed response, duplicate identity or account conflict
aborts the full import. Empty responses do not delete or deactivate accounts.
Employees omitted from a response remain unchanged; return their inactive status
explicitly to disable their login. Active statuses are case insensitive.

Employee ID is the stable identity; employee code becomes the login username.
New accounts get the requested initial password (stored with Django password
hashing) and the selected default group. Existing matching usernames are linked,
while retaining their passwords and groups. Later syncs update name, branch and
login status, without replacing groups or passwords. Root can change groups from
Django's Users admin. Staff/superuser accounts are excluded from employee changes.
`BRANCH` stores the location name (up to 255 characters); `SITECODE` stores its
code without padding or removing leading zeroes. Designation, gender and workspace
are stored and visible in the employee directory admin. When SITECODE is present,
only EMP_WORKSPACE=Branch updates the profile's primary branch code from SITECODE.
Zone, area and other location types are recorded without granting branch scope or
head-office privileges. Existing additional branch grants remain unchanged.
Legacy five-column feeds without SITECODE continue to treat BRANCH as a code of
up to 32 characters. For named locations, always include SITECODE and EMP_WORKSPACE.

Validation:

```sh
python -m django test chat.tests.test_employee_sync --settings=chat.tests.employee_settings
```

## On-prem release and tool access

Use the existing launcher from a clean, committed GitHub checkout. The launcher
runs both application and employee sync tests inside the built image before
migration, retains Kubernetes environment/Secrets, backs up SQLite with writers
stopped, applies migrations, and verifies the replacement pod and HTTPS health.

After configuring and enabling the endpoint, inspect worker logs:

```sh
kubectl --context mamun -n rag logs deployment/buro-django -c employee-sync --tail=50
kubectl --context mamun -n rag get pods
```

A new employee gets the selected Django group. Tool role matching uses exact,
case-sensitive group names. In Agent Tools, enable the desired registered tool
and its module and add this group to allowed roles. The exact advertised MCP
tool name must match the registered name. Loan reports also require a separate
loan access policy and scope; syncing a branch does not grant that policy.
Do not choose the privileged `admin` group as the ordinary employee default.

Pre-release checks passed: 350 application tests, including 68 focused tool
access/selection tests; 14 employee sync tests. Live NiFi connectivity, gateway
credentials/catalog and model behavior must still be verified on-prem. The
launcher already runs one authenticated report check after deployment.
