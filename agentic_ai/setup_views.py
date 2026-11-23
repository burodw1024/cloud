"""Guided admin setup without shell commands or manually entered role JSON."""
import hashlib
import json

from asgiref.sync import async_to_sync
from django.apps import apps
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie

from chat.dynamic_config import clear_setting_cache, get_setting, get_llm_model
from .mcp_gateway import (
    GatewayError, _failure, _http_url, discover_gateway, gateway_enabled,
    load_config, load_gateway_config, probe_model,
)
from .models import AgentTool

SETUP_PERMISSIONS = ("agentic_ai.add_agenttool", "agentic_ai.change_agenttool", "chat.change_systemsetting")


def _roles():
    return sorted(set(Group.objects.values_list("name", flat=True)) | {"admin", "staff"})


def _save_settings(values):
    model = apps.get_model("chat", "SystemSetting")
    with transaction.atomic():
        for key, value in values.items():
            model.objects.update_or_create(key=key, defaults={"value": value, "is_active": True})
    for key in values:
        transaction.on_commit(lambda key=key: clear_setting_cache(key), robust=True)


def _signature(config):
    return hashlib.sha256(json.dumps({key: config[key] for key in
        ("provider", "model", "model_url", "model_key")}, sort_keys=True).encode()).hexdigest()


