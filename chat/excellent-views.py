# chat/views.py  (FULL FILE - copy/paste)
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Union

import requests
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .views_hr_router import hr_ask
from .views_policy import policy_ask
from .views_core import core_ask
from .upload import require_staff_json, handle_upload_request
from .views_public import public_notice_search

JsonT = Union[Dict[str, Any], List[Any]]

# =========================================================
# Route-group RBAC
# Groups must be exactly: cashflow hr core policy
# =========================================================
ROUTE_GROUPS: Set[str] = {"cashflow", "hr", "core", "policy"}

# If user belongs to multiple groups, first match wins
ROUTE_PRIORITY: List[str] = ["cashflow", "core", "hr", "policy"]

ENFORCE_STAFF_ONLY = (os.getenv("ENFORCE_STAFF_ONLY", "1") or "1").lower() not in ("0", "false", "no")

# Anonymous toggles
ALLOW_ANON_CHAT = (os.getenv("ALLOW_ANON_CHAT", "1") or "1").lower() not in ("0", "false", "no")
ALLOW_ANON_PUBLIC = (os.getenv("ALLOW_ANON_PUBLIC", "1") or "1").lower() not in ("0", "false", "no")


def _user_group_names(user) -> Set[str]:
    try:
        if not user or not user.is_authenticated:
            return set()
        return set(user.groups.values_list("name", flat=True))
    except Exception:
        return set()


def _deny_auth(request) -> Optional[JsonResponse]:
    user = getattr(request, "user", None)

    if not user or not user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

    if getattr(user, "is_superuser", False):
        return None

    if ENFORCE_STAFF_ONLY and not getattr(user, "is_staff", False):
        return JsonResponse(
            {"ok": False, "error": "Staff access required.", "hint": "Set is_staff=True or set ENFORCE_STAFF_ONLY=0"},
            status=403,
        )

    return None


def _first_route_group(user) -> Optional[str]:
    if not user or not user.is_authenticated:
        return None
    if getattr(user, "is_superuser", False):
        # superuser can go anywhere; keep priority behavior
        return ROUTE_PRIORITY[0] if ROUTE_PRIORITY else "policy"

    groups = _user_group_names(user)
    for g in ROUTE_PRIORITY:
        if g in groups:
            return g
    return None


def _json_body(request) -> Dict[str, Any]:
    try:
        raw = (request.body or b"{}").decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _clean(v: Any) -> str:
    return ("" if v is None else str(v)).strip()


# =========================================================
# Pages
# =========================================================
def index(request):
    return render(request, "chat/index.html")


# =========================================================
# Chat (smalltalk only)
# =========================================================
def _env(name: str, default: str) -> str:
    return (os.getenv(name, default) or "").strip()


def _env_float(name: str, default: str) -> float:
    try:
        return float(_env(name, default))
    except Exception:
        return float(default)


@dataclass(frozen=True)
class Cfg:
    OLLAMA_URL: str = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    GEN_MODEL: str = _env("GEN_MODEL", "aya-23-8b")
    GEN_TIMEOUT: float = _env_float("GEN_TIMEOUT", "600")


CFG = Cfg()

SYSTEM_PROMPT_CHAT = _env(
    "SYSTEM_PROMPT_CHAT",
    "You are a helpful assistant. If you are unsure, say you are unsure.",
)


def _http_json(method: str, url: str, *, timeout: float, json_body: Optional[dict] = None) -> JsonT:
    r = requests.request(method, url, json=json_body, timeout=timeout, headers={"Accept": "application/json"})
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:600]}")
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Invalid JSON from {url}: {e}. Body={r.text[:600]}")


def _ollama_chat(system_prompt: str, user_prompt: str) -> str:
    data = _http_json(
        "POST",
        f"{CFG.OLLAMA_URL}/api/chat",
        timeout=CFG.GEN_TIMEOUT,
        json_body={
            "model": CFG.GEN_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.2, "top_p": 0.85, "repeat_penalty": 1.2, "num_ctx": 4096, "num_predict": 220},
        },
    )
    if not isinstance(data, dict):
        return ""
    return _clean((data.get("message") or {}).get("content"))


GREETINGS = ["hi", "hello", "hey", "assalamualaikum", "as-salamu alaykum", "হ্যালো", "সালাম", "আসসালামু আলাইকুম"]


