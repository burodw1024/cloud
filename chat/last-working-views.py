# chat/views.py
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union, Set

import requests
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .views_hr_router import hr_ask  # HR (NiFi/Oracle truth + HR RBAC)
from django.views.decorators.csrf import csrf_protect
# =========================================================
# GROUPS + RBAC
# =========================================================
ALL_GROUPS: Set[str] = {
    "BRANCH_MANAGER",
    "DEPT_ACCOUNTS",
    "DEPT_FINANCE",
    "DEPT_HR",
    "DEPT_IT",
    "DEPT_IT_HEAD",
    "DEPT_PROCUREMENT",
    "DEPT_RESEARCH",
    "G1",
    "G2",
    "G3",
    "G4",
    "G5",
    "HEAD_OFFICE",
}

ROUTER_POLICY: Dict[str, Set[str]] = {
    "hr": {"DEPT_HR", "HEAD_OFFICE"},
    "rag_docs": set(ALL_GROUPS),
    "chat": set(ALL_GROUPS),
}

ENFORCE_STAFF_ONLY = (os.getenv("ENFORCE_STAFF_ONLY", "1") or "1").lower() not in ("0", "false", "no")
ALLOW_ANON_CHAT = (os.getenv("ALLOW_ANON_CHAT", "1") or "1").lower() not in ("0", "false", "no")
ALLOW_ANON_RAG  = (os.getenv("ALLOW_ANON_RAG", "0") or "0").lower() not in ("0", "false", "no")


def _user_group_names(user) -> Set[str]:
    try:
        if not user or not user.is_authenticated:
            return set()
        return set(user.groups.values_list("name", flat=True))
    except Exception:
        return set()


def _deny_if_not_allowed(request, router_name: str) -> Optional[JsonResponse]:
    user = getattr(request, "user", None)

    # ✅ allow anonymous for chat/rag if enabled
    if not user or not user.is_authenticated:
        if router_name == "chat" and ALLOW_ANON_CHAT:
            return None
        if router_name == "rag_docs" and ALLOW_ANON_RAG:
            return None
        return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

    # superuser bypass
    if getattr(user, "is_superuser", False):
        return None

    # staff-only gate (only for authenticated users)
    if ENFORCE_STAFF_ONLY and not getattr(user, "is_staff", False):
        return JsonResponse({"ok": False, "error": "Staff access required."}, status=403)

    allowed = ROUTER_POLICY.get(router_name, set())
    if not allowed:
        return JsonResponse({"ok": False, "error": "Access denied (unknown router).", "router": router_name}, status=403)

    user_groups = _user_group_names(user)
    if user_groups & allowed:
        return None

    return JsonResponse(
        {
            "ok": False,
            "error": "Access denied.",
            "router": router_name,
            "required_groups": sorted(list(allowed)),
            "your_groups": sorted(list(user_groups))[:50],
        },
        status=403,
    )



# =========================================================
# Config helpers
# =========================================================
def _env(name: str, default: str) -> str:
    return (os.getenv(name, default) or "").strip()


def _env_float(name: str, default: str) -> float:
    try:
        return float(_env(name, default))
    except Exception:
        return float(default)


def _env_int(name: str, default: str) -> int:
    try:
        return int(_env(name, default))
    except Exception:
        return int(default)


# =========================================================
# Main config
# =========================================================
@dataclass(frozen=True)
class Cfg:
    OLLAMA_URL: str = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    QDRANT_URL: str = _env("QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")

    GEN_MODEL: str = _env("GEN_MODEL", "aya-23-8b")
    EMBED_MODEL: str = _env("EMBED_MODEL", "nomic-embed-text")

    GEN_TIMEOUT: float = _env_float("GEN_TIMEOUT", "600")
    EMBED_TIMEOUT: float = _env_float("EMBED_TIMEOUT", "300")
    QDRANT_TIMEOUT: float = _env_float("QDRANT_TIMEOUT", "300")

    TOP_K_DEFAULT: int = _env_int("TOP_K_DEFAULT", "6")
    MIN_RAG_SCORE: float = _env_float("MIN_RAG_SCORE", "0.70")

    DOC_COLLECTION: str = _env("DOC_COLLECTION", "dw_text")


CFG = Cfg()

SYSTEM_PROMPT_DOCS = _env(
    "SYSTEM_PROMPT_DOCS",
    (
        "তুমি একটি অন-প্রিম নীতিমালা সহকারী।\n"
        "কঠোর নিয়ম:\n"
        "1) CONTEXT থাকলে শুধুই CONTEXT থেকে উত্তর দেবে।\n"
        "2) CONTEXT এ স্পষ্টভাবে না থাকলে ঠিক এটাই লিখবে: ডেটা অপর্যাপ্ত।\n"
        "3) কোনো নতুন কথা/ভিডিও/ধারণা/গল্প যোগ করবে না।\n"
        "4) উত্তর বাংলায় হবে এবং সংক্ষিপ্ত হবে।\n"
    ),
)

