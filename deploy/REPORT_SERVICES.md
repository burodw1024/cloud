# WhatsApp and Superset as report services

Open `/agentic/services/` or choose **Services** in the workspace navigation.
Both services are visible even before setup, with instructions and role-aware
configuration links. Loading this page sends no messages, runs no report and
creates no Superset objects. Configured status is not a live health assertion.

## WhatsApp

1. In Integration settings, configure the exact MCP WhatsApp tool name and the
   allowed international recipient numbers. Discover and enable the tool and
   grant its role or explicit user-tool access. Branch enforcement is for
   financial report tools, not the WhatsApp delivery tool.
2. The tool must declare and accept the Cloud API text body: messaging_product,
   to, type=text, text.body. A hello_world-only template service cannot carry a
   report. Configure provider authentication and HTTP body mapping in
   ContextForge; credentials stay there, not in user questions.
3. In Services, click **Check WhatsApp connection**. It discovers and validates
   the tool within a 15-second check deadline and sends no message. It does not
   verify Meta acceptance or delivery. Checks are limited to one per user per
   30 seconds.
4. Run a question, select the configured recipient below the result, choose
   **Preview summary** or **Preview full result**, review, then **Confirm and
   send**. The report is held server-side and current access is checked again.
   Existing approval requirements remain; an approval-required response is not
   a sent message. Complete your existing approval process or have an authorized
   administrator review the tool's approval configuration.
5. Text messages require Meta's active customer-service messaging window. Outside
   that window an approved, appropriately configured template is required;
   template-based report delivery is not implemented by this text exporter.
   A long full report must use the summary instead (4,096-character text limit).

The app records the accepted message ID in its run log. MCP/HTTP response
wrappers are handled, but a success response without a message ID never counts
as verified acceptance. Delivery is not confirmed by acceptance; no webhook
delivery tracker is added here. Never retry an unknown send outcome blindly.

## Superset

1. Open **Services → Connect & test Superset** as an integration administrator.
   Configure API/browser addresses, service credentials, the dedicated analytics
   database and schema, each publisher's Superset user mapping, and verified
   private access. Use **Test saved connection**. Credentials remain encrypted.
   See `agentic_ai/SUPERSET_SETUP.md` for the backend requirements.
2. A publisher needs Staff and `agentic_ai.publish_dashboard`, plus source tool
   and branch/zone access. No permissions are granted automatically.
3. Run a supported structured report. Under its data, choose **Generate Superset
   report**, select a fixed template and numeric measures, then generate a
   preview. Explicitly choose **Create private Superset dashboard** to publish.
4. The action is bound to the selected server-held report token, not whatever
   another browser tab last queried. Expired, foreign and revoked snapshots are
   rejected without silently substituting another report. The 30-minute source
   snapshot and existing privacy projection remain in force.

Snapshots contain up to 100 supported displayed rows. Personal fields excluded
by the existing projection are not exported; unsupported/text-only results
explain why no export action is available. This is not live SQL, scheduled
refresh or publication by a language model. Review cleanup and retention in
`agentic_ai/SUPERSET_SETUP.md`.

The automated tests use synthetic gateway/Meta/Superset responses. Real WhatsApp
delivery and Superset connectivity must be checked on premises after deployment;
registration alone is not proof that the provider is operational.
