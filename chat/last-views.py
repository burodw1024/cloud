# chat/views.py  (COMPLETE FILE - routing + general chat + workflow integration)
# NOTE:
# - This file handles general chat + routing + workflow interception.
# - Uploaded document Q&A / summarization is handled in chat/views_upload_doc.py (separate endpoint /api/doc/ask).

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

from .models import ChatSession, ChatMessage
from .memory_utils import get_short_memory
from .qdrant_memory_store import search_long_memory, save_long_term_memory

from .views_hr_router import hr_ask
from .views_policy import policy_ask
from .views_core import core_ask
from .views_public import public_notice_search

# =========================
# Workflow imports
# =========================
from workflows.models import WorkflowSession
from workflows.services.repository import get_active_definition, get_active_workflow
from workflows.services.intent_detector import detect_intent
from workflows.services.command_parser import (
    parse_change_command,
    is_resume_command,
    is_cancel_command,
    is_handoff_request,
)
from workflows.services.engine import WorkflowEngine
from workflows.services.permissions import user_can_start_workflow

JsonT = Union[Dict[str, Any], List[Any]]

# =========================================================
# Route-group RBAC
# =========================================================
ROUTE_GROUPS: Set[str] = {"general", "cashflow", "hr", "core", "policy"}
ROUTE_PRIORITY: List[str] = ["general", "cashflow", "core", "hr", "policy"]

ENFORCE_STAFF_ONLY = (os.getenv("ENFORCE_STAFF_ONLY", "1") or "1").lower() not in ("0", "false", "no")
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
        return ROUTE_PRIORITY[0] if ROUTE_PRIORITY else "general"

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
# Normal Chat (general)
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

    STM_LIMIT: int = int(_env("STM_LIMIT", "10") or "10")
    LTM_TOP_K: int = int(_env("LTM_TOP_K", "5") or "5")


CFG = Cfg()

SYSTEM_PROMPT_CHAT = _env(
    "SYSTEM_PROMPT_CHAT",
    """You are a helpful assistant.

Rules:
- Answer directly and clearly.
- If unsure, say you are unsure.
- Do not invent facts.
""",
)


def _http_json(method: str, url: str, *, timeout: float, json_body: Optional[dict] = None) -> JsonT:
    r = requests.request(method, url, json=json_body, timeout=timeout, headers={"Accept": "application/json"})
    if not r.ok:
        raise RuntimeError("HTTP %s from %s: %s" % (r.status_code, url, (r.text or "")[:600]))
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError("Invalid JSON from %s: %s. Body=%s" % (url, e, (r.text or "")[:600]))


def _ollama_chat_messages(messages: List[Dict[str, str]]) -> str:
    data = _http_json(
        "POST",
        f"{CFG.OLLAMA_URL}/api/chat",
        timeout=CFG.GEN_TIMEOUT,
        json_body={
            "model": CFG.GEN_MODEL,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.2,
                "top_p": 0.85,
                "repeat_penalty": 1.2,
                "num_ctx": 4096,
                "num_predict": 220,
            },
        },
    )
    if not isinstance(data, dict):
        return ""
    return _clean((data.get("message") or {}).get("content"))