SYSTEM_PROMPT_CHAT = _env(
    "SYSTEM_PROMPT_CHAT",
    "You are a helpful assistant. If you are unsure, say you are unsure.",
)

RAG_TEMPLATE = """CONTEXT:
{context}

QUESTION:
{question}

উত্তর (শুধুই CONTEXT থেকে, ৩-৪ লাইনে):
""".strip()


JsonT = Union[Dict[str, Any], List[Any]]


def _json_body(request) -> Dict[str, Any]:
    try:
        raw = (request.body or b"{}").decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _clean(v: Any) -> str:
    return ("" if v is None else str(v)).strip()


def _http_json(method: str, url: str, *, timeout: float, json_body: Optional[dict] = None) -> JsonT:
    r = requests.request(method, url, json=json_body, timeout=timeout, headers={"Accept": "application/json"})
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:300]}")
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Invalid JSON from {url}: {e}. Body={r.text[:300]}")


def _ollama_chat(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.2,
    top_p: float = 0.85,
    num_predict: int = 220,
) -> str:
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
            "options": {
                "temperature": float(temperature),
                "top_p": float(top_p),
                "repeat_penalty": 1.2,
                "num_ctx": 4096,
                "num_predict": int(num_predict),
            },
        },
    )
    if not isinstance(data, dict):
        return ""
    return _clean((data.get("message") or {}).get("content"))


def _ollama_embed(text: str) -> List[float]:
    data = _http_json(
        "POST",
        f"{CFG.OLLAMA_URL}/api/embeddings",
        timeout=CFG.EMBED_TIMEOUT,
        json_body={"model": CFG.EMBED_MODEL, "prompt": text},
    )
    if not isinstance(data, dict) or not isinstance(data.get("embedding"), list):
        raise RuntimeError("Ollama embeddings failed")
    return data["embedding"]


def _qdrant_search(collection: str, vector: List[float], limit: int) -> List[dict]:
    data = _http_json(
        "POST",
        f"{CFG.QDRANT_URL}/collections/{collection}/points/search",
        timeout=CFG.QDRANT_TIMEOUT,
        json_body={
            "vector": vector,
            "limit": int(limit),
            "with_payload": True,
            "with_vector": False,
        },
    )
    if not isinstance(data, dict):
        return []
    hits = data.get("result") or []
    return hits if isinstance(hits, list) else []


def _top_hit(hits: List[dict]) -> Tuple[float, str]:
    if not hits:
        return 0.0, ""
    h = hits[0]
    score = float(h.get("score") or 0.0)
    payload = h.get("payload") or {}
    text = _clean(payload.get("prompt"))
    return score, text


def _dedupe_hits(hits: List[dict]) -> List[dict]:
    seen = set()
    out = []
    for h in hits:
        p = h.get("payload") or {}
        t = _clean(p.get("prompt"))
        key = t[:240]
        if key and key not in seen:
            seen.add(key)
            out.append(h)
    return out


# =========================================================
# Router heuristics
# =========================================================
HR_HINTS = [
    "employee", "emp", "employee id", "employeeid",
    "salary", "basic", "basicsalary",
    "top salary", "highest salary",
    "বেতন", "বেসিক", "কর্মচারী", "এমপ্লয়ি", "আইডি",
    "সর্বোচ্চ", "সর্বনিম্ন", "গড়", "মোট", "কতজন",
    "pin", "কোড", "code",
]


def _is_hr_question(q: str) -> bool:
    ql = (q or "").lower()
    return any(k in ql for k in HR_HINTS)


GREETINGS = ["hi", "hello", "hey", "হ্যালো", "সালাম", "আসসালামু আলাইকুম"]


def _is_smalltalk(q: str) -> bool:
    qn = (q or "").strip().lower()
    if not qn:
        return False
    if len(qn) <= 12:
        return True
    return any(g in qn for g in GREETINGS)


# =========================================================
# Pages
# =========================================================
def index(request):
    return render(request, "chat/index.html")


# =========================================================
# APIs
# =========================================================
@csrf_exempt
@require_POST
def api_chat(request):
    deny = _deny_if_not_allowed(request, "chat")
    if deny:
        return deny

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

    answer = _ollama_chat(SYSTEM_PROMPT_CHAT, "\n".join(parts), temperature=0.2, top_p=0.85, num_predict=220)
    return JsonResponse({"ok": True, "answer": answer})


