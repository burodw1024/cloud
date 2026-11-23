# Agentic report delivery

The result screen supports the existing WhatsApp Cloud API integration and
dedicated Gmail, Outlook and Viber tools. For the new channels, Agentic checks
tool access, preview, the recipient allowlist and current report access before
dispatch. A provider acknowledgment means acceptance, with delivery verified
through the provider if needed.

## Configure providers

Use dedicated sender accounts. Only configure channels you want to enable.
Store secrets in the adapter workload, not in the Agentic settings form.

| Channel | Required credentials | Gateway tool before optional namespacing |
| --- | --- | --- |
| Gmail | `GMAIL_REPORT_SENDER`, `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`. Obtain a refresh token with the Gmail send scope for the configured mailbox. | `deliver_gmail_report` |
| Outlook | `OUTLOOK_REPORT_SENDER`, `OUTLOOK_TENANT_ID`, `OUTLOOK_CLIENT_ID`, `OUTLOOK_CLIENT_SECRET`. Grant Graph application Mail.Send and scope access to the dedicated mailbox in Exchange. | `deliver_outlook_report` |
| Viber | `VIBER_BOT_TOKEN`, `VIBER_BOT_SENDER_NAME`. Use actual bot subscriber IDs and an active provider account. Commercial terms may apply. | `deliver_viber_report` |
| WhatsApp | Existing ContextForge tool backed by Meta WhatsApp Cloud API. Configure a text-message tool and approved international numbers in the WhatsApp settings. | Deployment-specific |

Run the adapter using the repository's production dependencies. Set a distinct
`REPORT_DELIVERY_MCP_TOKEN` and configure ContextForge to use it as an
Authorization bearer token. Set `REPORT_DELIVERY_ALLOWED_HOSTS` to the internal
hostnames (include exact ports if used) that ContextForge sends in the Host header.
Serve the adapter behind internal TLS and restrict
access to the gateway. Restrict outbound access to the required provider APIs.

```bash
uvicorn deploy.report_delivery.server:app --host 0.0.0.0 --port 8080
```

Register the adapter's `/mcp` endpoint as Streamable HTTP in ContextForge.
Restrict access to its delivery tools in the gateway. In Agentic **Integration
settings**, discover and enable the selected tools for approved roles, then open
**Report delivery**, enter each exact gateway tool name and an approved
recipient list, and save. A superuser must also grant the matching channel in **Report service access** to
the approved role or user. Existing channel configuration and ContextForge tool
roles remain required. After upgrading, existing non-superuser senders need this
grant before they can send again.
The **Check tool** action validates discovery and the
input schema without sending. Verify each enabled channel with a non-sensitive
report and an approved test recipient before rollout. Failed or uncertain sends
require checking provider activity before another attempt.

Compatible gateway adapters may be used instead. Gmail and Outlook tools must
accept `to`, `subject`, and `body`. Viber tools must accept `receiver`, `type`,
and `text`. Return `{"accepted": true}` only after the provider acknowledges
acceptance.

## Twilio WhatsApp and SMS

Set these secrets and sender values on the report delivery adapter. Use
international sender numbers with a leading plus sign.

| Adapter variable | Value |
| --- | --- |
| `TWILIO_ACCOUNT_SID` | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_WHATSAPP_FROM` | Configured WhatsApp sender number |
| `TWILIO_SMS_FROM` | SMS-enabled Twilio number |

Restart the adapter, refresh its upstream tools in ContextForge, and discover
them in Agentic. In **Integration settings → WhatsApp result sharing**, save the
exact discovered name for `deliver_twilio_whatsapp_report` and your approved
recipient numbers. To use Meta again, select your Meta tool in the same setting.
In **Report delivery → Twilio SMS**, save the discovered name for
`deliver_twilio_sms_report` and approved phone numbers. Grant WhatsApp or
Twilio SMS access to the intended users or roles under **Report service access**.

The Twilio tools send an actual report summary of up to 1,600 characters.
Twilio's guided WhatsApp trial can send its sample templates from the Console.
For custom report text, enable custom messaging on the Twilio account and send
within the 24-hour window after the recipient messages your WhatsApp sender.
This adapter sends free-form text. Scheduled WhatsApp messages outside that
window require a separate approved-template integration. SMS sender and
destination requirements depend on the recipient country.

Use the message SID in Agentic's send response or audit record to check
delivery in Twilio Messaging Logs. A successful send through the tool confirms
provider acceptance.
