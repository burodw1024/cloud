"""Explicit, user-scoped WhatsApp exports through the configured MCP gateway."""
import asyncio
import hashlib
import json
import re
import uuid

from asgiref.sync import async_to_sync, sync_to_async
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect

from chat.dynamic_config import get_setting
from .api import _can_replay_result
from .mcp_gateway import (_allowed, _decision, _failure, _validator, discover_tools,
                          gateway_enabled, load_gateway_config, mcp_http_client)
from .models import AgentRunLog, AgentTool
from .share_channels import CHANNELS, SUBJECT, accepted_delivery, delivery_arguments, describe

TTL = 900


def configuration():
    numbers = json.loads(get_setting('MCP_WHATSAPP_RECIPIENTS', '["8801859135478"]'))
    if (not isinstance(numbers, list) or len(numbers) > 50
            or any(not isinstance(n, str) or not re.fullmatch(r'[1-9][0-9]{7,14}', n) for n in numbers)):
        raise ValueError('Configure up to 50 international phone numbers, digits only.')
    tool = get_setting('MCP_WHATSAPP_TOOL', 'send-whatsapp-template').strip()
    return {'tool': tool, 'recipients': list(dict.fromkeys(numbers))}


def recipient_text(value):
    numbers = [n.strip().lstrip('+') for n in re.split(r'[,\n]', value) if n.strip()]
    # Reuse strict configuration validation without touching persisted settings.
    if len(numbers) > 50 or any(not re.fullmatch(r'[1-9][0-9]{7,14}', n) for n in numbers):
        raise ValueError('Use up to 50 international numbers, one per line, for example 8801XXXXXXXXX.')
    return list(dict.fromkeys(numbers))


def settings_status(user):
    """Local readiness only: never sends a message or claims Meta delivery."""
    try:
        config = configuration()
    except (ValueError, TypeError):
        return {'tool': '', 'recipients': [], 'ready': False,
                'status': 'Invalid WhatsApp settings. Ask an administrator to check setup.'}
    record = AgentTool.objects.filter(name=config['tool']).first()
    if not config['tool'] or not config['recipients']:
        message = 'Configure a WhatsApp tool and recipient in Set up tools.'
    elif not gateway_enabled():
        message = 'MCP gateway is paused. Complete tool setup and enable the gateway.'
    elif not record:
        message = 'WhatsApp tool has not been discovered. Discover tools and enable the exact configured name.'
    elif not record.is_active:
        message = 'WhatsApp tool is disabled. Enable it in Set up tools.'
    elif not _allowed(record, user):
        message = 'Your role is not authorized for the WhatsApp tool.'
    else:
        return {**config, 'ready': True,
                'status': 'Local settings ready. Run a question to preview a message. Meta authentication and delivery are not verified.'}
    # Do not expose recipient numbers to unauthorized users.
    return {'tool': config['tool'], 'recipients': [], 'ready': False, 'status': message}


def availability(user):
    status = settings_status(user)
    return {'tool': status['tool'], 'recipients': status['recipients']} if status['ready'] else {'tool': '', 'recipients': []}


def text_arguments(schema, recipient, text):
    if not {'messaging_product', 'to', 'type', 'text'} <= set(schema.get('properties', {})):
        raise ValueError('The WhatsApp service must declare messaging_product, to, type and text. Ask an administrator to register a text-message tool; a template-only tool cannot send report text.')
    arguments = {'messaging_product': 'whatsapp', 'to': recipient, 'type': 'text', 'text': {'body': text}}
    try:
        _validator(schema).validate(arguments)
    except Exception:
        raise ValueError('This WhatsApp tool does not accept report text. Configure a Cloud API text-message tool; a hello_world-only template cannot send the report.') from None
    return arguments


def accepted_message_id(data):
    """Read common MCP/HTTP wrappers without treating tool success as delivery."""
    for _ in range(8):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                return None
        elif isinstance(data, dict):
            if data.get('isError') or data.get('error') or data.get('success') is False:
                return None
            messages = data.get('messages')
            if isinstance(messages, list):
                for message in messages:
                    identifier = message.get('id') if isinstance(message, dict) else None
                    if isinstance(identifier, str) and identifier.strip() and len(identifier) <= 500:
                        return identifier
                return None
            if 'structuredContent' in data:
                data = data['structuredContent']
            elif isinstance(data.get('content'), list) and len(data['content']) == 1:
                data = data['content'][0].get('text')
            else:
                data = next((data[key] for key in ('result', 'data', 'body') if key in data), None)
        else:
            return None
    return None