@csrf_exempt
@require_POST
def api_ask(request):
    deny = _deny_if_not_allowed(request, "rag_docs")
    if deny:
        return deny

    body = _json_body(request)
    question = _clean(body.get("question") or body.get("message"))
    if not question:
        return JsonResponse({"ok": False, "error": "question is required"}, status=400)

    top_k = int(body.get("top_k") or CFG.TOP_K_DEFAULT)
    collection = CFG.DOC_COLLECTION

    vec = _ollama_embed(question)
    hits = _dedupe_hits(_qdrant_search(collection, vec, limit=top_k))

    ctx_lines: List[str] = []
    sources: List[dict] = []
    for i, h in enumerate(hits, 1):
        p = h.get("payload") or {}
        txt = _clean(p.get("prompt"))
        if not txt:
            continue
        ctx_lines.append(f"[{i}] {txt}")
        sources.append({"id": h.get("id"), "score": h.get("score"), "collection": collection})

    context = "\n\n".join(ctx_lines)
    score, top_text = _top_hit(hits)

    if not top_text or score < CFG.MIN_RAG_SCORE:
        return JsonResponse({"ok": True, "answer": "ডেটা অপর্যাপ্ত।", "sources": sources, "top_score": score})

    prompt = RAG_TEMPLATE.format(context=context, question=question)
    answer = _ollama_chat(SYSTEM_PROMPT_DOCS, prompt, temperature=0.0, top_p=0.7, num_predict=220)

    if "ডেটা অপর্যাপ্ত" in (answer or ""):
        answer = "ডেটা অপর্যাপ্ত।"

    return JsonResponse({"ok": True, "answer": answer, "sources": sources, "top_score": score})


@csrf_exempt
@require_POST
def auto_ask(request):
    body = _json_body(request)
    question = _clean(body.get("question") or body.get("message"))
    if not question:
        return JsonResponse({"ok": False, "error": "question is required"}, status=400)

    # =========================================================
    # ✅ FOLLOW-UP HANDLER (pins/employeeids from last salary_over)
    # =========================================================
    ql = question.lower().strip()
    wants_pin = any(k in ql for k in ["pin", "পিন", "code", "কোড"])
    wants_empid = any(k in ql for k in ["employeeid", "employee id", "empid", "emp id", "এমপ্লয়ি আইডি", "আইডি"])

    if (wants_pin or wants_empid) and hasattr(request, "session") and request.session.get("last_hr_salary_over"):
        last = request.session.get("last_hr_salary_over") or {}
        pins = last.get("pins") or []
        empids = last.get("employeeids") or []

        parts = []
        if wants_pin:
            parts.append("PIN তালিকা:\n" + ("\n".join(pins[:200]) if pins else "ডেটা অপর্যাপ্ত।"))
        if wants_empid:
            parts.append("EmployeeID তালিকা:\n" + ("\n".join(empids[:200]) if empids else "ডেটা অপর্যাপ্ত।"))

        return JsonResponse(
            {
                "ok": True,
                "answer": "\n\n".join(parts).strip() or "ডেটা অপর্যাপ্ত।",
                "pins": pins if wants_pin else [],
                "employeeids": empids if wants_empid else [],
                "from_session": True,
            }
        )

    # normal routing
    want_rag = bool(body.get("want_rag", True))

    if _is_smalltalk(question):
        deny = _deny_if_not_allowed(request, "chat")
        if deny:
            return deny
        request._body = json.dumps({"message": question, "history": body.get("history") or []}, ensure_ascii=False).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return api_chat(request)

    if _is_hr_question(question):
        deny = _deny_if_not_allowed(request, "hr")
        if deny:
            return deny

        hr_payload = {
            "question": question,
            "explain": bool(body.get("explain", False)),
        }
        request._body = json.dumps(hr_payload, ensure_ascii=False).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return hr_ask(request)

    if want_rag:
        deny = _deny_if_not_allowed(request, "rag_docs")
        if deny:
            return deny
        request._body = json.dumps({"question": question, "top_k": body.get("top_k", CFG.TOP_K_DEFAULT)}, ensure_ascii=False).encode("utf-8")
        request.META["CONTENT_TYPE"] = "application/json"
        return api_ask(request)

    deny = _deny_if_not_allowed(request, "chat")
    if deny:
        return deny
    request._body = json.dumps({"message": question, "history": body.get("history") or []}, ensure_ascii=False).encode("utf-8")
    request.META["CONTENT_TYPE"] = "application/json"
    return api_chat(request)



import re
import requests

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect


# chat/views.py
from django.views.decorators.csrf import csrf_exempt
from .upload import require_staff_json, handle_upload_request

