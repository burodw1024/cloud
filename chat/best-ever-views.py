# chat/views.py  (COMPLETE FILE - routing + general chat + workflow integration + customer support)
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

JsonT = Union[Dict[str, Any], List[Any]]

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


def index(request):
    return render(request, "chat/index.html")


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
    GEN_MODEL: str = _env("GEN_MODEL", "aya-23-8b:q5")
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
                "temperature": 0.2,
                "top_p": 0.85,
                "repeat_penalty": 1.2,
                "num_ctx": 4096,
                "num_predict": 220,
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
    block_terms = ["salary", "tk", "taka", "amount", "balance", "loan", "nid", "passport", "employee id", "pin", "otp"]
    if any(b in t for b in block_terms):
        return False
    triggers = ["remember", "from now on", "i prefer", "always", "important", "call me", "my name is"]
    return any(k in t for k in triggers)


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

    try:
        if (not is_auth) and not ALLOW_ANON_CHAT:
            CHAT_REQUESTS_TOTAL.labels(
                endpoint=endpoint, mode=mode, model=model, status="auth_required"
            ).inc()
            CHAT_REQUEST_DURATION_SECONDS.labels(
                endpoint=endpoint, mode=mode, model=model
            ).observe(time.perf_counter() - chat_start)
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        body = _json_body(request)
        msg = _clean(body.get("message") or body.get("question"))
        mode = _clean(body.get("mode")) or "general"
        model = _clean(body.get("model")) or CFG.GEN_MODEL

        if not msg:
            CHAT_REQUESTS_TOTAL.labels(
                endpoint=endpoint, mode=mode, model=model, status="bad_request"
            ).inc()
            CHAT_REQUEST_DURATION_SECONDS.labels(
                endpoint=endpoint, mode=mode, model=model
            ).observe(time.perf_counter() - chat_start)
            return JsonResponse({"ok": False, "error": "message is required"}, status=400)

        session = None
        session_id = body.get("session_id")

        if is_auth:
            if session_id is not None:
                session = ChatSession.objects.filter(id=session_id, user=user).first()
            if not session:
                session = ChatSession.objects.create(user=user, title="Chat")

            workflow_result = _try_workflow(request, session, msg)
            if workflow_result and workflow_result.get("handled"):
                try:
                    ChatMessage.objects.create(session=session, role="user", content=msg)
                    ChatMessage.objects.create(session=session, role="assistant", content=workflow_result["answer"])
                except Exception:
                    pass

                CHAT_REQUESTS_TOTAL.labels(
                    endpoint=endpoint,
                    mode="workflow",
                    model="workflow_engine",
                    status="success",
                ).inc()
                CHAT_REQUEST_DURATION_SECONDS.labels(
                    endpoint=endpoint,
                    mode="workflow",
                    model="workflow_engine",
                ).observe(time.perf_counter() - chat_start)

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
            rag_start = time.perf_counter()
            try:
                ltm = search_long_memory(user.id, msg, limit=CFG.LTM_TOP_K) or []
                RAG_HITS_TOTAL.labels(collection="long_memory").inc(len(ltm))
                RAG_SEARCH_DURATION_SECONDS.labels(
                    collection="long_memory", result="success"
                ).observe(time.perf_counter() - rag_start)
            except Exception:
                ltm = []
                RAG_SEARCH_DURATION_SECONDS.labels(
                    collection="long_memory", result="error"
                ).observe(time.perf_counter() - rag_start)

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
            llm_start = time.perf_counter()
            ollama_json = _ollama_chat_messages(messages, model)
            llm_elapsed = time.perf_counter() - llm_start

            LLM_REQUEST_DURATION_SECONDS.labels(model=model).observe(llm_elapsed)
            LLM_REQUESTS_TOTAL.labels(model=model, result="success").inc()

            total_duration = ns_to_seconds(ollama_json.get("total_duration"))
            load_duration = ns_to_seconds(ollama_json.get("load_duration"))
            prompt_eval_count = int(ollama_json.get("prompt_eval_count") or 0)
            eval_count = int(ollama_json.get("eval_count") or 0)

            if total_duration > 0:
                LLM_REQUEST_DURATION_SECONDS.labels(model=model).observe(total_duration)
            if load_duration > 0:
                LLM_LOAD_DURATION_SECONDS.labels(model=model).observe(load_duration)

            if prompt_eval_count > 0:
                LLM_PROMPT_TOKENS_TOTAL.labels(model=model).inc(prompt_eval_count)
            if eval_count > 0:
                LLM_OUTPUT_TOKENS_TOTAL.labels(model=model).inc(eval_count)

            answer = _clean((ollama_json.get("message") or {}).get("content"))
            if not answer:
                answer = _clean(ollama_json.get("response"))

            if is_auth and session:
                ChatMessage.objects.create(session=session, role="user", content=msg)
                ChatMessage.objects.create(session=session, role="assistant", content=answer)

                if _should_save_long_memory(msg):
                    try:
                        save_long_term_memory(user.id, msg)
                    except Exception:
                        pass

                CHAT_REQUESTS_TOTAL.labels(
                    endpoint=endpoint, mode=mode, model=model, status="success"
                ).inc()
                CHAT_REQUEST_DURATION_SECONDS.labels(
                    endpoint=endpoint, mode=mode, model=model
                ).observe(time.perf_counter() - chat_start)

                return JsonResponse({"ok": True, "answer": answer, "session_id": session.id})

            CHAT_REQUESTS_TOTAL.labels(
                endpoint=endpoint, mode=mode, model=model, status="success"
            ).inc()
            CHAT_REQUEST_DURATION_SECONDS.labels(
                endpoint=endpoint, mode=mode, model=model
            ).observe(time.perf_counter() - chat_start)

            return JsonResponse({"ok": True, "answer": answer})

        except Exception as e:
            LLM_REQUESTS_TOTAL.labels(model=model, result="error").inc()
            CHAT_REQUESTS_TOTAL.labels(
                endpoint=endpoint, mode=mode, model=model, status="error"
            ).inc()
            CHAT_REQUEST_DURATION_SECONDS.labels(
                endpoint=endpoint, mode=mode, model=model
            ).observe(time.perf_counter() - chat_start)
            return JsonResponse({"ok": False, "error": f"Chat backend error: {e}"}, status=502)

    finally:
        ACTIVE_CHAT_REQUESTS.dec()