def export_text(result, mode):
    answer = str(result.get('answer') or '').strip()
    report = (result.get('raw') or {}).get('structuredContent')
    if mode == 'summary':
        # Extractive summary: no model call and no invented calculations.
        text = str(result.get('summary') or answer).strip()
        if len(text) > 1000:
            text = text[:997] + '…'
        return 'Result summary\n' + text
    text = answer
    if isinstance(report, dict) and isinstance(report.get('columns'), list) and isinstance(report.get('rows'), list):
        columns = report['columns']
        lines = [' | '.join(str(c) for c in columns)]
        lines += [' | '.join(str(row.get(c, '')) for c in columns) for row in report['rows']]
        text += '\n\n' + '\n'.join(lines)
        if report.get('truncated'):
            text += '\nOnly returned rows are included; the source report has more records.'
    if not text.strip():
        raise ValueError('This result has no text to send.')
    if len(text) > 4096:
        raise ValueError('Full result exceeds WhatsApp’s 4,096-character text limit. Send the summary instead. Nothing was sent.')
    return text


def remember_result(user, result):
    if not result.get('ok') or result.get('kind') in {'clarification', 'loan_question_help', 'business_question_help'}:
        return
    whatsapp_ready = bool(availability(user)['recipients'])
    other_ready = any(describe(channel, user)['ready'] for channel in CHANNELS)
    if not whatsapp_ready and not other_ready:
        return
    token = uuid.uuid4().hex
    cache.set(f'agentic:whatsapp:result:{user.pk}:{token}', result.copy(), TTL)
    if whatsapp_ready:
        result['whatsapp_result_id'] = token
    if other_ready:
        result['share_result_id'] = token


async def dispatch(config, name, recipient, text, user, source_result=None, channel="whatsapp"):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {'Authorization': 'Bearer ' + config['token']} if config['token'] else {}
    async with streamablehttp_client(config['url'], headers=headers, timeout=config['timeout'],
            sse_read_timeout=config['timeout'], httpx_client_factory=mcp_http_client) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await discover_tools(session, {name})
            tool = next((t for t in tools if t.name == name), None)
            if tool is None:
                raise ValueError('The configured delivery tool is not available on this gateway.')
            arguments = (text_arguments(tool.inputSchema, recipient, text) if channel == 'whatsapp'
                         else delivery_arguments(channel, tool.inputSchema, recipient, text))
            if source_result is not None and not await sync_to_async(_can_replay_result)(user, source_result):
                return {'ok': False, 'http_status': 403, 'error': 'Report access changed before sharing. Nothing was sent.'}
            if channel != 'whatsapp':
                current = await sync_to_async(describe)(channel, user)
                if not current['ready'] or current['tool'] != name or recipient not in current['recipients']:
                    return {'ok': False, 'http_status': 403, 'error': 'Sharing access changed. Nothing was sent.'}
            decision = await sync_to_async(_decision)(user, name, arguments)
            if decision:
                return decision
            # No retry: a timeout after dispatch can have an unknown delivery outcome.
            response = await session.call_tool(name, arguments=arguments)
            data = response.model_dump(mode='json', by_alias=True, exclude_none=True)
            if data.get('isError'):
                return {'ok': False, 'error': 'Delivery tool reported an error. Check ContextForge logs before retrying.'}
            if channel != 'whatsapp' and accepted_delivery(data):
                return {'ok': True, 'message': f'{CHANNELS[channel]} accepted the message. Delivery is not yet confirmed.'}
            identifier = accepted_message_id(data) if channel == 'whatsapp' else None
            if identifier:
                return {'ok': True, 'message_id': identifier,
                        'message': 'Meta accepted the message. Delivery is not yet confirmed.'}
            return {'ok': False, 'error': 'Acceptance could not be verified. Check gateway logs before retrying; do not assume the message failed.'}


def reply(data, status=200):
    response = JsonResponse(data, status=status)
    response['Cache-Control'] = 'no-store'
    return response


