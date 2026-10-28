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

TTL = 900


def configuration():
    numbers = json.loads(get_setting('MCP_WHATSAPP_RECIPIENTS', '[]'))
    if (not isinstance(numbers, list) or len(numbers) > 50
            or any(not isinstance(n, str) or not re.fullmatch(r'[1-9][0-9]{7,14}', n) for n in numbers)):
        raise ValueError('Configure up to 50 international phone numbers, digits only.')
    tool = get_setting('MCP_WHATSAPP_TOOL', '').strip()
    return {'tool': tool, 'recipients': list(dict.fromkeys(numbers))}


def recipient_text(value):
    numbers = [n.strip().lstrip('+') for n in re.split(r'[,\n]', value) if n.strip()]
    # Reuse strict configuration validation without touching persisted settings.
    if len(numbers) > 50 or any(not re.fullmatch(r'[1-9][0-9]{7,14}', n) for n in numbers):
        raise ValueError('Use up to 50 international numbers, one per line, for example 8801XXXXXXXXX.')
    return list(dict.fromkeys(numbers))


def availability(user):
    try:
        config = configuration()
        record = AgentTool.objects.filter(name=config['tool'], is_active=True).first()
        if gateway_enabled() and record and _allowed(record, user):
            return config
    except (ValueError, TypeError):
        pass
    return {'tool': '', 'recipients': []}


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
    if (not result.get('ok') or result.get('kind') == 'clarification'
            or not availability(user)['recipients']):
        return
    token = uuid.uuid4().hex
    cache.set(f'agentic:whatsapp:result:{user.pk}:{token}', result.copy(), TTL)
    result['whatsapp_result_id'] = token


async def dispatch(config, name, recipient, text, user):
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
                raise ValueError('Configured WhatsApp tool is not available on this gateway.')
            arguments = {'messaging_product': 'whatsapp', 'to': recipient,
                         'type': 'text', 'text': {'body': text}}
            try:
                _validator(tool.inputSchema).validate(arguments)
            except Exception:
                raise ValueError('WhatsApp tool must accept the Cloud API text-message body. A hello_world-only template tool cannot send report text.') from None
            decision = await sync_to_async(_decision)(user, name, arguments)
            if decision:
                return decision
            # No retry: a timeout after dispatch can have an unknown delivery outcome.
            response = await session.call_tool(name, arguments=arguments)
            data = response.model_dump(mode='json', by_alias=True, exclude_none=True)
            if data.get('isError'):
                return {'ok': False, 'error': 'WhatsApp tool reported an error. Check ContextForge logs before retrying.'}
            payload = data.get('structuredContent')
            if payload is None:
                content = data.get('content', [])
                if len(content) == 1 and content[0].get('type') == 'text':
                    try:
                        payload = json.loads(content[0].get('text', ''))
                    except ValueError:
                        pass
            if isinstance(payload, dict) and payload.get('messages') and not payload.get('error'):
                return {'ok': True, 'message': 'Meta accepted the message. Delivery is not yet confirmed.'}
            return {'ok': False, 'error': 'Acceptance could not be verified. Check gateway logs before retrying; do not assume the message failed.'}


def reply(data, status=200):
    response = JsonResponse(data, status=status)
    response['Cache-Control'] = 'no-store'
    return response


@csrf_protect
def whatsapp_api(request):
    if not request.user.is_authenticated or not request.user.is_active:
        return reply({'ok': False, 'error': 'Authentication required.'}, 401)
    if request.method != 'POST':
        return reply({'ok': False, 'error': 'POST required.'}, 405)
    audit = None
    dispatched = False
    try:
        if len(request.body) > 8192:
            raise ValueError('Request is too large.')
        body = json.loads(request.body)
        if not isinstance(body, dict):
            raise ValueError('Expected a JSON object.')
        config = availability(request.user)
        recipient, token, mode = body.get('recipient'), body.get('result_id'), body.get('mode')
        if not config['tool'] or recipient not in config['recipients']:
            return reply({'ok': False, 'error': 'Recipient or WhatsApp tool is not authorized.'}, 403)
        if mode not in {'summary', 'full'} or not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
            raise ValueError('Choose a valid result and summary/full mode.')
        result = cache.get(f'agentic:whatsapp:result:{request.user.pk}:{token}')
        if not result:
            raise ValueError('Result expired. Run the question again before sending.')
        if not _can_replay_result(request.user, result):
            return reply({'ok': False, 'error': 'You no longer have access to this result.'}, 403)
        text = export_text(result, mode)
        if body.get('action') == 'preview':
            return reply({'ok': True, 'text': text, 'characters': len(text)})
        if body.get('action') != 'send' or body.get('confirmed') is not True:
            raise ValueError('Preview and confirm the message before sending.')
        digest = hashlib.sha256(f'{request.user.pk}:{token}:{recipient}:{mode}'.encode()).hexdigest()
        if not cache.add('agentic:whatsapp:send:' + digest, True, TTL):
            return reply({'ok': False, 'error': 'This message was already requested. Check its status before retrying.'}, 409)
        audit = AgentRunLog.objects.create(user=request.user, user_query=f'WhatsApp {mode} export',
            selected_tool=config['tool'], detected_intent='whatsapp_export', status='failed', risk_level='high',
            tool_input={'mode': mode, 'recipient_suffix': recipient[-4:], 'result_id': token},
            error_message='Dispatch pending; check gateway before retrying.')
        gateway = load_gateway_config()
        dispatched = True
        output = async_to_sync(asyncio.wait_for)(dispatch(gateway, config['tool'], recipient, text, request.user), gateway['request_timeout'])
        audit.status = 'success' if output.get('ok') else ('approval_required' if output.get('approval_required') else 'failed')
        audit.error_message = '' if output.get('ok') else output.get('error', output.get('answer', ''))
        audit.save(update_fields=['status', 'error_message'])
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
