# chat/views.py
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Union

import requests
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .views_notice import notice_ask
from .metrics import (
    CHAT_REQUESTS_TOTAL,
    CHAT_REQUEST_DURATION_SECONDS,
    LLM_REQUEST_DURATION_SECONDS,
    LLM_LOAD_DURATION_SECONDS,
    LLM_PROMPT_TOKENS_TOTAL,
    LLM_OUTPUT_TOKENS_TOTAL,
    LLM_REQUESTS_TOTAL,
    RAG_SEARCH_DURATION_SECONDS,
    RAG_HITS_TOTAL,
    NIFI_REQUEST_DURATION_SECONDS,
    NIFI_REQUESTS_TOTAL,
    ACTIVE_CHAT_REQUESTS,
)

from .models import ChatSession, ChatMessage
from .memory_utils import get_short_memory
from .qdrant_memory_store import search_long_memory, save_long_term_memory

from .views_hr_router import hr_ask
from .views_policy import policy_ask
from .views_core import core_ask
from .views_public import public_notice_search
from .customer_support_handler import handle_customer_support

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
from chat.app_config import APP_CONFIG
from chat.dynamic_config import get_qdrant_url
from chat.dynamic_config import get_ollama_url
JsonT = Union[Dict[str, Any], List[Any]]


# -------------------------
# Environment helpers
# -------------------------
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "y", "on")


def _strip_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


# -------------------------
# Central runtime config
# -------------------------
@dataclass(frozen=True)
class Cfg:
    # Ollama / LLM
   # OLLAMA_URL: str = _strip_url(_env("OLLAMA_URL", "http://192.168.1.144:31134"))
    OLLAMA_URL: str = get_ollama_url() 
    GEN_MODEL: str = _env("GEN_MODEL", "aya-23-8b:q5")
    SQL_MODEL: str = _env("SQL_MODEL", "sqlcoder-7b:q4km")
    GEN_TIMEOUT: float = _env_float("GEN_TIMEOUT", 600.0)

    LLM_TEMPERATURE: float = _env_float("LLM_TEMPERATURE", 0.2)
    LLM_TOP_P: float = _env_float("LLM_TOP_P", 0.85)
    LLM_REPEAT_PENALTY: float = _env_float("LLM_REPEAT_PENALTY", 1.2)
    LLM_NUM_CTX: int = _env_int("LLM_NUM_CTX", 4096)
    LLM_NUM_PREDICT: int = _env_int("LLM_NUM_PREDICT", 220)

    # Qdrant
    #QDRANT_URL: str = _strip_url(_env("QDRANT_URL", "http://192.168.1.176:31633"))

    #QDRANT_URL: str = APP_CONFIG.qdrant.get_url(check_health=True)

    QDRANT_URL: str =  get_qdrant_url(check_health=True)
    QDRANT_API_KEY: str = _env("QDRANT_API_KEY", "")
    QDRANT_TIMEOUT: float = _env_float("QDRANT_TIMEOUT", 30.0)

    QDRANT_COLLECTION_POLICY: str = _env("QDRANT_COLLECTION_POLICY", "buro_policy")
    QDRANT_COLLECTION_NOTICE: str = _env("QDRANT_COLLECTION_NOTICE", "hr_notices")
    QDRANT_COLLECTION_CUSTOMER_SUPPORT: str = _env(
        "QDRANT_COLLECTION_CUSTOMER_SUPPORT",
        "customer_support_knowledge",
    )
    QDRANT_COLLECTION_LONG_MEMORY: str = _env(
        "QDRANT_COLLECTION_LONG_MEMORY",
        "user_long_memory",
    )
    QDRANT_COLLECTION_CASHFLOW: str = _env("QDRANT_COLLECTION_CASHFLOW", "dw_cashflow")

    # NiFi
    NIFI_BASE_URL: str = _strip_url(_env("NIFI_BASE_URL", "http://192.168.1.139:8888"))
    NIFI_CUSTOMER_SUPPORT_URL: str = _strip_url(
        _env("NIFI_CUSTOMER_SUPPORT_URL", "http://192.168.1.139:8888/core/query")
    )
    NIFI_HR_URL: str = _strip_url(
        _env("NIFI_HR_URL", "http://192.168.1.139:8888/hr/query")
    )
    NIFI_CORE_URL: str = _strip_url(
        _env("NIFI_CORE_URL", "http://192.168.1.139:8888/core/query")
    )
    NIFI_CASHFLOW_URL: str = _strip_url(
        _env("NIFI_CASHFLOW_URL", "http://192.168.1.139:8888/cashflow/query")
    )
    NIFI_TIMEOUT: float = _env_float("NIFI_TIMEOUT", 60.0)

    # Memory
    STM_LIMIT: int = _env_int("STM_LIMIT", 10)
    LTM_TOP_K: int = _env_int("LTM_TOP_K", 5)

    # Access
    ENFORCE_STAFF_ONLY: bool = _env_bool("ENFORCE_STAFF_ONLY", True)
    ALLOW_ANON_CHAT: bool = _env_bool("ALLOW_ANON_CHAT", True)
    ALLOW_ANON_PUBLIC: bool = _env_bool("ALLOW_ANON_PUBLIC", True)


