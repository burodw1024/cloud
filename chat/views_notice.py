from __future__ import annotations

import json
import os
import re
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

import requests
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from chat.app_config import APP_CONFIG
JsonT = Union[Dict[str, Any], List[Any]]


# =========================================================
# Config
# =========================================================
def _env(name: str, default: str) -> str:
    return (os.getenv(name, default) or "").strip()


def _env_int(name: str, default: str) -> int:
    try:
        return int(_env(name, default))
    except Exception:
        return int(default)


def _env_float(name: str, default: str) -> float:
    try:
        return float(_env(name, default))
    except Exception:
        return float(default)


@dataclass(frozen=True)
class Cfg:
    #QDRANT_URL: str = _env("QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")
    QDRANT_URL: str = APP_CONFIG.qdrant.get_url(check_health=True)
    NOTICE_COLLECTION: str = _env("NOTICE_COLLECTION", "hr_notices")
    QDRANT_TIMEOUT: float = _env_float("QDRANT_TIMEOUT", "25")

    OLLAMA_URL: str = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    EMBED_MODEL: str = _env("EMBED_MODEL", "nomic-embed-text")
    EMBED_TIMEOUT: float = _env_float("EMBED_TIMEOUT", "120")

    TOP_K: int = _env_int("NOTICE_TOP_K", "8")
    SCORE_THRESHOLD: float = _env_float("NOTICE_SCORE_THRESHOLD", "0.01")
    MAX_CTX_CHARS: int = _env_int("NOTICE_MAX_CTX_CHARS", "20000")
    MAX_EXCERPTS: int = _env_int("NOTICE_MAX_EXCERPTS", "8")


CFG = Cfg()


# =========================================================
# Access control
# =========================================================
NOTICE_ACCESS_GROUPS = [
    "notice",
    "notice_user",
    "policy",
    "hr",
    "hr_notice",
]