@csrf_exempt
def upload_jsonl(request):
    auth_err = require_staff_json(request)
    if auth_err:
        return auth_err
    return handle_upload_request(request)



_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")


def _validate_collection_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Collection name is required.")
    if not _NAME_RE.match(name):
        raise ValueError(
            "Invalid collection name. Use only letters, digits, underscore (_), dash (-), dot (.), no spaces."
        )
    return name


def _qdrant_request(method: str, url: str, *, json=None, timeout=20):
    try:
        r = requests.request(method, url, json=json, timeout=timeout)
    except requests.RequestException as ex:
        raise RuntimeError(f"Qdrant request failed: {ex}") from ex

    if r.status_code >= 300:
        body = (r.text or "")[:600]
        raise RuntimeError(f"Qdrant error {r.status_code}: {body}")
    return r


def _create_collection(QDRANT_URL: str, name: str, dims: int, distance: str):
    # Try "newer" style (named vectors) first; then fallback to "older" style
    payloads = [
        {"vectors": {"default": {"size": dims, "distance": distance}}},
        {"vectors": {"size": dims, "distance": distance}},
    ]

    last_err = None
    for payload in payloads:
        try:
            _qdrant_request("PUT", f"{QDRANT_URL}/collections/{name}", json=payload, timeout=20)
            return  # success
        except Exception as ex:
            last_err = ex

    raise last_err or RuntimeError("Create failed for unknown reason.")


@staff_member_required
@csrf_protect
def qdrant_admin_page(request):
    QDRANT_URL = (getattr(settings, "QDRANT_BASE", "") or "").rstrip("/")
    msg, err, raw, active_info = None, None, None, None

    if not QDRANT_URL:
        return render(
            request,
            "chat/qdrant_admin.html",
            {
                "qdrant_url": "",
                "collections": [],
                "active_collection": "",
                "active_info": None,
                "raw": None,
                "msg": None,
                "err": "QDRANT_BASE is not set in settings.py",
            },
        )

    # --- POST actions ---
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        selected = (request.POST.get("selected_collection") or "").strip()
        typed = (request.POST.get("collection_name") or "").strip()
        confirm = (request.POST.get("confirm_name") or "").strip()

        name = typed or selected

        # Defensive parsing
        try:
            dims = int(request.POST.get("dims") or "768")
        except ValueError:
            dims = 768

        distance = (request.POST.get("distance") or "Cosine").strip() or "Cosine"

        try:
            name = _validate_collection_name(name)

            if action in ("delete", "recreate") and confirm != name:
                raise ValueError("Confirm name must exactly match the collection name.")

            if action == "create":
                _create_collection(QDRANT_URL, name, dims, distance)
                msg = f"Created collection: {name}"

            elif action == "delete":
                _qdrant_request("DELETE", f"{QDRANT_URL}/collections/{name}", timeout=20)
                msg = f"Deleted collection: {name}"
                # If deleting active, clear it
                if request.session.get("qdrant_active_collection") == name:
                    request.session["qdrant_active_collection"] = ""

            elif action == "recreate":
                # Delete (ignore 404 if it doesn't exist)
                try:
                    _qdrant_request("DELETE", f"{QDRANT_URL}/collections/{name}", timeout=20)
                except Exception as ex:
                    # If it's "not found" Qdrant typically returns 404; allow recreate anyway
                    if " 404" not in str(ex):
                        raise
                _create_collection(QDRANT_URL, name, dims, distance)
                msg = f"Recreated collection: {name}"

            elif action == "set_active":
                request.session["qdrant_active_collection"] = name
                msg = f"Active collection set to: {name}"

            else:
                raise ValueError("Unknown action.")

        except Exception as ex:
            err = str(ex)

    # --- List collections ---
    collections = []
    try:
        r = _qdrant_request("GET", f"{QDRANT_URL}/collections", timeout=10)
        raw = f"HTTP {r.status_code}\n{r.text}"
        data = r.json()
        collections = [c["name"] for c in data.get("result", {}).get("collections", [])]
    except Exception as ex:
        raw = f"EXCEPTION calling {QDRANT_URL}/collections: {repr(ex)}"
        err = err or str(ex)

    # --- Active collection info ---
    active = (request.session.get("qdrant_active_collection") or "").strip()
    if active:
        try:
            r = _qdrant_request("GET", f"{QDRANT_URL}/collections/{active}", timeout=10)
            active_info = r.json()
        except Exception as ex:
            active_info = {"error": str(ex)}

    return render(
        request,
        "chat/qdrant_admin.html",
        {
            "qdrant_url": QDRANT_URL,
            "collections": collections,
            "active_collection": active,
            "active_info": active_info,
            "raw": raw,
            "msg": msg,
            "err": err,
        },
    )
