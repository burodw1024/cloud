"""Configuration and gateway contracts for outbound report delivery."""
import json
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from chat.dynamic_config import get_setting

CHANNELS = {'gmail': 'Gmail', 'outlook': 'Outlook', 'viber': 'Viber'}
SUBJECT = 'BURO Insight AI report'


def setting_keys(channel):
    prefix = f'MCP_{channel.upper()}_REPORT'
    return prefix + '_TOOL', prefix + '_RECIPIENTS'


def recipient_list(channel, values):
    if not isinstance(values, list) or len(values) > 50:
        raise ValueError('Configure up to 50 recipients.')
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 254:
            raise ValueError('Enter one valid recipient per line.')
        if channel == 'viber':
            if not re.fullmatch(r'[A-Za-z0-9_+/=-]{1,254}', value):
                raise ValueError('Use Viber bot subscriber IDs.')
        else:
            try:
                validate_email(value)
            except ValidationError:
                raise ValueError('Enter a valid email address per line.') from None
    return list(dict.fromkeys(values))


def configured(channel):
    tool_key, recipients_key = setting_keys(channel)
    return {
        'tool': get_setting(tool_key).strip(),
        'recipients': recipient_list(channel, json.loads(get_setting(recipients_key, '[]'))),
    }


def outbound_tool_names():
    names = {get_setting('MCP_WHATSAPP_TOOL', 'send-whatsapp-template').strip()}
    names.update(get_setting(setting_keys(channel)[0]).strip() for channel in CHANNELS)
    return names - {''}


def describe(channel, user=None):
    from .mcp_gateway import _allowed, gateway_enabled
    from .models import AgentTool

    detail = {'key': channel, 'label': CHANNELS[channel], 'ready': False, 'tool': '', 'recipients': []}
    try:
        config = configured(channel)
    except (ValueError, TypeError):
        return {**detail, 'status': 'Recipient settings are invalid.'}
    if user is None:
        return {**detail, **config}
    if not config['tool'] or not config['recipients']:
        return {**detail, 'status': 'Configure a gateway tool and approved recipients in Integration settings.'}
    record = AgentTool.objects.filter(name=config['tool']).first()
    if not gateway_enabled() or not record or not _allowed(record, user):
        return {**detail, 'status': 'This delivery channel is unavailable for your account.'}
    return {**detail, **config, 'ready': True, 'status': 'Configured. Gateway schema and provider delivery can be verified separately.'}


def delivery_arguments(channel, schema, recipient, text):
    from .mcp_gateway import _validator

    arguments = ({'receiver': recipient, 'type': 'text', 'text': text} if channel == 'viber'
                 else {'to': recipient, 'subject': SUBJECT, 'body': text})
    if not set(arguments) <= set(schema.get('properties', {})):
        raise ValueError(f'The {CHANNELS[channel]} gateway tool does not declare the required delivery fields.')
    try:
        _validator(schema).validate(arguments)
    except Exception:
        raise ValueError(f'The {CHANNELS[channel]} gateway tool rejected the report message schema.') from None
    return arguments


def accepted_delivery(data):
    """Require the adapter to explicitly acknowledge provider acceptance."""
    for _ in range(8):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                return False
        elif isinstance(data, dict):
            if data.get('isError') or data.get('error') or data.get('success') is False:
                return False
            if 'accepted' in data:
                return data['accepted'] is True
            if isinstance(data.get('structuredContent'), dict):
                data = data['structuredContent']
            elif isinstance(data.get('content'), list) and len(data['content']) == 1:
                content = data['content'][0]
                data = content.get('text') if isinstance(content, dict) else None
            else:
                return False
        else:
            return False
    return False
