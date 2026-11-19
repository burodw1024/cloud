"""Private ContextForge upstream for report delivery.

Run with uvicorn deploy.report_delivery.server:app behind an internal TLS
endpoint. Provider credentials belong in the adapter workload's secrets.
"""
import base64
from email.message import EmailMessage
import hmac
import os
import re
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

mcp = FastMCP('BURO report delivery', stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(
                  enable_dns_rebinding_protection=True,
                  allowed_hosts=[host.strip() for host in os.environ.get(
                      'REPORT_DELIVERY_ALLOWED_HOSTS', 'localhost:*,127.0.0.1:*').split(',') if host.strip()],
              ))


def secret(key):
    value = os.environ.get(key, '').strip()
    if not value:
        raise ValueError('Provider setup incomplete.')
    return value


def bearer(token):
    return {'Authorization': 'Bearer ' + token}


def exchange(client, endpoint, payload):
    response = client.post(endpoint, data=payload)
    response.raise_for_status()
    token = response.json().get('access_token')
    if not isinstance(token, str) or not token:
        raise ValueError('No provider access token was returned.')
    return token


def outcome(provider, perform):
    try:
        with httpx.Client(timeout=httpx.Timeout(12, connect=4), follow_redirects=False) as client:
            return {'accepted': perform(client) is True}
    except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError):
        return {'accepted': False, 'error': f'{provider} acceptance could not be confirmed. Check provider activity before retrying.'}


@mcp.tool()
def deliver_gmail_report(to: str, subject: str, body: str) -> dict:
    """Send a reviewed report from the configured Gmail account."""
    def send(client):
        message = EmailMessage()
        message['From'] = secret('GMAIL_REPORT_SENDER')
        message['To'] = to
        message['Subject'] = subject
        message.set_content(body)
        token = exchange(client, 'https://oauth2.googleapis.com/token', {
            'grant_type': 'refresh_token', 'client_id': secret('GMAIL_CLIENT_ID'),
            'client_secret': secret('GMAIL_CLIENT_SECRET'), 'refresh_token': secret('GMAIL_REFRESH_TOKEN'),
        })
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode('ascii')
        response = client.post('https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
                               headers=bearer(token), json={'raw': raw})
        response.raise_for_status()
        identifier = response.json().get('id')
        return isinstance(identifier, str) and bool(identifier.strip())

    return outcome('Gmail', send)


@mcp.tool()
def deliver_outlook_report(to: str, subject: str, body: str) -> dict:
    """Send a reviewed report from a dedicated Microsoft 365 mailbox."""
    def send(client):
        tenant = secret('OUTLOOK_TENANT_ID')
        if not re.fullmatch(r'[a-fA-F0-9-]{36}', tenant):
            raise ValueError('Use the Microsoft Entra tenant ID.')
        sender = quote(secret('OUTLOOK_REPORT_SENDER'), safe='')
        token = exchange(client, f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token', {
            'grant_type': 'client_credentials', 'client_id': secret('OUTLOOK_CLIENT_ID'),
            'client_secret': secret('OUTLOOK_CLIENT_SECRET'), 'scope': 'https://graph.microsoft.com/.default',
        })
        response = client.post(f'https://graph.microsoft.com/v1.0/users/{sender}/sendMail',
                               headers=bearer(token), json={'message': {
                                   'subject': subject, 'body': {'contentType': 'Text', 'content': body},
                                   'toRecipients': [{'emailAddress': {'address': to}}],
                               }, 'saveToSentItems': True})
        response.raise_for_status()
        return response.status_code == 202

    return outcome('Outlook', send)


@mcp.tool()
def deliver_viber_report(receiver: str, type: str, text: str) -> dict:
    """Send a reviewed text report to a Viber bot subscriber."""
    if type != 'text':
        return {'accepted': False, 'error': 'This tool supports text only.'}

    def send(client):
        response = client.post('https://chatapi.viber.com/pa/send_message',
                               headers={'X-Viber-Auth-Token': secret('VIBER_BOT_TOKEN')},
                               json={'receiver': receiver, 'type': 'text', 'text': text,
                                     'sender': {'name': secret('VIBER_BOT_SENDER_NAME')}})
        response.raise_for_status()
        result = response.json()
        status = result.get('status')
        return isinstance(status, int) and status is not False and status == 0 and bool(result.get('message_token'))

    return outcome('Viber', send)


mcp_app = mcp.streamable_http_app()


async def app(scope, receive, send):
    if scope['type'] == 'http':
        expected = os.environ.get('REPORT_DELIVERY_MCP_TOKEN', '').encode()
        supplied = dict(scope.get('headers', ())).get(b'authorization', b'')
        if not expected or not hmac.compare_digest(supplied, b'Bearer ' + expected):
            await JSONResponse({'error': 'Authentication required.'}, status_code=401)(scope, receive, send)
            return
    await mcp_app(scope, receive, send)