def _reply(body, status=200):
    response = JsonResponse(body, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _probe_config(config):
    return {**config, "timeout": min(config.get("timeout", 10), 10),
            "request_timeout": min(config.get("request_timeout", 12), 12)}


def _integration_status(service, user):
    from . import bangla_summary, directory_sync, reporting_admin, reporting_config, whatsapp

    try:
        if service == "directory":
            directory_sync.configuration()
        elif service == "superset":
            config = reporting_config.current()
            if not all(config.get(key) for key in
                       ("URL", "USERNAME", "PASSWORD", "DATABASE_ID", "SNAPSHOT_DB")):
                return "Needs setup", "Complete the connection and report storage fields."
        elif service == "summary":
            bangla_summary.summary_config()
            return "Configured", "The summary endpoint is configured. Model invocation is checked when summarizing."
        elif service == "whatsapp":
            config = whatsapp.configuration()
            if not config["tool"] or not config["recipients"]:
                return "Needs setup", "Choose a WhatsApp tool and allowed recipients."
            status = whatsapp.settings_status(user)
            if not status["ready"]:
                return "Inactive", status["status"]
            tools = async_to_sync(discover_gateway)(_probe_config(load_gateway_config()))
            tool = next((tool for tool in tools if tool.name == config["tool"]), None)
            if tool is None:
                return "Down", "The configured WhatsApp tool is missing from the gateway."
            whatsapp.text_arguments(tool.inputSchema, config["recipients"][0], "Configuration check")
            return "Ready", "Gateway tool and text schema verified. Delivery has not been tested."
    except (ValueError, TypeError, GatewayError) as exc:
        if service != "whatsapp" or isinstance(exc, (ValueError, TypeError)):
            return "Needs setup", "Check the saved integration settings."
        return "Down", "The gateway check failed."
    except Exception:
        return "Down", "The connection check failed."

    try:
        if service == "directory":
            directory_sync.fetch_snapshot()
            return "Up", "The API returned a valid directory snapshot."
        reporting_admin.check_connection()
        return "Up", "Superset login, database access and snapshot storage verified."
    except Exception:
        return "Down", "The connection check failed. Open the integration settings for details."


@login_required
@ensure_csrf_cookie
def query_page(request):
    from .workspace import workspace_context
    from .whatsapp import settings_status
    from .loan_access import own_access_summary
    from .reporting_config import workspace_settings
    from .share_channels import CHANNELS, describe
    from django.urls import reverse
    workspace = workspace_context(request.user, request.GET.get('module', ''))
    whatsapp_status = settings_status(request.user)
    for capability in workspace['capabilities']:
        if capability['name'] == whatsapp_status['tool']:
            capability['service_url'] = reverse('agentic_ai:services') + '#whatsapp-service-title'
    question = ''
    if request.GET.get('reuse'):
        from uuid import UUID
        from .api import _can_replay_result
        from .models import ReportRun
        try:
            run_id = UUID(request.GET['reuse'])
        except ValueError:
            run_id = None
        if run_id:
            run = ReportRun.objects.filter(pk=run_id, user=request.user).first()
            if run and _can_replay_result(request.user, run.source):
                question = run.question
    channels = [{'key': 'whatsapp', 'label': 'WhatsApp', **whatsapp_status}]
    channels.extend(describe(channel, request.user) for channel in CHANNELS)
    response = render(request, "agentic_ai/ask.html", {**workspace, "whatsapp": whatsapp_status,
                      "share_channels": [channel for channel in channels if channel['ready']], "draft_question": question,
                      'reporting':workspace_settings(request.user), 'own_loan_access': own_access_summary(request.user) if workspace.get('has_loan_capabilities') else None})
    response["Cache-Control"] = "no-store"
    return response


def _pricing_context():
    from .usage_meter import parse_prices
    provider = get_setting("MCP_MODEL_PROVIDER", "ollama").lower()
    model = get_setting("MCP_MODEL") or (get_llm_model() if provider == "ollama" else "")
    prices = parse_prices(get_setting("MCP_MODEL_PRICING", "{}"))
    price = prices.get(provider + ":" + model, {})
    return {"pricing_provider": provider, "pricing_model": model,
            "model_price": price if isinstance(price, dict) else {}}


@staff_member_required(login_url='/login/')
@permission_required(SETUP_PERMISSIONS, raise_exception=True)
@ensure_csrf_cookie
@csrf_protect
def setup_page(request):
    from . import directory_config
    cache_key = f"agentic:setup:{request.user.pk}"
    if request.method == "GET":
        from .whatsapp import configuration
        from .share_channels import CHANNELS, describe
        try:
            whatsapp = configuration()
        except (ValueError, TypeError):
            whatsapp = {"tool": get_setting("MCP_WHATSAPP_TOOL", ""), "recipients": []}
        response = render(request, "agentic_ai/setup.html", {
            "directory": directory_config.public_status(), "directory_default_url": directory_config.DEFAULT_URL,
            "gateway_url": get_setting("MCP_GATEWAY_URL"), "model": get_llm_model(),
            "enabled": gateway_enabled(), **_pricing_context(),
            "summary_model": get_setting("MCP_SUMMARY_MODEL", "openrouter/auto"),
            "summary_base": get_setting("MCP_SUMMARY_BASE_URL"),
            "share_channels": [{**detail, 'recipient_text': '\n'.join(detail['recipients'])}
                               for detail in (describe(channel) for channel in CHANNELS)],
            "whatsapp_tool": whatsapp["tool"],
            "whatsapp_recipients": "\n".join(whatsapp["recipients"]),
        })
        response["Cache-Control"] = "no-store"
        return response
    if request.method != "POST":
        return _reply({"ok": False, "error": "POST required"}, 405)
    try:
        if len(request.body) > 65536:
            return _reply({"ok": False, "error": "Request is too large."}, 413)
        body = json.loads(request.body.decode())
        if not isinstance(body, dict):
            raise ValueError
        action = body.get("action")
        if action == "integration_status":
            service = body.get("service")
            if service not in ("directory", "superset", "summary", "whatsapp"):
                return _reply({"ok": False, "error": "Unknown integration."}, 400)
            state, detail = _integration_status(service, request.user)
            return _reply({"ok": True, "state": state, "detail": detail})
        if action == "save_directory":
            try:
                version = directory_config.save(request.user, body.get('url'), body.get('allow_http'),
                                                body.get('token', ''), body.get('version'))
            except ValueError as exc:
                return _reply({'ok': False, 'error': str(exc)}, 400)
            return _reply({'ok': True, 'version': version, 'message': 'Directory API settings saved. The token is encrypted. You can check the saved connection or fetch a directory sync preview.'})
        if action == "check_directory":
            from .directory_sync import fetch_snapshot
            try:
                rows = fetch_snapshot()
            except ValueError as exc:
                return _reply({'ok': False, 'error': str(exc)}, 400)
            return _reply({'ok': True, 'message': f"Directory connection verified: {len(rows)} branches across {len({row['ZONECODE'] for row in rows})} zones. No directory entries or user permissions were changed."})
        if action == "check_whatsapp":
            from .whatsapp import configuration, settings_status, text_arguments
            status = settings_status(request.user)
            if not status['ready']:
                return _reply({"ok": False, "error": status['status']}, 400)
            config = configuration()
            tools = async_to_sync(discover_gateway)(load_gateway_config())
            tool = next((t for t in tools if t.name == config['tool']), None)
            if tool is None:
                return _reply({"ok": False, "error": "Configured tool is absent from the connected gateway. Discover tools and check the exact name."}, 400)
            try:
                text_arguments(tool.inputSchema, config['recipients'][0], 'Configuration check', tool.name)
            except ValueError as exc:
                return _reply({'ok': False, 'error': str(exc)}, 400)
            return _reply({"ok": True, "message": "Gateway tool and text input schema checked. No message sent. Provider credentials, request-body mapping, and delivery remain unverified."})
        if action == "save_summary":
            from urllib.parse import urlsplit
            base, model = body.get("base", ""), body.get("model", "")
            if not isinstance(base, str) or not isinstance(model, str) or not model.strip() or len(model) > 200:
                raise ValueError
            if base.strip():
                parsed = urlsplit(_http_url(base.strip(), "summary base"))
                gateway = urlsplit(_http_url(get_setting("MCP_GATEWAY_URL"), "gateway"))
                if (parsed.scheme, parsed.netloc) != (gateway.scheme, gateway.netloc):
                    return _reply({"ok": False, "error": "Use the same ContextForge origin as the gateway connection."}, 400)
            _save_settings({"MCP_SUMMARY_BASE_URL": base.strip(), "MCP_SUMMARY_MODEL": model.strip()})
            return _reply({"ok": True, "message": "Bangla summary settings saved. The gateway token is reused; its role needs llm.invoke."})
        if action in {'save_report_channel', 'check_report_channel'}:
            from .share_channels import CHANNELS, delivery_arguments, describe, recipient_list, setting_keys
            channel = body.get('channel')
            if not isinstance(channel, str) or channel not in CHANNELS:
                raise ValueError
            if action == 'save_report_channel':
                name, recipients = body.get('tool', ''), body.get('recipients', '')
                if not isinstance(name, str) or len(name) > 150 or not isinstance(recipients, str):
                    raise ValueError
                try:
                    values = recipient_list(channel, [line.strip() for line in recipients.splitlines() if line.strip()])
                except ValueError as exc:
                    return _reply({'ok': False, 'error': str(exc)}, 400)
                tool_key, recipients_key = setting_keys(channel)
                _save_settings({tool_key: name.strip(), recipients_key: json.dumps(values)})
                return _reply({'ok': True, 'message': f'{CHANNELS[channel]} settings saved.'})
            status = describe(channel, request.user)
            if not status['ready']:
                return _reply({'ok': False, 'error': status['status']}, 400)
            tools = async_to_sync(discover_gateway)(load_gateway_config())
            tool = next((item for item in tools if item.name == status['tool']), None)
            if tool is None:
                return _reply({'ok': False, 'error': 'The configured delivery tool is absent from the gateway.'}, 400)
            try:
                delivery_arguments(channel, tool.inputSchema, status['recipients'][0], 'Configuration check')
            except ValueError as exc:
                return _reply({'ok': False, 'error': str(exc)}, 400)
            return _reply({'ok': True, 'message': 'Gateway tool and message schema verified. No message was sent.'})
        if action == "save_whatsapp":
            from .whatsapp import recipient_text
            name = body.get("tool", "")
            recipients = body.get("recipients", "")
            if not isinstance(name, str) or not isinstance(recipients, str) or len(name) > 150:
                raise ValueError
            name = name.strip()
            numbers = recipient_text(recipients)
            _save_settings({"MCP_WHATSAPP_TOOL": name, "MCP_WHATSAPP_RECIPIENTS": json.dumps(numbers)})
            return _reply({"ok": True, "message": "WhatsApp settings saved. Click Check WhatsApp settings, then refresh the workspace. If needed, discover and enable the tool below."})
        if action == "save_pricing":
            from .usage_meter import parse_prices, validate_price
            current = _pricing_context()
            if (body.get("provider") != current["pricing_provider"]
                    or body.get("model") != current["pricing_model"] or not current["pricing_model"]):
                return _reply({"ok": False, "error": "The model changed. Refresh before saving its price."}, 409)
            prices = parse_prices(get_setting("MCP_MODEL_PRICING", "{}"))
            key = current["pricing_provider"] + ":" + current["pricing_model"]
            inputs, outputs = body.get("input_rate"), body.get("output_rate")
            if inputs == "" and outputs == "":
                prices.pop(key, None)
            else:
                price = {"currency": body.get("currency", "USD"),
                         "input_per_million": inputs, "output_per_million": outputs}
                if body.get("cached_rate") not in (None, ""):
                    price["cached_input_per_million"] = body["cached_rate"]
                prices[key] = validate_price(price)
            _save_settings({"MCP_MODEL_PRICING": json.dumps(prices)})
            return _reply({"ok": True, "message": "Model pricing saved. Future requests use these estimated rates; historical requests are unchanged."})
        if action == "save_connection":
            url = body.get("url")
            token = body.get("token", "")
            if not isinstance(url, str) or not isinstance(token, str) or len(token) > 4096:
                raise ValueError
            values = {"MCP_GATEWAY_URL": _http_url(url.strip(), "gateway"), "MCP_GATEWAY_ENABLED": "false"}
            if token.strip():
                values["MCP_GATEWAY_TOKEN"] = token.strip()
            _save_settings(values)
            cache.delete(cache_key)
            return _reply({"ok": True, "message": "Connection saved. Tools are paused until setup is complete. Next, find the available tools."})
        if action == "discover":
            from .loan_access import protected_tools
            from .loan_scope_backend import TOKEN_FIELD
            from .application_scope import tool_supported
            mandatory_scope_tools = protected_tools()
            config = load_gateway_config()
            tools = async_to_sync(discover_gateway)(_probe_config(config) if body.get("background") is True else config)
            known = {record.name: record for record in AgentTool.objects.filter(name__in=[t.name for t in tools]).select_related("module")}
            catalog = []
            for tool in tools:
                record = known.get(tool.name)
                supports_application_scope = tool_supported(tool)
                default_mode = "UI" if tool.name in mandatory_scope_tools and supports_application_scope else "SIGNED"
                catalog.append({
                    "name": tool.name, "description": tool.description or "",
                    "display_name": record.display_name if record else "",
                    "category": record.category if record else "General",
                    "example_question": record.example_question if record else "",
                    "enabled": bool(record and record.is_active),
                    "module_paused": bool(record and record.module_id and not record.module.is_active),
                    "roles": record.allowed_roles if record else [],
                    "requires_approval": record.requires_approval if record else not bool(tool.annotations and tool.annotations.readOnlyHint),
                    "requires_loan_scope": tool.name in mandatory_scope_tools or bool(record and record.requires_loan_scope),
                    "loan_scope_mandatory": tool.name in mandatory_scope_tools,
                    "loan_access_mode": record.loan_access_mode if record else default_mode,
                    "fixed_branch_codes": record.fixed_branch_codes if record else [],
                    "application_scope_supported": supports_application_scope,
                    "loan_scope_contract": tool.inputSchema.get('properties', {}).get(TOKEN_FIELD, {}).get('type') == 'string' and TOKEN_FIELD in tool.inputSchema.get('required', []),
                })
            cache.set(cache_key, {"url": config["url"], "tools": catalog}, 300)
            missing = list(AgentTool.objects.filter(is_active=True).exclude(name__in=known).order_by("name").values_list("name", flat=True))
            return _reply({"ok": True, "tools": catalog, "roles": _roles(),
                           "missing": missing, "gateway_enabled": gateway_enabled(),
                           "message": f"Found {len(catalog)} tools. Choose tools and who can use them."})
        if action == "check_model":
            cache.delete(cache_key + ":model")
            config = load_config()
            check = async_to_sync(probe_model)(_probe_config(config) if body.get("background") is True else config)
            cache.set(cache_key + ":model", _signature(config), 300)
            return _reply({"ok": True, "message": f"Your project model ({check['model']}) can choose tools."})
        if action == "save_tools":
            snapshot = cache.get(cache_key)
            config = load_config()
            if not snapshot or snapshot["url"] != config["url"]:
                return _reply({"ok": False, "error": "Find the tools again; the connection or tool list has changed."}, 409)
            if cache.get(cache_key + ":model") != _signature(config):
                return _reply({"ok": False, "error": "Check the project model before enabling tools."}, 409)
            items = body.get("tools")
            if not isinstance(items, list) or len(items) > 64:
                raise ValueError
            known = {tool["name"]: tool for tool in snapshot["tools"]}
            roles = set(_roles())
            names = set()
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise ValueError
                name = item["name"]
                selected = item.get("roles")
                if (name not in known or name in names or not isinstance(selected, list)
                    or any(not isinstance(role, str) or role not in roles for role in selected)
                    or not isinstance(item.get("enabled"), bool)
                    or not isinstance(item.get("requires_approval"), bool)):
                    raise ValueError
                for field, maximum in (("display_name", 150), ("category", 80), ("example_question", 2000)):
                    value = item.get(field)
                    if field in item and (not isinstance(value, str) or len(value) > maximum):
                        raise ValueError
                if 'loan_access_mode' in item and item['loan_access_mode'] not in ('UI', 'APP', 'SIGNED', 'FIXED'):
                    raise ValueError
                if (item.get('enabled') and item.get('loan_access_mode') in {'UI', 'APP'}
                        and not known[name].get('application_scope_supported')):
                    return _reply({'ok': False, 'error': f'{name} needs a supported loan or savings schema with an exact branch_code filter for User Access permissions. Its current schema is incompatible; no settings were saved.'}, 400)
                if 'fixed_branch_codes' in item:
                    candidate = AgentTool(name=name, loan_access_mode=item.get('loan_access_mode', 'SIGNED'),
                                          fixed_branch_codes=item['fixed_branch_codes'])
                    try:
                        candidate.clean()
                    except ValidationError:
                        return _reply({"ok": False, "error": "Enter distinct exact branch codes for the fixed NiFi scope."}, 400)
                if item.get('loan_access_mode') == 'FIXED' and not item.get('fixed_branch_codes'):
                    return _reply({"ok": False, "error": "Fixed mode requires the full NiFi branch allowlist."}, 400)
                if 'requires_loan_scope' in item and not isinstance(item['requires_loan_scope'], bool):
                    raise ValueError
                if item["enabled"] and not selected:
                    return _reply({"ok": False, "error": f"Choose at least one role for {name}."}, 400)
                names.add(name)
            if not any(item["enabled"] for item in items):
                return _reply({"ok": False, "error": "Choose at least one tool to enable."}, 400)
            with transaction.atomic():
                for item in items:
                    record, _ = AgentTool.objects.get_or_create(name=item["name"])
                    record.description = known[item["name"]]["description"]
                    record.is_active = item["enabled"]
                    record.allowed_roles = item["roles"]
                    record.requires_approval = item["requires_approval"]
                    for field in ('loan_access_mode', 'fixed_branch_codes'):
                        if field in item:
                            setattr(record, field, item[field])
                    if record.loan_access_mode == 'FIXED':
                        record.requires_loan_scope = True
                    if 'requires_loan_scope' in item:
                        record.requires_loan_scope = item['requires_loan_scope'] or record.loan_access_mode in {'UI', 'APP', 'FIXED'}
                    for field in ("display_name", "category", "example_question"):
                        if field in item:
                            setattr(record, field, item[field].strip() or ("General" if field == "category" else ""))
                    record.full_clean()
                    record.save()
                _save_settings({"MCP_GATEWAY_ENABLED": "true"})
            return _reply({"ok": True, "message": "Capabilities are enabled. You can now ask a question in your workspace."})
        return _reply({"ok": False, "error": "Choose a valid setup action."}, 400)
    except (ValueError, UnicodeError, TypeError):
        return _reply({"ok": False, "error": "Check the submitted settings and try again."}, 400)
    except Exception as exc:
        code, message, status = _failure(exc)
        return _reply({"ok": False, "code": code, "error": message}, status)
