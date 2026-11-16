"""Discoverable report delivery/reporting services; page loads never call them."""
import asyncio
from asgiref.sync import async_to_sync
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect

from . import whatsapp
from .dashboard_studio import permitted, response
from .reporting_config import workspace_settings
from .superset_publisher import configuration, PublishError


@login_required
def services_page(request):
    if request.method != 'GET':
        return response({'ok': False, 'error': 'GET required.'}, 405)
    if not request.user.is_active:
        return response({'ok': False, 'error': 'Active account required.'}, 403)
    reporting = workspace_settings(request.user)
    status = 'Ask an administrator to connect Superset in Reporting setup.'
    ready = False
    if reporting['configured']:
        status = 'Superset is connected. Publishing requires a staff account with dashboard publishing permission.'
    if permitted(request.user):
        try:
            configuration(request.user)
            ready = True
            status = 'Publishing settings are configured. Run a report to generate a preview; service connectivity is checked separately.'
        except PublishError as exc:
            status = str(exc)
    result = render(request, 'agentic_ai/services.html', {
        'whatsapp': whatsapp.settings_status(request.user), 'reporting': reporting,
        'superset_status': status, 'superset_ready': ready})
    result['Cache-Control'] = 'no-store'
    return result


@csrf_protect
def check_whatsapp(request):
    if not request.user.is_authenticated or not request.user.is_active:
        return response({'ok': False, 'error': 'Authentication required.'}, 401)
    if request.method != 'POST':
        return response({'ok': False, 'error': 'POST required.'}, 405)
    status = whatsapp.settings_status(request.user)
    if not status['ready']:
        return response({'ok': False, 'error': status['status']}, 403)
    if not cache.add(f'agentic:services:whatsapp-check:{request.user.pk}', True, 30):
        return response({'ok': False, 'error': 'Please wait 30 seconds before checking again.'}, 429)
    try:
        from .mcp_gateway import discover_gateway
        tools = async_to_sync(asyncio.wait_for)(discover_gateway(whatsapp.load_gateway_config()), 15)
        tool = next((tool for tool in tools if tool.name == status['tool']), None)
        if tool is None:
            raise ValueError('The configured WhatsApp tool was not found. Ask an administrator to discover tools and check its exact name.')
        whatsapp.text_arguments(tool.inputSchema, status['recipients'][0], 'Configuration check')
        return response({'ok': True, 'message': 'Gateway connection and text-message schema checked. No message was sent. Meta acceptance and delivery require a real, explicitly confirmed send.'})
    except ValueError as exc:
        return response({'ok': False, 'error': str(exc)}, 400)
    except Exception:
        return response({'ok': False, 'error': 'The WhatsApp gateway check could not complete. Ask an administrator to check connection, credentials and TLS settings. No message was sent.'}, 502)
