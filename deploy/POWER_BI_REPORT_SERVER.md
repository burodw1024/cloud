# Power BI Report Server handoff

Enable **Reporting setup → Power BI Report Server → Power BI handoff** and
use **Report service access** as a superuser to grant **Download Power BI
workbooks** to the intended users or groups. Downloads work before the Report Server is installed. When the server
is ready, add the internal web portal URL, such as
`https://powerbi.internal/reports`. The link opens in the user's browser under
their Report Server permissions.

From a structured result or **My report runs**, choose **Download Power BI
workbook**. The workbook contains a named `ReportData` table and a
`SourceScope` sheet with the source and report scope. It uses the authorized
report snapshot, which is cached for 30 minutes. The user can reuse the question
to create a fresh snapshot after expiry. Handle downloaded workbooks under the
organization's report data rules.

Use [Power BI Desktop for Report Server](https://learn.microsoft.com/power-bi/report-server/install-powerbi-desktop)
matched to the server release. Import the Excel workbook, choose the
`ReportData` table and optionally `SourceScope`, then build the report or use
an internal template. Keep identifier columns as text when setting Power Query
data types. Save or publish from Desktop to the authorized Report Server folder.
This handoff does not configure scheduled refresh. Refreshing from a local
workbook would require a stable, accessible data source and its own permissions.

Validate deployment with a non-sensitive report and a restricted test folder.
Check that the workbook opens, the imported data types and scope are correct,
and Report Server permissions match the intended audience.

## Publish a shared Detail table

Create **BURO Reports** in the Report Server portal. Give a dedicated Windows
account **Publisher** on that folder and give the intended audience **Browser**
access. The published report embeds every row of the authorized result and
stays in Report Server until removed. Everyone with access to the folder can
see those rows. If a report should no longer be shared, remove it in the
Report Server portal.

Set these environment variables on both the BURO web container and the
scheduler. Supply the password from the deployment's secret settings.

| Variable | Value |
| --- | --- |
| `AGENTIC_POWERBI_PUBLISH_USER` | Windows publishing account, such as `FileServer\buro-publisher` |
| `AGENTIC_POWERBI_PUBLISH_PASSWORD` | Windows account password |
| `AGENTIC_POWERBI_PUBLISH_FOLDER` | Report Server folder path, defaults to `/BURO Reports` |
| `AGENTIC_POWERBI_SOAP_URL` | Optional Report Server web service URL, including `/ReportService2010.asmx`, if the default `/ReportServer/ReportService2010.asmx` was customized. Use the same origin as the portal. |

Publishing uses Windows NTLM authentication through curl. Use the trusted
internal portal URL configured in Reporting setup. Apply Django migrations,
keep **Power BI handoff** enabled, and grant both **Download Power BI workbooks**
and **Publish Power BI Report Server reports** through **Report service access**.

Open a structured result from **My report runs** and choose **Publish to Power BI
Report Server** while the snapshot is available. For scheduled runs, choose
**Publish a shared Power BI Detail table**. The report run links to the published
report after Report Server confirms the upload. If confirmation times out, look
for `BURO-<run UUID>` in the target folder before creating another run.
