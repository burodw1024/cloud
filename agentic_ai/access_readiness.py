"""Root-only discovery diagnostics; never dispatches tools or exposes credentials."""
import os

from asgiref.sync import async_to_sync
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from .loan_scope_backend import TOKEN_FIELD
from .models import AgentTool


def report_readiness(records, discovered, signing_ready, protected):
    schemas = {tool.name: tool.inputSchema for tool in discovered}
    results = []
    for record in records:
        if not (record.requires_loan_scope or record.loan_access_mode in {'FIXED', 'APP'} or record.name in protected):
            continue
        schema = schemas.get(record.name)
        if not record.is_active or (record.module_id and not record.module.is_active):
            code, message = 'disabled', 'Tool or business module is paused.'
        elif schema is None:
            code, message = 'not_discovered', 'Registered tool was not returned by the connected MCP server.'
        elif record.loan_access_mode in {'APP', 'UI'}:
            from .application_scope import schema_supported
            if schema_supported(schema):
                code, message = 'application_scope_detected', 'Application reporting contract detected. Django supplies the permitted branch list or one checked branch_code; verify Oracle filtering before aggregation in staging.'
            else:
                code, message = 'missing_application_scope_contract', 'The MCP schema must declare native branch_code and report_date strings without requiring loan_access_token. If app_branch_codes is declared, it must be a required array of strings.'
        elif record.loan_access_mode == 'FIXED':
            code, message = 'fixed_scope', 'Uses a fixed backend branch list; does not follow arbitrary per-user branch selections.'
        elif (schema.get('properties', {}).get(TOKEN_FIELD, {}).get('type') != 'string'
              or TOKEN_FIELD not in schema.get('required', [])):
            code, message = 'missing_token_contract', 'Backend update needed: MCP schema must require the string loan_access_token and forward it to the NiFi verifier.'
        elif not signing_ready:
            code, message = 'missing_signing_key', 'Django signing key is missing. Configure the same dedicated key in Django and the NiFi verifier.'
        else:
            code, message = 'schema_detected', 'Signed input schema and Django signing key detected. NiFi verification and SQL branch enforcement still require a staged report check.'
        results.append({'name': record.name, 'title': record.display_name or record.name,
                        'code': code, 'message': message})
    return results


@staff_member_required(login_url='/login/')
@require_POST
@csrf_protect
def access_readiness(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    from .mcp_gateway import load_gateway_config, discover_gateway, gateway_enabled
    from .loan_access import protected_tools
    if not gateway_enabled():
        body, status = {'ok': False, 'error': 'MCP gateway is disabled. Enable it in Integration settings.'}, 409
    else:
        try:
            config = load_gateway_config()
            config.update(timeout=min(config['timeout'], 15), request_timeout=25)
            discovered = async_to_sync(discover_gateway)(config)
            results = report_readiness(AgentTool.objects.select_related('module').order_by('name'), discovered,
                len(os.environ.get('LOAN_ACCESS_SIGNING_KEY', '').encode()) >= 32, protected_tools())
            body, status = {'ok': True, 'tools': results, 'report_executed': False}, 200
        except Exception:
            body, status = {'ok': False, 'error': 'MCP discovery failed. Check the gateway connection in Integration settings. No report was executed.'}, 503
    response = JsonResponse(body, status=status)
    response['Cache-Control'] = 'no-store'
    return response
