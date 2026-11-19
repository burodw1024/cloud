# Scheduled Agentic reports

Grant **Schedule reports** through **Report service access**. Users can then
open **Scheduled requests**, enter an explicit question and a future date and
time in Asia/Dhaka, and choose an available destination.

- **My report runs** saves an authorized report snapshot.
- **Private Superset dashboard** uses the configured default template and the
  user's existing Superset permissions and mapping.
- **Power BI workbook** prepares an authorized download for the existing
  Desktop workflow. Download it within the 30-minute snapshot window.
- **WhatsApp, Gmail, Outlook, Viber** send a text summary to a selected
  approved recipient. Provider acceptance and delivery are different states.

The first release supports one-time schedules for scoped loan and savings
reports. The worker filters the tool catalog for report tools and requires a
verified application, fixed or signed branch grant before dispatch. Permissions
and destination access are checked again when the report runs. Pending work can
be cancelled. Dispatched work is deliberately not retried automatically, since
publication or delivery may already have happened. Check report history, User
attempts and provider activity if a scheduled request is interrupted.

The standard on-prem launcher applies migration 0016 and starts the
`agentic-scheduler` container in the Django pod. It shares the existing image,
runtime configuration and SQLite data mount. The launcher stops it with the
other writers during deployment. A ready pod should show four containers.