CFG = Cfg()


# -------------------------
# Route / permission config
# -------------------------
ROUTE_GROUPS: Set[str] = {
    "general",
    "cashflow",
    "hr",
    "core",
    "policy",
    "notice",
    "customer_support",
}

ROUTE_PRIORITY: List[str] = [
    "general",
    "cashflow",
    "core",
    "hr",
    "customer_support",
    "policy",
    "notice",
]

CUSTOMER_SUPPORT_GROUPS: Set[str] = {
    "customer_support",
    "branch_staff",
    "support_staff",
    "support_manager",
    "admin",
}

ENFORCE_STAFF_ONLY = CFG.ENFORCE_STAFF_ONLY
ALLOW_ANON_CHAT = CFG.ALLOW_ANON_CHAT
ALLOW_ANON_PUBLIC = CFG.ALLOW_ANON_PUBLIC


SYSTEM_PROMPT_CHAT = _env(
    "SYSTEM_PROMPT_CHAT",
    """You are a helpful assistant.

Rules:
- Answer directly and clearly.
- If unsure, say you are unsure.
- Do not invent facts.
""",
)


# -------------------------
# Basic helpers
# -------------------------
def _user_group_names(user) -> Set[str]:
    try:
        if not user or not user.is_authenticated:
            return set()
        return set(user.groups.values_list("name", flat=True))
    except Exception:
        return set()


def _has_group(user, group_name: str) -> bool:
    try:
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "is_superuser", False):
            return True
        return user.groups.filter(name=group_name).exists()
    except Exception:
        return False


def _has_any_group(user, group_names: Set[str]) -> bool:
    try:
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "is_superuser", False):
            return True
        user_groups = _user_group_names(user)
        return bool(user_groups.intersection(group_names))
    except Exception:
        return False


def _has_customer_support_access(user) -> bool:
    return _has_any_group(user, CUSTOMER_SUPPORT_GROUPS)


def _deny_group(group_name: str) -> JsonResponse:
    return JsonResponse(
        {"ok": False, "error": f"{group_name} group access required."},
        status=403,
    )


def _deny_customer_support() -> JsonResponse:
    return JsonResponse(
        {
            "ok": False,
            "error": "Customer Support module access required.",
            "hint": "Add the user to one of these groups: customer_support, branch_staff, support_staff, support_manager, admin.",
        },
        status=403,
    )