def _is_smalltalk(q: str) -> bool:
    qn = (q or "").strip().lower()
    if not qn:
        return False
    if qn in GREETINGS:
        return True
    if any(qn.startswith(g + " ") for g in GREETINGS):
        return True
    if qn in {"thanks", "thank you", "ok", "okay", "tq", "ধন্যবাদ"}:
        return True
    return False


# Notice detection (safer)
NOTICE_HINTS = ["notice", "circular", "memo", "বিজ্ঞপ্তি", "নোটিশ", "সার্কুলার", "মেমো"]


def _is_notice(q: str) -> bool:
    ql = (q or "").lower()
    return any(k in ql for k in NOTICE_HINTS)


@csrf_exempt
@require_POST
def api_chat(request):
    user = getattr(request, "user", None)
    if (not user or not user.is_authenticated) and not ALLOW_ANON_CHAT:
        return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

    body = _json_body(request)
    msg = _clean(body.get("message") or body.get("question"))
    if not msg:
        return JsonResponse({"ok": False, "error": "message is required"}, status=400)

    history = body.get("history") or []
    parts: List[str] = []
    for m in history[-6:]:
        role = _clean(m.get("role"))
        content = _clean(m.get("content"))
        if role and content:
            parts.append(f"{role.upper()}: {content}")
    parts.append(f"USER: {msg}")
    parts.append("ASSISTANT:")

    try:
        answer = _ollama_chat(SYSTEM_PROMPT_CHAT, "\n".join(parts))
        return JsonResponse({"ok": True, "answer": answer})
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Chat backend error: {e}"}, status=502)


# =========================================================
# AUTO ROUTER (GROUP-BASED)
# =========================================================
@csrf_exempt
@require_POST
def auto_ask(request):
    """
    Rule:
      - If user belongs to group cashflow/hr/core/policy -> route to that view
      - If no route group matched -> default policy
    """
    body = _json_body(request)
    question = _clean(body.get("question") or body.get("message"))
    if not question:
        return JsonResponse({"ok": False, "error": "question is required"}, status=400)

    # smalltalk -> chat (optional)
    if _is_smalltalk(question):
        request._body = json.dumps({"message": question, "history": body.get("history") or []}, ensure_ascii=False).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return api_chat(request)

    # public notice -> public endpoint (optional)
    if _is_notice(question):
        if (not getattr(request, "user", None) or not request.user.is_authenticated) and not ALLOW_ANON_PUBLIC:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)
        request._body = json.dumps({"query": question, "top_k": body.get("top_k")}, ensure_ascii=False).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return public_notice_search(request)

    # secured routes require auth
    deny = _deny_auth(request)
    if deny:
        return deny

    route = _first_route_group(request.user)
    if route not in ROUTE_GROUPS:
        route = "policy"

    # Route by group
    if route == "cashflow":
        from .views_cashflow_router import cashflow_query  # lazy import
        request._body = json.dumps({"q": question}, ensure_ascii=False).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        resp = cashflow_query(request)
        # add routing debug (safe)
        try:
            data = json.loads(resp.content.decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                data.setdefault("routed_to", "cashflow")
                return JsonResponse(data, status=resp.status_code)
        except Exception:
            pass
        return resp

    if route == "core":
        request._body = json.dumps({"question": question, "top_k": body.get("top_k")}, ensure_ascii=False).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        resp = core_ask(request)
        try:
            data = json.loads(resp.content.decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                data.setdefault("routed_to", "core")
                return JsonResponse(data, status=resp.status_code)
        except Exception:
            pass
        return resp

    if route == "hr":
        request._body = json.dumps({"question": question, "explain": bool(body.get("explain", False))}, ensure_ascii=False).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        resp = hr_ask(request)
        try:
            data = json.loads(resp.content.decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                data.setdefault("routed_to", "hr")
                return JsonResponse(data, status=resp.status_code)
        except Exception:
            pass
        return resp

    # default policy
    request._body = json.dumps(
        {"question": question, "top_k": body.get("top_k"), "collection": body.get("collection")},
        ensure_ascii=False,
    ).encode("utf-8")
    request.META["CONTENT_TYPE"] = "application/json"
    resp = policy_ask(request)
    try:
        data = json.loads(resp.content.decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            data.setdefault("routed_to", "policy")
            return JsonResponse(data, status=resp.status_code)
    except Exception:
        pass
    return resp


@csrf_exempt
def upload_jsonl(request):
    auth_err = require_staff_json(request)
    if auth_err:
        return auth_err
    return handle_upload_request(request)