def _should_save_long_memory(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    block_terms = ["salary", "tk", "taka", "amount", "balance", "loan", "nid", "passport", "employee id", "pin", "otp"]
    if any(b in t for b in block_terms):
        return False
    triggers = ["remember", "from now on", "i prefer", "always", "important", "call me", "my name is"]
    return any(k in t for k in triggers)


# =========================================================
# Workflow integration
# =========================================================
def _try_workflow(request, session_obj, msg: str) -> Optional[Dict[str, Any]]:
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return None

    if not session_obj:
        return None

    session_id_str = str(session_obj.id)

    active = get_active_workflow(user, session_id_str)

    if active:
        engine = WorkflowEngine(active)

        if is_resume_command(msg):
            return {
                "handled": True,
                "answer": engine.resume(),
                "workflow_active": True,
                "workflow_intent": active.intent_name,
            }

        if is_cancel_command(msg):
            return {
                "handled": True,
                "answer": engine.cancel(),
                "workflow_active": False,
                "workflow_intent": active.intent_name,
            }

        if is_handoff_request(msg):
            return {
                "handled": True,
                "answer": engine.request_handoff(msg),
                "workflow_active": False,
                "workflow_intent": active.intent_name,
                "handoff": True,
            }

        change_field = parse_change_command(msg)
        if change_field:
            return {
                "handled": True,
                "answer": engine.change_answer(change_field),
                "workflow_active": True,
                "workflow_intent": active.intent_name,
            }

        result = engine.process_text(msg)
        return {
            "handled": True,
            "answer": result["reply"],
            "workflow_active": not result["completed"],
            "workflow_intent": active.intent_name,
        }

    intent = detect_intent(msg)
    if not intent:
        return None

    definition = get_active_definition(intent)
    if not definition:
        return None

    if not user_can_start_workflow(user, definition):
        return {
            "handled": True,
            "answer": "You do not have permission to use this service.",
            "workflow_active": False,
            "workflow_intent": intent,
        }

    wf = WorkflowSession.objects.create(
        user=user,
        session_id=session_id_str,
        intent_name=intent,
        definition=definition,
        status="active",
        collected_data={},
    )
    engine = WorkflowEngine(wf)

    return {
        "handled": True,
        "answer": engine.start(),
        "workflow_active": True,
        "workflow_intent": intent,
    }


@csrf_exempt
@require_POST
def api_chat(request):
    """
    General chat endpoint.
    This endpoint does NOT handle uploaded docs.
    Uploaded docs are handled by /api/doc/ask in views_upload_doc.py
    """
    user = getattr(request, "user", None)
    is_auth = bool(user and user.is_authenticated)

    if (not is_auth) and not ALLOW_ANON_CHAT:
        return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

    body = _json_body(request)
    msg = _clean(body.get("message") or body.get("question"))
    if not msg:
        return JsonResponse({"ok": False, "error": "message is required"}, status=400)

    session = None
    session_id = body.get("session_id")

    if is_auth:
        if session_id is not None:
            session = ChatSession.objects.filter(id=session_id, user=user).first()
        if not session:
            session = ChatSession.objects.create(user=user, title="Chat")

        # ============================================
        # WORKFLOW CHECK FIRST
        # ============================================
        workflow_result = _try_workflow(request, session, msg)
        if workflow_result and workflow_result.get("handled"):
            try:
                ChatMessage.objects.create(session=session, role="user", content=msg)
                ChatMessage.objects.create(session=session, role="assistant", content=workflow_result["answer"])
            except Exception:
                pass

            return JsonResponse(
                {
                    "ok": True,
                    "answer": workflow_result["answer"],
                    "session_id": session.id,
                    "workflow_active": workflow_result.get("workflow_active", False),
                    "workflow_intent": workflow_result.get("workflow_intent"),
                    "handoff": workflow_result.get("handoff", False),
                }
            )

    ltm: List[str] = []
    stm_msgs: List[Dict[str, str]] = []

    if is_auth and session:
        try:
            ltm = search_long_memory(user.id, msg, limit=CFG.LTM_TOP_K) or []
        except Exception:
            ltm = []
        try:
            stm_msgs = get_short_memory(session, limit=CFG.STM_LIMIT) or []
        except Exception:
            stm_msgs = []

    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT_CHAT}]

    for m in ltm:
        if m:
            messages.append({"role": "system", "content": f"FACT: {m}"})

    for m in stm_msgs:
        role = _clean(m.get("role"))
        content = _clean(m.get("content"))
        if role in ("user", "assistant", "system") and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": msg})

    try:
        answer = _ollama_chat_messages(messages)

        if is_auth and session:
            ChatMessage.objects.create(session=session, role="user", content=msg)
            ChatMessage.objects.create(session=session, role="assistant", content=answer)

            if _should_save_long_memory(msg):
                try:
                    save_long_term_memory(user.id, msg)
                except Exception:
                    pass

            return JsonResponse({"ok": True, "answer": answer, "session_id": session.id})

        return JsonResponse({"ok": True, "answer": answer})

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Chat backend error: {e}"}, status=502)


# =========================================================
# Auto router (routes by group)
# =========================================================
GREETINGS = ["hi", "hello", "hey", "assalamualaikum", "as-salamu alaykum", "হ্যালো", "সালাম", "আসসালামু আলাইকুম"]
NOTICE_HINTS = ["notice", "circular", "memo", "বিজ্ঞপ্তি", "নোটিশ", "সার্কুলার", "মেমো"]


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


def _is_notice(q: str) -> bool:
    ql = (q or "").lower()
    return any(k in ql for k in NOTICE_HINTS)


@csrf_exempt
@require_POST
def auto_ask(request):
    """
    Router endpoint:
      - smalltalk -> api_chat
      - notice -> public_notice_search
      - otherwise -> route by user's first group (general/cashflow/core/hr/policy)
    IMPORTANT: This endpoint does NOT handle uploaded docs.
    If file_id exists in the UI, the UI must call /api/doc/ask.
    """
    body = _json_body(request)
    question = _clean(body.get("question") or body.get("message"))
    if not question:
        return JsonResponse({"ok": False, "error": "question is required"}, status=400)

    # Smalltalk -> general chat
    if _is_smalltalk(question):
        request._body = json.dumps({"message": question, "session_id": body.get("session_id")}, ensure_ascii=False).encode(
            "utf-8"
        )
        request.META["CONTENT_TYPE"] = "application/json"
        return api_chat(request)

    # Public notice
    if _is_notice(question):
        if (not getattr(request, "user", None) or not request.user.is_authenticated) and not ALLOW_ANON_PUBLIC:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)
        request._body = json.dumps({"query": question, "top_k": body.get("top_k")}, ensure_ascii=False).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return public_notice_search(request)

    # Business routes require auth
    deny = _deny_auth(request)
    if deny:
        return deny

    route = _first_route_group(request.user)
    if route not in ROUTE_GROUPS:
        route = "general"

    if route == "general":
        request._body = json.dumps({"message": question, "session_id": body.get("session_id")}, ensure_ascii=False).encode(
            "utf-8"
        )
        request.META["CONTENT_TYPE"] = "application/json"
        return api_chat(request)

    if route == "cashflow":
        from .views_cashflow_router import cashflow_query

        request._body = json.dumps({"q": question}, ensure_ascii=False).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return cashflow_query(request)

    if route == "core":
        request._body = json.dumps({"question": question, "top_k": body.get("top_k")}, ensure_ascii=False).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return core_ask(request)

    if route == "hr":
        request._body = json.dumps(
            {"question": question, "explain": bool(body.get("explain", False))}, ensure_ascii=False
        ).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return hr_ask(request)

    # policy
    request._body = json.dumps(
        {"question": question, "top_k": body.get("top_k"), "collection": body.get("collection")},
        ensure_ascii=False,
    ).encode("utf-8")
    request.META["CONTENT_TYPE"] = "application/json"
    return policy_ask(request)