@csrf_protect
def whatsapp_api(request, channel="whatsapp"):
    if not request.user.is_authenticated or not request.user.is_active:
        return reply({'ok': False, 'error': 'Authentication required.'}, 401)
    if request.method != 'POST':
        return reply({'ok': False, 'error': 'POST required.'}, 405)
    if channel not in ('whatsapp', *CHANNELS):
        return reply({'ok': False, 'error': 'Unknown sharing channel.'}, 404)
    label = CHANNELS.get(channel, 'WhatsApp')
    dispatched = False
    try:
        if len(request.body) > 8192:
            raise ValueError('Request is too large.')
        body = json.loads(request.body)
        if not isinstance(body, dict):
            raise ValueError('Expected a JSON object.')
        config = availability(request.user) if channel == 'whatsapp' else describe(channel, request.user)
        recipient, token, mode = body.get('recipient'), body.get('result_id'), body.get('mode')
        if not config['tool'] or recipient not in config['recipients']:
            return reply({'ok': False, 'error': 'Recipient or delivery tool is not authorized.'}, 403)
        if not isinstance(mode, str) or mode not in {'summary', 'full'} or not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
            raise ValueError('Choose a valid result and summary/full mode.')
        result = cache.get(f'agentic:whatsapp:result:{request.user.pk}:{token}')
        if not result:
            raise ValueError('Result expired. Run the question again before sending.')
        if not _can_replay_result(request.user, result):
            return reply({'ok': False, 'error': 'You no longer have access to this result.'}, 403)
        text = export_text(result, mode)
        preview_key = 'agentic:share:preview:' + hashlib.sha256(
            f'{request.user.pk}:{channel}:{token}:{recipient}:{mode}:{config["tool"]}:{text}'.encode()).hexdigest()
        if body.get('action') == 'preview':
            if channel != 'whatsapp':
                cache.set(preview_key, True, TTL)
            return reply({'ok': True, 'text': text, 'characters': len(text),
                          'subject': SUBJECT if channel in {'gmail', 'outlook'} else ''})
        if body.get('action') != 'send' or body.get('confirmed') is not True:
            raise ValueError('Preview and confirm the message before sending.')
        if channel != 'whatsapp' and not cache.get(preview_key):
            raise ValueError('Preview this recipient and message before sending.')
        dedupe_scope = '' if channel == 'whatsapp' else channel + ':'
        digest = hashlib.sha256(f'{request.user.pk}:{dedupe_scope}{token}:{recipient}:{mode}'.encode()).hexdigest()
        if not cache.add('agentic:whatsapp:send:' + digest, True, TTL):
            return reply({'ok': False, 'error': 'This message was already requested. Check its status before retrying.'}, 409)
        audit = AgentRunLog.objects.create(user=request.user, user_query=f'{label} {mode} export',
            selected_tool=config['tool'], detected_intent=f'{channel}_export', status='failed', risk_level='high',
            tool_input={'mode': mode, 'recipient_suffix': recipient[-4:], 'result_id': token},
            error_message='Dispatch pending; check gateway before retrying.')
        gateway = load_gateway_config()
        dispatched = True
        extra = {} if channel == 'whatsapp' else {'channel': channel}
        output = async_to_sync(asyncio.wait_for)(
            dispatch(gateway, config['tool'], recipient, text, request.user, result, **extra), gateway['request_timeout'])
        audit.status = 'success' if output.get('ok') else ('approval_required' if output.get('approval_required') else 'failed')
        audit.error_message = '' if output.get('ok') else output.get('error', output.get('answer', ''))
        if output.get('ok'):
            audit.tool_output_summary = ('Meta accepted message: ' + str(output.get('message_id', 'not supplied'))
                                         if channel == 'whatsapp' else f'{label} accepted the message.')
        audit.save(update_fields=['status', 'error_message', 'tool_output_summary'])
        if output.get('answer') and not output.get('message'):
            output['message'] = output['answer']
        return reply(output, output.get('http_status', 200 if output.get('ok') else 502))
    except (ValueError, TypeError, UnicodeError) as exc:
        return reply({'ok': False, 'error': str(exc)}, 400)
    except Exception as exc:
        code, message, status = _failure(exc)
        if dispatched:
            message += ' Delivery may have started. Check gateway logs before retrying.'
        return reply({'ok': False, 'code': code, 'error': message}, status)