GREETINGS = ["hi", "hello", "hey", "assalamualaikum", "as-salamu alaykum", "হ্যালো", "সালাম", "আসসালামু আলাইকুম"]
NOTICE_HINTS = ["notice", "circular", "memo", "বিজ্ঞপ্তি", "নোটিশ", "সার্কুলার", "মেমো"]

CUSTOMER_SUPPORT_HINTS = [
    "customer",
    "customer support",
    "customer care",
    "client",
    "member id",
    "member no",
    "customer id",
    "account no",
    "loan status",
    "loan pending",
    "installment",
    "installment problem",
    "payment not updated",
    "savings balance",
    "complaint",
    "ticket",
    "ticket status",
    "branch contact",
    "account close",
    "account closing",
    "new member",
    "member admission",
    "গ্রাহক",
    "কাস্টমার",
    "সদস্য",
    "সদস্য নং",
    "সদস্য আইডি",
    "গ্রাহক আইডি",
    "ঋণের অবস্থা",
    "লোন স্ট্যাটাস",
    "কিস্তি",
    "কিস্তি আপডেট",
    "জমা হয়নি",
    "সঞ্চয় ব্যালেন্স",
    "ব্যালেন্স",
    "অভিযোগ",
    "টিকেট",
    "শাখার যোগাযোগ",
    "হিসাব বন্ধ",
    "নতুন সদস্য",
    "ভর্তি",

 "loaneeno",
    "loan summary",
    "loan summaries",
    "loan information",
    "loan details",
    "loan balance",
    "loan outstanding",
    "loan installment",
    "loan repayment",
    "ঋণের সারাংশ",
    "ঋণের তথ্য",
    "লোন সামারি",
   "loanee list",
        "loanee no by branch",
        "loaneeno by branch",
        "branch wise loaneeno",    "লোন তথ্য",

]