def _deny_auth(request) -> Optional[JsonResponse]:
    user = getattr(request, "user", None)

    if not user or not user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

    if getattr(user, "is_superuser", False):
        return None

    if ENFORCE_STAFF_ONLY and not getattr(user, "is_staff", False):
        return JsonResponse(
            {
                "ok": False,
                "error": "Staff access required.",
                "hint": "Set is_staff=True or set ENFORCE_STAFF_ONLY=0",
            },
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

    if groups.intersection(CUSTOMER_SUPPORT_GROUPS):
        return "customer_support"

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


def _set_json_body(request, payload: Dict[str, Any]) -> None:
    request._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request.META["CONTENT_TYPE"] = "application/json"


def index(request):
    return render(request, "chat/index.html")


# -------------------------
# HTTP / LLM helpers
# -------------------------
def _http_json(
    method: str,
    url: str,
    *,
    timeout: float,
    json_body: Optional[dict] = None,
) -> JsonT:
    r = requests.request(
        method,
        url,
        json=json_body,
        timeout=timeout,
        headers={"Accept": "application/json"},
    )

    if not r.ok:
        raise RuntimeError("HTTP %s from %s: %s" % (r.status_code, url, (r.text or "")[:600]))

    try:
        return r.json()
    except Exception as e:
        raise RuntimeError("Invalid JSON from %s: %s. Body=%s" % (url, e, (r.text or "")[:600]))


def ns_to_seconds(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value) / 1_000_000_000.0
    except Exception:
        return 0.0


def _ollama_chat_messages(messages: List[Dict[str, str]], model: str) -> Dict[str, Any]:
    data = _http_json(
        "POST",
        f"{CFG.OLLAMA_URL}/api/chat",
        timeout=CFG.GEN_TIMEOUT,
        json_body={
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": CFG.LLM_TEMPERATURE,
                "top_p": CFG.LLM_TOP_P,
                "repeat_penalty": CFG.LLM_REPEAT_PENALTY,
                "num_ctx": CFG.LLM_NUM_CTX,
                "num_predict": CFG.LLM_NUM_PREDICT,
            },
        },
    )

    if not isinstance(data, dict):
        return {}

    return data


def _should_save_long_memory(text: str) -> bool:
    if not text:
        return False

    t = text.lower()

    block_terms = [
        "salary",
        "tk",
        "taka",
        "amount",
        "balance",
        "loan",
        "nid",
        "passport",
        "employee id",
        "pin",
        "otp",
    ]

    if any(b in t for b in block_terms):
        return False

    triggers = [
        "remember",
        "from now on",
        "i prefer",
        "always",
        "important",
        "call me",
        "my name is",
    ]

    return any(k in t for k in triggers)


# -------------------------
# Workflow helper
# -------------------------
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
            "answer": result.get("reply", ""),
            "workflow_active": not bool(result.get("completed", False)),
            "workflow_intent": active.intent_name,
        }

    intent = detect_intent(msg)
    if not intent:
        return None

    definition = get_active_definition(intent)

    if not definition:
        return {
            "handled": True,
            "answer": f"Workflow intent '{intent}' was detected, but no active workflow definition was found.",
            "workflow_active": False,
            "workflow_intent": intent,
        }

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


