"""Guided admin setup without shell commands or manually entered role JSON."""
import hashlib
import json

from asgiref.sync import async_to_sync
from django.apps import apps
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import Group
from django.core.cache import cache
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


@login_required
@ensure_csrf_cookie
def query_page(request):
    from .workspace import workspace_context
    from .whatsapp import availability
    response = render(request, "agentic_ai/ask.html", {"model": get_llm_model(), **workspace_context(request.user), "whatsapp": availability(request.user)})
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


@staff_member_required
@permission_required(SETUP_PERMISSIONS, raise_exception=True)
@ensure_csrf_cookie
@csrf_protect
def setup_page(request):
    cache_key = f"agentic:setup:{request.user.pk}"
    if request.method == "GET":
        from .whatsapp import configuration
        try:
            whatsapp = configuration()
        except (ValueError, TypeError):
            whatsapp = {"tool": get_setting("MCP_WHATSAPP_TOOL", ""), "recipients": []}
        response = render(request, "agentic_ai/setup.html", {
            "gateway_url": get_setting("MCP_GATEWAY_URL"), "model": get_llm_model(),
            "enabled": gateway_enabled(), **_pricing_context(),
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
        if action == "save_whatsapp":
            from .whatsapp import recipient_text
            name = body.get("tool", "")
            recipients = body.get("recipients", "")
            if not isinstance(name, str) or not isinstance(recipients, str) or len(name) > 150:
                raise ValueError
            name = name.strip()
            if name and not AgentTool.objects.filter(name=name, is_active=True).exists():
                return _reply({"ok": False, "error": "First discover and enable the exact WhatsApp tool name below."}, 400)
            numbers = recipient_text(recipients)
            _save_settings({"MCP_WHATSAPP_TOOL": name, "MCP_WHATSAPP_RECIPIENTS": json.dumps(numbers)})
            return _reply({"ok": True, "message": "WhatsApp settings saved. Refresh the workspace to load recipients."})
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
            config = load_gateway_config()
            tools = async_to_sync(discover_gateway)(config)
            known = {record.name: record for record in AgentTool.objects.filter(name__in=[t.name for t in tools])}
            catalog = []
            for tool in tools:
                record = known.get(tool.name)
                catalog.append({
                    "name": tool.name, "description": tool.description or "",
                    "display_name": record.display_name if record else "",
                    "category": record.category if record else "General",
                    "example_question": record.example_question if record else "",
                    "enabled": bool(record and record.is_active),
                    "roles": record.allowed_roles if record else [],
                    "requires_approval": record.requires_approval if record else not bool(tool.annotations and tool.annotations.readOnlyHint),
                })
            cache.set(cache_key, {"url": config["url"], "tools": catalog}, 300)
            return _reply({"ok": True, "tools": catalog, "roles": _roles(),
                           "message": f"Found {len(catalog)} tools. Choose tools and who can use them."})
        if action == "check_model":
            config = load_config()
            check = async_to_sync(probe_model)(config)
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