TOPIC_LIST_HINTS = [
    "list of topic", "list topics", "list of topics", "show topics", "show all topics",
    "what topics can you answer", "which topics can you answer", "what can i ask",
    "topic list", "sub topic list", "subtopic list", "all topics", "all subtopics",
    "কোন কোন টপিক", "কোন কোন বিষয়", "কি কি টপিক", "কি কি বিষয়",
    "সব টপিক", "সব বিষয়", "টপিক লিস্ট", "সাব টপিক লিস্ট", "উপবিষয় তালিকা",
    "কী কী প্রশ্ন করতে পারি", "আমি কী কী জিজ্ঞেস করতে পারি", "কোন কোন প্রশ্ন করতে পারি",
]


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


def _is_customer_support_question(q: str) -> bool:
    ql = (q or "").lower()
    return any(k in ql for k in CUSTOMER_SUPPORT_HINTS)


def _is_policy_topic_list_request(q: str) -> bool:
    ql = (q or "").lower()
    return any(k in ql for k in TOPIC_LIST_HINTS)


def _looks_like_policy_question(q: str) -> bool:
    ql = (q or "").lower()
    policy_hints = [
        "policy", "hr policy", "promotion", "promotion eligibility", "grade", "leave",
        "casual leave", "earned leave", "maternity leave", "paternity leave", "festival leave",
        "sick leave", "probation", "confirmation", "seniority", "transfer", "distance allowance",
        "overtime allowance", "gratuity", "provident fund", "income tax", "resignation",
        "retirement", "termination", "show cause", "warning letter", "attendance", "absence",
        "sexual harassment", "sexual harasment", "joining rules", "recruitment", "grade structure",
        "staff type", "staff classification", "project staff", "project employee",
        "vehicle loan", "car loan", "house loan", "flat loan",
        "নীতিমালা", "পলিসি", "পদোন্নতি", "পদোন্নতির যোগ্যতা",
        "গ্রেড", "ছুটি", "নৈমিত্তিক ছুটি", "অর্জিত ছুটি", "মাতৃত্বজনিত ছুটি", "পিতৃত্বজনিত ছুটি",
        "উৎসবকালীন ছুটি", "অসুস্থতাজনিত ছুটি", "শিক্ষানবিশকাল", "স্থায়ীকরণ", "জ্যেষ্ঠতা", "বদলি",
        "দূরত্ব ভাতা", "অধিকাল ভাতা", "গ্র্যাচুইটি", "প্রভিডেন্ট ফান্ড", "আয়কর", "পদত্যাগ",
        "অবসর", "অব্যাহতি", "কারণ দর্শানো", "সতর্কীকরণ পত্র", "হাজিরা", "অনুপস্থিতি",
        "যৌন হয়রানি", "যোগদান বিধি", "নিয়োগ", "গ্রেড কাঠামো", "কর্মীদের শ্রেণী বিভাগ",
        "ঋণ", "গাড়ী ঋণ", "গাড়ি ঋণ", "গৃহ ঋণ", "ফ্ল্যাট ঋণ",
    ]
    return _is_policy_topic_list_request(q) or any(k in ql for k in policy_hints)


def _body_module(body: Dict[str, Any]) -> str:
    return _clean(
        body.get("module")
        or body.get("route")
        or body.get("mode")
        or body.get("selected_module")
    ).lower() or "auto"


