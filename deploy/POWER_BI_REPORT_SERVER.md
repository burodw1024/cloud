# Power BI Report Server handoff

Enable **Reporting setup → Power BI Report Server → Power BI handoff** and
grant **Export authorized Power BI report snapshots** to the intended users or
groups. Downloads work before the Report Server is installed. When the server
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
