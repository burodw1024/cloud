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

## Operations

Apply migration 0017 for durable claim leases and delivery reconciliation. The
worker processes up to two schedules at once, with one in-flight request per
user in the worker. It renews claims every ten seconds. A claim that expires
after a worker interruption is flagged for review. Once delivery can have
started, the request is never requeued. Use provider activity or Superset
export history to determine the outcome before creating a new schedule.

Superusers can open **Scheduled requests → Scheduler operations** to inspect
overdue, running, and unreviewed requests. Record what you verified and mark
the request reviewed. Reviewing a request preserves its delivery status and
does not send it again.

The existing Django Prometheus exporter exposes aggregate metrics prefixed
`agentic_scheduler_`. Point the existing scrape target at the app, then
load [the alert rules](agentic-scheduler-alerts.yml) into your Prometheus
rule configuration and route them to your operators. Configure and verify the
scrape and alert routing on-prem. This file does not install monitoring.

Before broad rollout, use a synthetic report and one approved recipient.
Verify that an overdue job alerts, a permission revoked after scheduling blocks
execution, and a worker restart after destination dispatch results in review
without a second send. Measure queue delay and SQLite lock contention with
your expected report duration and concurrency.