# -------------------------
# General chat endpoint
# -------------------------
@csrf_exempt
@require_POST
def api_chat(request):
    endpoint = "/api/chat"
    mode = "general"
    model = CFG.GEN_MODEL
    chat_start = time.perf_counter()

    ACTIVE_CHAT_REQUESTS.inc()

    user = getattr(request, "user", None)
    is_auth = bool(user and user.is_authenticated)

    # Protected manual website sync command.
    # This MUST run before public website RAG detection because
    # phrases like "sync buro website" also contain website keywords.
    if _is_website_sync_request(question):
        return _handle_website_sync_command(request)

    # Explicit Customer Support module selection
    if selected_module in {"customer_support", "customer", "support", "customer-care", "customer_care"}:
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        deny = _deny_auth(request)
        if deny:
            return deny

        if not _has_customer_support_access(request.user):
            return _deny_customer_support()

        return handle_customer_support(request, question)

    # Explicit module: Policy
    if selected_module == "policy":
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        if not _has_group(request.user, "policy"):
            return _deny_group("policy")

        _set_json_body(
            request,
            {
                "question": question,
                "top_k": body.get("top_k"),
                "collection": body.get("collection"),
            },
        )
        return policy_ask(request)

    # Explicit module: Notice
    if selected_module == "notice":
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        if not _has_group(request.user, "notice"):
            return _deny_group("notice")

        _set_json_body(
            request,
            {
                "question": question,
                "top_k": body.get("top_k"),
            },
        )
        return notice_ask(request)

    # Explicit module: Core
    if selected_module == "core":
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        deny = _deny_auth(request)
        if deny:
            return deny

        _set_json_body(
            request,
            {
                "question": question,
                "top_k": body.get("top_k"),
                "limit": body.get("limit"),
                "execute": body.get("execute", True),
                "pretty": body.get("pretty", True),
                "debug": body.get("debug", False),
            },
        )
        return core_ask(request)

    # Explicit module: HR
    if selected_module == "hr":
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        deny = _deny_auth(request)
        if deny:
            return deny

        _set_json_body(
            request,
            {
                "question": question,
                "explain": bool(body.get("explain", False)),
            },
        )
        return hr_ask(request)

    # Explicit module: Cashflow
    if selected_module == "cashflow":
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        deny = _deny_auth(request)
        if deny:
            return deny

        from .views_cashflow_router import cashflow_query

        _set_json_body(
            request,
            {
                "q": question,
            },
        )
        return cashflow_query(request)

    # Loan policy/rules questions should go to policy, not customer support/workflow/core data.
    ql = question.lower()

    if "loan" in ql and (
        "rule" in ql
        or "rules" in ql
        or "policy" in ql
        or "eligibility" in ql
        or "vehicle" in ql
        or "car" in ql
        or "house" in ql
        or "flat" in ql
        or "নীতিমালা" in question
        or "নিয়ম" in question
        or "যোগ্যতা" in question
        or "ঋণের নিয়ম" in question
        or "লোন নিয়ম" in question
        or "ঋণের নীতিমালা" in question
        or "লোন নীতিমালা" in question
        or "গাড়ী" in question
        or "গাড়ি" in question
    ):
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        if not _has_group(user, "policy"):
            return _deny_group("policy")

        _set_json_body(
            request,
            {
                "question": question,
                "top_k": body.get("top_k"),
                "collection": body.get("collection"),
            },
        )
        return policy_ask(request)

    # Auto-detect Customer Support
    if selected_module == "auto" and _is_customer_support_question(question):
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        deny = _deny_auth(request)
        if deny:
            return deny

        if not _has_customer_support_access(request.user):
            return _deny_customer_support()

        return handle_customer_support(request, question)

    # Core data questions must go to Core before workflow detection.
    # This fixes: "Show top 20 outstanding loan" wrongly becoming loan_application workflow.
    if selected_module == "auto" and _looks_like_core_data_question(question):
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        deny = _deny_auth(request)
        if deny:
            return deny

        _set_json_body(
            request,
            {
                "question": question,
                "top_k": body.get("top_k"),
                "limit": body.get("limit"),
                "execute": body.get("execute", True),
                "pretty": body.get("pretty", True),
                "debug": body.get("debug", False),
            },
        )
        return core_ask(request)

    # Workflow handling
    if is_auth:
        session = None
        session_id = body.get("session_id")

        if session_id is not None:
            session = ChatSession.objects.filter(id=session_id, user=user).first()

        if not session:
            session = ChatSession.objects.create(user=user, title="Chat")

        workflow_result = _try_workflow(request, session, question)

        if workflow_result and workflow_result.get("handled"):
            try:
                ChatMessage.objects.create(session=session, role="user", content=question)
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

    # Smalltalk
    if _is_smalltalk(question):
        _set_json_body(
            request,
            {
                "message": question,
                "session_id": body.get("session_id"),
            },
        )
        return api_chat(request)

    # Policy topic list
    if _is_policy_topic_list_request(question):
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        if not _has_group(request.user, "policy"):
            return _deny_group("policy")

        _set_json_body(
            request,
            {
                "question": question,
                "top_k": body.get("top_k"),
                "collection": body.get("collection"),
            },
        )
        return policy_ask(request)

    # Notice auto-detection
    if _is_notice(question):
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        if not _has_group(request.user, "notice"):
            return _deny_group("notice")

        _set_json_body(
            request,
            {
                "question": question,
                "top_k": body.get("top_k"),
            },
        )
        return notice_ask(request)

    # Protected route below
    deny = _deny_auth(request)
    if deny:
        return deny

    # Policy auto-detection
    if _looks_like_policy_question(question):
        if not _has_group(request.user, "policy"):
            return _deny_group("policy")

        _set_json_body(
            request,
            {
                "question": question,
                "top_k": body.get("top_k"),
                "collection": body.get("collection"),
            },
        )
        return policy_ask(request)

    # Group-based default routing
    route = _first_route_group(request.user)

    if route not in ROUTE_GROUPS:
        route = "general"

    if route == "general":
        _set_json_body(
            request,
            {
                "message": question,
                "session_id": body.get("session_id"),
            },
        )
        return api_chat(request)

    if route == "cashflow":
        from .views_cashflow_router import cashflow_query

        _set_json_body(
            request,
            {
                "q": question,
            },
        )
        return cashflow_query(request)

    if route == "core":
        _set_json_body(
            request,
            {
                "question": question,
                "top_k": body.get("top_k"),
                "limit": body.get("limit"),
                "execute": body.get("execute", True),
                "pretty": body.get("pretty", True),
                "debug": body.get("debug", False),
            },
        )
        return core_ask(request)

    if route == "hr":
        _set_json_body(
            request,
            {
                "question": question,
                "explain": bool(body.get("explain", False)),
            },
        )
        return hr_ask(request)

    if route == "customer_support":
        if not _has_customer_support_access(request.user):
            return _deny_customer_support()

        return handle_customer_support(request, question)

    if route == "notice":
        if not _has_group(request.user, "notice"):
            return _deny_group("notice")

        _set_json_body(
            request,
            {
                "question": question,
                "top_k": body.get("top_k"),
            },
        )
        return notice_ask(request)

    # Final fallback to policy
    if not _has_group(request.user, "policy"):
        return _deny_group("policy")

    _set_json_body(
        request,
        {
            "question": question,
            "top_k": body.get("top_k"),
            "collection": body.get("collection"),
        },
    )
    return policy_ask(request)


