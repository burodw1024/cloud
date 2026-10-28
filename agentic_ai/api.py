"""Authenticated, CSRF-protected entry point for agent execution."""
import hashlib
import json
import logging
import re
import time

from django.core.cache import cache
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect

from .mcp_gateway import MAX_QUERY_CHARS

logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 65536
RATE_LIMIT = 20
IDEMPOTENCY_TTL = 900


def execute_query(**kwargs):
    # Avoid loading local integrations merely to validate an HTTP request.
    from .orchestrator import run_agentic_query
    return run_agentic_query(**kwargs)


def _can_replay_result(user, result):
    """A cached report must obey current authorization, just like a new call."""
    tool_name = result.get("tool")
    if not tool_name:
        return True
    if result.get("plan", {}).get("intent") == "mcp_tool_call":
        from .models import AgentTool
        from .mcp_gateway import _allowed
        record = AgentTool.objects.filter(name=tool_name, is_active=True).first()
        return bool(record and _allowed(record, user))
    from .permissions import has_tool_permission
    from .tool_registry import TOOL_REGISTRY
    config = TOOL_REGISTRY.get(tool_name)
    return bool(config and has_tool_permission(user, config))


def _response(body, status=200):
    response = JsonResponse(body, status=status)
    response["Cache-Control"] = "no-store"
    if body.get("request_id"):
        response["X-Request-ID"] = body["request_id"]
    return response


@csrf_protect
def agentic_query_api(request):
    if request.method != "POST":
        response = _response({"ok": False, "error": "POST required"}, 405)
        response["Allow"] = "POST"
        return response
    if not getattr(request.user, "is_authenticated", False) or not request.user.is_active:
        return _response({"ok": False, "error": "Authentication required"}, 401)
    try:
        if len(request.body) > MAX_BODY_BYTES:
            raise RequestDataTooBig
        if request.content_type == "application/json":
            body = json.loads(request.body.decode("utf-8"))
        elif request.content_type in {"application/x-www-form-urlencoded", "multipart/form-data"}:
            body = request.POST
        else:
            return _response({"ok": False, "error": "Use application/json or a form request."}, 415)
    except RequestDataTooBig:
        return _response({"ok": False, "error": "Request body is too large."}, 413)
    except (ValueError, UnicodeError):
        return _response({"ok": False, "error": "Malformed JSON request."}, 400)
    if not hasattr(body, "get"):
        return _response({"ok": False, "error": "The request body must be an object."}, 400)
    query = body.get("message") or body.get("query") or ""
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        return _response({"ok": False, "error": f"message/query must be a nonempty string of at most {MAX_QUERY_CHARS} characters."}, 400)
    query = query.strip()
    key = request.headers.get("Idempotency-Key", "")
    if key and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        return _response({"ok": False, "error": "Invalid Idempotency-Key."}, 400)
    fingerprint = hashlib.sha256(query.encode()).hexdigest()
    cache_key = None
    try:
        if key:
            digest = hashlib.sha256(f"{request.user.pk}:{key}".encode()).hexdigest()
            cache_key = "agentic:idempotency:" + digest
            initial = {"fingerprint": fingerprint, "pending": True}
            if not cache.add(cache_key, initial, IDEMPOTENCY_TTL):
                previous = cache.get(cache_key)
                if not previous or previous["fingerprint"] != fingerprint:
                    return _response({"ok": False, "error": "Idempotency key conflict. Use a new key for a new query."}, 409)
                if previous.get("pending"):
                    return _response({"ok": False, "error": "This request is already running or its outcome is pending. Check the audit log before retrying."}, 409)
                if not _can_replay_result(request.user, previous["result"]):
                    return _response({"ok": False, "code": "permission_denied",
                                      "error": "This report is no longer available to your current roles."}, 403)
                return _response({**previous["result"], "replayed": True}, previous["status"])
        rate_key = f"agentic:rate:{request.user.pk}:{int(time.time()) // 60}"
        if cache.add(rate_key, 1, 120):
            count = 1
        else:
            count = cache.incr(rate_key)
        if count > RATE_LIMIT:
            if cache_key:
                cache.delete(cache_key)
            response = _response({"ok": False, "error": "Too many requests. Try again in one minute."}, 429)
            response["Retry-After"] = "60"
            return response
    except Exception:
        logger.error("Agent execution cache is unavailable")
        return _response({"ok": False, "error": "Request coordination is unavailable; no tool was executed."}, 503)
    result = execute_query(user=request.user, query=query, session_id=request.session.session_key)
    status = result.pop("http_status", 200)
    try:
        from .whatsapp import remember_result
        remember_result(request.user, result)
    except Exception:
        logger.warning("WhatsApp result snapshot unavailable")
    if cache_key:
        try:
            cache.set(cache_key, {"fingerprint": fingerprint, "result": result, "status": status}, IDEMPOTENCY_TTL)
        except Exception:
            # Keep the durable audit/result intact; never retry a dispatched tool here.
            logger.error("Agent idempotency result storage failed")
    return _response(result, status)