@csrf_exempt
@require_POST
def auto_ask(request):
    body = _json_body(request)
    question = _clean(body.get("question") or body.get("message"))
    selected_module = _body_module(body)

    if not question:
        return JsonResponse({"ok": False, "error": "question is required"}, status=400)

    user = getattr(request, "user", None)
    is_auth = bool(user and user.is_authenticated)

    # Explicit Customer Support module selection.
    # This keeps your central /api/auto/ask endpoint but routes to the new module.
    if selected_module in {"customer_support", "customer", "support", "customer-care", "customer_care"}:
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        deny = _deny_auth(request)
        if deny:
            return deny

        if not _has_customer_support_access(request.user):
            return _deny_customer_support()

        return handle_customer_support(request, question)

    # Loan policy/rules questions should go to policy, not customer support or workflow.
    ql = question.lower()
    if "loan" in ql and (
        "rule" in ql or
        "rules" in ql or
        "policy" in ql or
        "eligibility" in ql or
        "vehicle" in ql or
        "car" in ql or
        "house" in ql or
        "flat" in ql or
        "নীতিমালা" in question or
        "নিয়ম" in question or
        "যোগ্যতা" in question or
        "ঋণ" in question or
        "গাড়ী" in question or
        "গাড়ি" in question
    ):
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        if not _has_group(user, "policy"):
            return _deny_group("policy")

        request._body = json.dumps(
            {
                "question": question,
                "top_k": body.get("top_k"),
                "collection": body.get("collection"),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return policy_ask(request)

    # Auto-detect Customer Support after policy-loan protection.
    # Example: "loan status member id 12345", "কিস্তি জমা হয়েছে কিন্তু আপডেট হয়নি".
    if selected_module == "auto" and _is_customer_support_question(question):
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        deny = _deny_auth(request)
        if deny:
            return deny

        if not _has_customer_support_access(request.user):
            return _deny_customer_support()

        return handle_customer_support(request, question)

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

    if _is_smalltalk(question):
        request._body = json.dumps(
            {"message": question, "session_id": body.get("session_id")},
            ensure_ascii=False,
        ).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return api_chat(request)

    if _is_policy_topic_list_request(question):
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        if not _has_group(request.user, "policy"):
            return _deny_group("policy")

        request._body = json.dumps(
            {
                "question": question,
                "top_k": body.get("top_k"),
                "collection": body.get("collection"),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return policy_ask(request)

    if _is_notice(question):
        if not is_auth:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        if not _has_group(request.user, "notice"):
            return _deny_group("notice")

        request._body = json.dumps(
            {
                "question": question,
                "top_k": body.get("top_k"),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return notice_ask(request)

    deny = _deny_auth(request)
    if deny:
        return deny

    if _looks_like_policy_question(question):
        if not _has_group(request.user, "policy"):
            return _deny_group("policy")

        request._body = json.dumps(
            {
                "question": question,
                "top_k": body.get("top_k"),
                "collection": body.get("collection"),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return policy_ask(request)

    route = _first_route_group(request.user)
    if route not in ROUTE_GROUPS:
        route = "general"

    if route == "general":
        request._body = json.dumps(
            {"message": question, "session_id": body.get("session_id")},
            ensure_ascii=False,
        ).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return api_chat(request)

    if route == "cashflow":
        from .views_cashflow_router import cashflow_query
        request._body = json.dumps(
            {"q": question},
            ensure_ascii=False,
        ).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return cashflow_query(request)

    if route == "core":
        request._body = json.dumps(
            {"question": question, "top_k": body.get("top_k")},
            ensure_ascii=False,
        ).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return core_ask(request)

    if route == "hr":
        request._body = json.dumps(
            {"question": question, "explain": bool(body.get("explain", False))},
            ensure_ascii=False,
        ).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return hr_ask(request)

    if route == "customer_support":
        if not _has_customer_support_access(request.user):
            return _deny_customer_support()
        return handle_customer_support(request, question)

    if route == "notice":
        if not _has_group(request.user, "notice"):
            return _deny_group("notice")

        request._body = json.dumps(
            {"question": question, "top_k": body.get("top_k")},
            ensure_ascii=False,
        ).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return notice_ask(request)

    if not _has_group(request.user, "policy"):
        return _deny_group("policy")

    request._body = json.dumps(
        {
            "question": question,
            "top_k": body.get("top_k"),
            "collection": body.get("collection"),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request.META["CONTENT_TYPE"] = "application/json"
    return policy_ask(request)