# -------------------------
# Optional backend health endpoint
# -------------------------
def backend_health(request):
    """
    Optional URL:
      path("api/backend-health/", views.backend_health, name="backend_health")
    """

    result: Dict[str, Any] = {
        "ok": True,
        "ollama": {
            "url": CFG.OLLAMA_URL,
            "ok": False,
        },
        "qdrant": {
            "url": CFG.QDRANT_URL,
            "ok": False,
        },
        "nifi": {
            "base_url": CFG.NIFI_BASE_URL,
            "customer_support_url": CFG.NIFI_CUSTOMER_SUPPORT_URL,
            "core_url": CFG.NIFI_CORE_URL,
            "cashflow_url": CFG.NIFI_CASHFLOW_URL,
            "ok": False,
        },
    }

    # Ollama
    try:
        r = requests.get(f"{CFG.OLLAMA_URL}/api/tags", timeout=5)
        result["ollama"]["status_code"] = r.status_code
        result["ollama"]["ok"] = r.status_code < 500
    except Exception as e:
        result["ollama"]["error"] = str(e)

    # Qdrant
    try:
        headers = {"Accept": "application/json"}
        if CFG.QDRANT_API_KEY:
            headers["api-key"] = CFG.QDRANT_API_KEY

        r = requests.get(f"{CFG.QDRANT_URL}/collections", timeout=5, headers=headers)
        result["qdrant"]["status_code"] = r.status_code
        result["qdrant"]["ok"] = r.status_code < 500
    except Exception as e:
        result["qdrant"]["error"] = str(e)

    # NiFi customer support
    try:
        r = requests.get(CFG.NIFI_CUSTOMER_SUPPORT_URL, timeout=5)
        result["nifi"]["customer_support_status_code"] = r.status_code
        result["nifi"]["customer_support_ok"] = r.status_code < 500
    except Exception as e:
        result["nifi"]["customer_support_error"] = str(e)

    # NiFi core
    try:
        r = requests.get(CFG.NIFI_CORE_URL, timeout=5)
        result["nifi"]["core_status_code"] = r.status_code
        result["nifi"]["core_ok"] = r.status_code < 500
    except Exception as e:
        result["nifi"]["core_error"] = str(e)

    result["nifi"]["ok"] = bool(
        result["nifi"].get("customer_support_ok")
        or result["nifi"].get("core_ok")
    )

    result["ok"] = bool(
        result["ollama"].get("ok")
        and result["qdrant"].get("ok")
    )

    return JsonResponse(result)