def _user_can_access_notice(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return user.groups.filter(name__in=NOTICE_ACCESS_GROUPS).exists()


def _forbidden_json():
    return JsonResponse(
        {"ok": False, "error": "You do not have permission to access the Notice module."},
        status=403,
        json_dumps_params={"ensure_ascii": False},
    )


# =========================================================
# Helpers
# =========================================================
def _json_body(request) -> Dict[str, Any]:
    try:
        raw = (request.body or b"{}").decode("utf-8", errors="replace")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _clean(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", _clean(s)).strip()


def _normalize_for_match(text: str) -> str:
    s = _clean(text).lower()
    s = s.replace("–", "-").replace("—", "-").replace("_", " ")
    s = s.replace("/", " ").replace("\\", " ")
    s = s.replace(",", " ").replace("(", " ").replace(")", " ")
    s = re.sub(r"[^0-9a-z\u0980-\u09ff\-\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_bn(text: str) -> bool:
    return bool(re.search(r"[\u0980-\u09FF]", text or ""))


def _safe_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


# =========================================================
# HTTP / Ollama / Qdrant
# =========================================================
def _http_json(method: str, url: str, *, timeout: float, json_body: Optional[dict] = None) -> JsonT:
    r = requests.request(
        method,
        url,
        json=json_body,
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:1200]}")
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Invalid JSON from {url}: {e}")


def _ollama_embed(text: str) -> List[float]:
    data = _http_json(
        "POST",
        f"{CFG.OLLAMA_URL}/api/embeddings",
        timeout=CFG.EMBED_TIMEOUT,
        json_body={"model": CFG.EMBED_MODEL, "prompt": text},
    )
    if not isinstance(data, dict):
        raise RuntimeError("Embedding response is not an object")
    emb = data.get("embedding")
    if not isinstance(emb, list) or not emb:
        raise RuntimeError("Embedding missing from Ollama response")
    return [float(x) for x in emb]


def _detect_vector_name(collection: str) -> Optional[str]:
    info = _http_json("GET", f"{CFG.QDRANT_URL}/collections/{collection}", timeout=CFG.QDRANT_TIMEOUT)
    if not isinstance(info, dict):
        return None

    result = info.get("result") or {}
    params = ((result.get("config") or {}).get("params") or {})
    vectors = params.get("vectors")

    if isinstance(vectors, dict) and "size" in vectors and "distance" in vectors:
        return None

    if isinstance(vectors, dict):
        if "default" in vectors:
            return "default"
        for k, v in vectors.items():
            if isinstance(v, dict) and "size" in v:
                return str(k)

    return None


def _qdrant_search(collection: str, query_vector: List[float], *, top_k: int, qfilter: Optional[dict]) -> List[dict]:
    vector_name = _detect_vector_name(collection)

    if vector_name:
        payload: Dict[str, Any] = {
            "vector": {"name": vector_name, "vector": query_vector},
            "limit": int(top_k),
            "with_payload": True,
            "with_vector": False,
        }
    else:
        payload = {
            "vector": query_vector,
            "limit": int(top_k),
            "with_payload": True,
            "with_vector": False,
        }

    if qfilter:
        payload["filter"] = qfilter

    data = _http_json(
        "POST",
        f"{CFG.QDRANT_URL}/collections/{collection}/points/search",
        timeout=CFG.QDRANT_TIMEOUT,
        json_body=payload,
    )
    result = data.get("result") if isinstance(data, dict) else None
    return result if isinstance(result, list) else []


# =========================================================
# Notice filters
# =========================================================
def _extract_notice_filters(question: str) -> Dict[str, str]:
    q = _clean(question)
    filters: Dict[str, str] = {}

    patterns = [
        r"(?i)\bnotice\s*no\b\s*[:\-]?\s*([A-Za-z0-9/._-]+)",
        r"(?i)\bmemo\s*no\b\s*[:\-]?\s*([A-Za-z0-9/._-]+)",
        r"(?i)\bref(?:erence)?\s*no\b\s*[:\-]?\s*([A-Za-z0-9/._-]+)",
        r"\b([A-Za-z]{2,20}/[A-Za-z]{1,20}/20\d{2}/\d{1,10})\b",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            filters["notice_no"] = m.group(1).strip()
            break

    m = re.search(r"\b(20\d{2})\b", q)
    if m:
        filters["year"] = m.group(1)

    qn = _normalize_for_match(q)
    if "human resources" in qn or re.search(r"\bhr\b", qn):
        filters["department"] = "HR"

    return filters


def _build_notice_filter(filters: Dict[str, str]) -> Optional[dict]:
    must = []

    if filters.get("notice_no"):
        must.append({"key": "notice_no", "match": {"value": filters["notice_no"]}})

    if filters.get("department"):
        must.append({"key": "department", "match": {"value": filters["department"]}})

    if filters.get("year"):
        must.append({"key": "publish_date", "match": {"text": filters["year"]}})

    return {"must": must} if must else None


# =========================================================
# Retrieval filtering
# =========================================================
def _score_pass(hit: dict) -> bool:
    try:
        return float(hit.get("score") or 0.0) >= float(CFG.SCORE_THRESHOLD)
    except Exception:
        return True


def _filter_hits(hits: List[dict]) -> List[dict]:
    out: List[dict] = []
    for h in hits:
        payload = h.get("payload") or {}
        txt = _clean(payload.get("text"))
        if not txt:
            continue
        if not _score_pass(h):
            continue
        out.append(h)
    return out


# =========================================================
# Context builder
# =========================================================
def _dedupe_keep_order(lines: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for ln in lines:
        norm = _normalize_for_match(ln)
        if len(norm) < 5:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(_norm_space(ln))
    return out


def _build_rag_context(hits: List[dict]) -> Dict[str, Any]:
    excerpts: List[str] = []
    citations: List[Dict[str, Any]] = []
    total_chars = 0
    idx = 0

    for h in hits[:CFG.MAX_EXCERPTS]:
        payload = h.get("payload") or {}
        txt = _clean(payload.get("text"))
        if not txt:
            continue

        title = _clean(payload.get("title"))
        notice_no = _clean(payload.get("notice_no"))
        publish_date = _clean(payload.get("publish_date"))
        department = _clean(payload.get("department"))
        section_name = _clean(payload.get("section_name"))
        file_name = _clean(payload.get("file_name"))

        lines = _dedupe_keep_order([ln.strip() for ln in txt.splitlines() if ln.strip()])
        if not lines:
            continue

        idx += 1
        header = (
            f"[{idx}] title={title} notice_no={notice_no} date={publish_date} "
            f"department={department} section={section_name} file={file_name}"
        )
        block = header + "\n" + "\n".join(lines) + "\n"

        if total_chars + len(block) > CFG.MAX_CTX_CHARS:
            break

        excerpts.append(block)
        total_chars += len(block)
        citations.append(
            {
                "idx": idx,
                "document_id": payload.get("document_id"),
                "title": title,
                "notice_no": notice_no,
                "publish_date": publish_date,
                "department": department,
                "section_name": section_name,
                "file_name": file_name,
                "score": h.get("score"),
            }
        )

    return {"context": "\n".join(excerpts).strip(), "citations": citations}


# =========================================================
# Answer formatting
# =========================================================
def _format_simple_answer(summary: str, bullets: List[str], citations: List[Dict[str, Any]]) -> str:
    src = ", ".join([f"[{c['idx']}]" for c in citations[:5]]) if citations else "[1]"
    out = [f"Summary: {summary}", "Notice Extract:"]
    for b in bullets:
        out.append(f"- {b}")
    out.append(f"Sources: {src}")
    return "\n".join(out)


def _format_answer(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> str:
    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]

    if _is_bn(question_raw):
        return _format_simple_answer(
            "প্রাসঙ্গিক নোটিশের অংশ নিচে দেওয়া হলো।",
            lines[:12],
            citations,
        )

    return _format_simple_answer(
        "Relevant notice excerpt is shown below.",
        lines[:12],
        citations,
    )


def _suggest_questions() -> List[str]:
    return [
        "What does the notice say about attendance?",
        "What is the leave submission procedure?",
        "Show notice TMTH/HR/2026/017",
    ]


# =========================================================
# Main query runner
# =========================================================
def _run_notice_query(question_raw: str, collection: str, top_k: int) -> Dict[str, Any]:
    filters = _extract_notice_filters(question_raw)
    qfilter = _build_notice_filter(filters)

    vec = _ollama_embed(question_raw)
    hits = _qdrant_search(collection, vec, top_k=max(top_k, 20), qfilter=qfilter)
    hits = _filter_hits(hits)

    if not hits:
        hits = _qdrant_search(collection, vec, top_k=50, qfilter=None)
        hits = _filter_hits(hits)

    rag = _build_rag_context(hits)
    return {
        "filters": filters,
        "qfilter": qfilter,
        "hits": hits,
        "ctx": rag["context"],
        "citations": rag["citations"],
    }


# =========================================================
# UI
# =========================================================
@login_required
@require_GET
def notice_chat_page(request):
    if not _user_can_access_notice(request.user):
        return render(request, "chat/notice_forbidden.html", status=403)
    return render(request, "chat/notice_chat.html")


# =========================================================
# API
# =========================================================
@csrf_exempt
@require_POST
@login_required
def notice_ask(request):
    if not _user_can_access_notice(request.user):
        return _forbidden_json()

    try:
        body = _json_body(request)
        question_raw = _clean(body.get("question") or body.get("query") or body.get("message"))
        if not question_raw:
            return JsonResponse({"ok": False, "error": "Missing question"}, status=400)

        collection = _clean(body.get("collection")) or CFG.NOTICE_COLLECTION
        top_k = _safe_int(body.get("top_k"), CFG.TOP_K)

        data = _run_notice_query(question_raw, collection, top_k)
        ctx = data["ctx"]
        citations = data["citations"]

        if not ctx.strip():
            msg = "প্রাসঙ্গিক নোটিশ পাওয়া যায়নি।" if _is_bn(question_raw) else "No relevant notice was found."
            return JsonResponse(
                {
                    "ok": True,
                    "answer": msg,
                    "suggested_questions": _suggest_questions(),
                    "citations": [],
                    "debug": {
                        "collection": collection,
                        "filters": data["filters"],
                        "filter_used": data["qfilter"],
                        "reason": "empty_context",
                    },
                },
                json_dumps_params={"ensure_ascii": False},
            )

        answer = _format_answer(question_raw, ctx, citations)

        return JsonResponse(
            {
                "ok": True,
                "answer": answer,
                "suggested_questions": _suggest_questions(),
                "citations": citations,
                "debug": {
                    "collection": collection,
                    "filters": data["filters"],
                    "filter_used": data["qfilter"],
                    "hits_count": len(data["hits"]),
                    "context_chars": len(ctx),
                },
            },
            json_dumps_params={"ensure_ascii": False},
        )

    except Exception as e:
        return JsonResponse(
            {
                "ok": False,
                "error": str(e),
                "trace": traceback.format_exc(),
            },
            status=500,
            json_dumps_params={"ensure_ascii": False},
        )


@csrf_exempt
@require_POST
@login_required
def notice_ask_stream(request):
    return notice_ask(request)
