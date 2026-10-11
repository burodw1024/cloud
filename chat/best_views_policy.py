from __future__ import annotations

import json
import os
import re
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .policy_topic_router import (
    normalize_for_match,
    rewrite_human_question,
    is_followup_request,
    detect_best_topic,
    build_exact_topic_filter,
    topic_text_matches,
    filter_hits_by_topic_family,
    format_policy_answer,
    suggest_questions,
    PREFIX_TOPIC_KEYS,
)

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
    QDRANT_URL: str = _env("QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")
    POLICY_COLLECTION: str = _env("POLICY_COLLECTION", "buro_policy")
    QDRANT_TIMEOUT: float = _env_float("QDRANT_TIMEOUT", "25")

    OLLAMA_URL: str = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    EMBED_MODEL: str = _env("EMBED_MODEL", "nomic-embed-text")
    EMBED_TIMEOUT: float = _env_float("EMBED_TIMEOUT", "120")

    TOP_K: int = _env_int("POLICY_TOP_K", "8")
    SCORE_THRESHOLD: float = _env_float("POLICY_SCORE_THRESHOLD", "0.01")

    MAX_CTX_CHARS: int = _env_int("POLICY_MAX_CTX_CHARS", "25000")
    MAX_EXCERPTS: int = _env_int("POLICY_MAX_EXCERPTS", "20")


CFG = Cfg()


# =========================================================
# Basic helpers
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


def _bn_to_en_digits(text: str) -> str:
    return (text or "").translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))


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


def _qdrant_scroll(collection: str, *, limit: int = 1000, qfilter: Optional[dict] = None) -> List[dict]:
    payload = {"limit": limit, "with_payload": True, "with_vector": False}
    if qfilter:
        payload["filter"] = qfilter

    data = _http_json(
        "POST",
        f"{CFG.QDRANT_URL}/collections/{collection}/points/scroll",
        timeout=CFG.QDRANT_TIMEOUT,
        json_body=payload,
    )
    if not isinstance(data, dict):
        return []
    result = data.get("result") or {}
    points = result.get("points") or []
    return points if isinstance(points, list) else []


# =========================================================
# Topic catalog from Qdrant
# =========================================================
_TOPIC_CACHE: Dict[str, List[Dict[str, str]]] = {}


def _get_topic_catalog(collection: str) -> List[Dict[str, str]]:
    if collection in _TOPIC_CACHE:
        return _TOPIC_CACHE[collection]

    points = _qdrant_scroll(collection, limit=2000)
    catalog: List[Dict[str, str]] = []
    seen: set[str] = set()

    for p in points:
        payload = p.get("payload") or {}
        topic_id = _clean(payload.get("topic_id") or payload.get("id"))
        topic_bn = _clean(payload.get("topic_bn"))
        topic_en = _clean(payload.get("topic_en"))
        clause_range = _clean(payload.get("clause_range"))
        source_file = _clean(payload.get("source_file"))

        key = "|".join([topic_id, topic_bn, topic_en, clause_range, source_file])
        if not key.strip() or key in seen:
            continue
        seen.add(key)

        catalog.append(
            {
                "topic_id": topic_id,
                "topic_bn": topic_bn,
                "topic_en": topic_en,
                "clause_range": clause_range,
                "source_file": source_file,
            }
        )

    _TOPIC_CACHE[collection] = catalog
    return catalog


# =========================================================
# Follow-up session helpers
# =========================================================
def _session_get_last_topic(request) -> Optional[Dict[str, str]]:
    try:
        if not hasattr(request, "session"):
            return None
        v = request.session.get("policy_last_topic")
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _session_set_last_topic(request, topic: Optional[Dict[str, str]]) -> None:
    try:
        if not hasattr(request, "session"):
            return
        if topic:
            request.session["policy_last_topic"] = {
                "topic_id": _clean(topic.get("topic_id")),
                "topic_bn": _clean(topic.get("topic_bn")),
                "topic_en": _clean(topic.get("topic_en")),
                "clause_range": _clean(topic.get("clause_range")),
                "source_file": _clean(topic.get("source_file")),
                "manual_key": _clean(topic.get("manual_key")),
            }
            if topic.get("topic_ids"):
                request.session["policy_last_topic"]["topic_ids"] = topic.get("topic_ids")
            if topic.get("family_intent"):
                request.session["policy_last_topic"]["family_intent"] = topic.get("family_intent")
            request.session.modified = True
    except Exception:
        pass


def _session_clear_last_topic(request) -> None:
    try:
        if not hasattr(request, "session"):
            return
        if "policy_last_topic" in request.session:
            del request.session["policy_last_topic"]
            request.session.modified = True
    except Exception:
        pass


# =========================================================
# Retrieval filtering
# =========================================================
def _score_pass(hit: dict) -> bool:
    try:
        return float(hit.get("score") or 0.0) >= float(CFG.SCORE_THRESHOLD)
    except Exception:
        return True


def _filter_hits(hits: List[dict], detected_topic: Optional[Dict[str, str]]) -> List[dict]:
    out: List[dict] = []
    for h in hits:
        payload = h.get("payload") or {}
        txt = _clean(payload.get("text") or payload.get("content") or payload.get("chunk") or payload.get("prompt"))
        if not txt:
            continue
        if not _score_pass(h):
            continue
        if not topic_text_matches(h, detected_topic):
            continue
        out.append(h)
    return out


def _rewrite_query(question: str, detected_topic: Optional[Dict[str, str]]) -> str:
    q = rewrite_human_question(question)
    if not detected_topic:
        return q

    parts = [q]
    if detected_topic.get("topic_bn"):
        parts.append(_clean(detected_topic.get("topic_bn")))
    if detected_topic.get("topic_en"):
        parts.append(_clean(detected_topic.get("topic_en")))
    return " | ".join([p for p in parts if p])


def _retrieve_hits(
    question_raw: str,
    rewritten_human_query: str,
    detected_topic: Optional[Dict[str, str]],
    collection: str,
    qfilter: Optional[dict],
    top_k: int,
) -> Tuple[str, List[dict]]:
    q_for_embed = _rewrite_query(rewritten_human_query, detected_topic)

    try:
        vec = _ollama_embed(q_for_embed)
    except Exception:
        vec = _ollama_embed(question_raw)

    hits = _qdrant_search(collection, vec, top_k=max(top_k, 20), qfilter=qfilter)
    hits = _filter_hits(hits, detected_topic)
    hits = filter_hits_by_topic_family(hits, detected_topic)

    if not hits and not detected_topic:
        hits = _qdrant_search(collection, vec, top_k=50, qfilter=None)
        hits = _filter_hits(hits, detected_topic)
        hits = filter_hits_by_topic_family(hits, detected_topic)

    if not hits and detected_topic:
        all_points = _qdrant_scroll(collection, limit=2000)
        family_hits: List[dict] = []

        topic_id = _clean(detected_topic.get("topic_id"))
        manual_key = _clean(detected_topic.get("manual_key"))
        topic_ids = [x for x in (detected_topic.get("topic_ids") or []) if _clean(x)]

        for p in all_points:
            payload = p.get("payload") or {}
            hit_topic_id = _clean(payload.get("topic_id") or payload.get("id"))
            txt = _clean(payload.get("text") or payload.get("content") or payload.get("chunk") or payload.get("prompt"))
            if not txt:
                continue

            if topic_ids and hit_topic_id in topic_ids:
                family_hits.append(
                    {
                        "id": p.get("id"),
                        "payload": payload,
                        "score": 1.0,
                    }
                )
            elif manual_key in PREFIX_TOPIC_KEYS and topic_id and hit_topic_id.startswith(topic_id):
                family_hits.append(
                    {
                        "id": p.get("id"),
                        "payload": payload,
                        "score": 1.0,
                    }
                )
            elif manual_key not in PREFIX_TOPIC_KEYS and topic_id and hit_topic_id == topic_id:
                family_hits.append(
                    {
                        "id": p.get("id"),
                        "payload": payload,
                        "score": 1.0,
                    }
                )

        family_hits = _filter_hits(family_hits, detected_topic)
        hits = family_hits[:50]

    return q_for_embed, hits


# =========================================================
# Context building
# =========================================================
def _extract_policy_lines(text: str) -> List[str]:
    raw_lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    pat_struct = re.compile(r"(^[-•])|(^[০-৯]+(\.[০-৯]+){0,4})|(^[0-9]+(\.[0-9]+){0,4})|(^গ্রেড[-\s]*[০-৯0-9]+)")
    structured = [ln for ln in raw_lines if pat_struct.search(ln)]
    return structured if structured else raw_lines


def _dedupe_keep_order(lines: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for ln in lines:
        norm = normalize_for_match(ln)
        if len(norm) < 8:
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
        txt = _clean(payload.get("text") or payload.get("content") or payload.get("chunk") or payload.get("prompt"))
        if not txt:
            continue

        topic_bn = _clean(payload.get("topic_bn"))
        topic_en = _clean(payload.get("topic_en"))
        clause = _clean(payload.get("clause_range"))
        src = _clean(payload.get("source_file") or payload.get("source"))

        lines = _dedupe_keep_order(_extract_policy_lines(txt))
        if not lines:
            continue

        idx += 1
        header = f"[{idx}] source_file={src} clause={clause} topic_bn={topic_bn} topic_en={topic_en}"
        block = header + "\n" + "\n".join(lines) + "\n"
        if total_chars + len(block) > CFG.MAX_CTX_CHARS:
            break

        excerpts.append(block)
        total_chars += len(block)
        citations.append(
            {
                "idx": idx,
                "id": payload.get("topic_id") or payload.get("id") or h.get("id"),
                "source_file": src,
                "clause": clause,
                "topic_bn": topic_bn,
                "topic_en": topic_en,
                "score": h.get("score"),
            }
        )

    return {"context": "\n".join(excerpts).strip(), "citations": citations}


# =========================================================
# Main execution
# =========================================================
def _run_query_common(request, question_raw: str, collection: str, top_k: int) -> Dict[str, Any]:
    followup = is_followup_request(question_raw)
    last_topic = _session_get_last_topic(request)

    rewritten_human_query = rewrite_human_question(question_raw)

    if followup and last_topic:
        detected_topic: Optional[Dict[str, str]] = dict(last_topic)
    else:
        detected_topic = detect_best_topic(rewritten_human_query, collection, _get_topic_catalog)

    if detected_topic:
        _session_set_last_topic(request, detected_topic)
    else:
        _session_clear_last_topic(request)

    qfilter = build_exact_topic_filter(detected_topic)
    q_for_embed, hits = _retrieve_hits(
        question_raw=question_raw,
        rewritten_human_query=rewritten_human_query,
        detected_topic=detected_topic,
        collection=collection,
        qfilter=qfilter,
        top_k=top_k,
    )

    rag = _build_rag_context(hits)
    return {
        "detected_topic": detected_topic,
        "qfilter": qfilter,
        "rewritten_human_query": rewritten_human_query,
        "rewritten_query": q_for_embed,
        "hits": hits,
        "ctx": rag["context"],
        "citations": rag["citations"],
        "followup": followup,
        "last_topic": last_topic,
    }


# =========================================================
# Views
# =========================================================
@csrf_exempt
@require_POST
def policy_ask(request):
    try:
        body = _json_body(request)

        question_raw = _clean(body.get("question") or body.get("query") or body.get("message"))
        if not question_raw:
            return JsonResponse({"ok": False, "error": "Missing question"}, status=400)

        collection = _clean(body.get("collection")) or CFG.POLICY_COLLECTION
        top_k = _safe_int(body.get("top_k"), CFG.TOP_K)

        data = _run_query_common(request, question_raw, collection, top_k)
        ctx = data["ctx"]
        citations = data["citations"]
        detected_topic = data["detected_topic"]

        if not ctx.strip():
            msg = "প্রাসঙ্গিক নীতিমালার তথ্য স্পষ্টভাবে পাওয়া যায়নি।" if _is_bn(question_raw) else "A clear policy excerpt was not found."
            return JsonResponse(
                {
                    "ok": True,
                    "answer": msg,
                    "topic_detected": detected_topic,
                    "suggested_questions": suggest_questions(detected_topic, question_raw),
                    "citations": [],
                    "debug": {
                        "detected_topic": detected_topic,
                        "filter_used": data["qfilter"],
                        "rewritten_human_query": data["rewritten_human_query"],
                        "rewritten_query": data["rewritten_query"],
                        "reason": "empty_context",
                    },
                },
                json_dumps_params={"ensure_ascii": False},
            )

        answer = format_policy_answer(question_raw, ctx, citations, detected_topic, _is_bn)

        return JsonResponse(
            {
                "ok": True,
                "answer": answer,
                "topic_detected": detected_topic,
                "suggested_questions": suggest_questions(detected_topic, question_raw),
                "citations": citations,
                "debug": {
                    "detected_topic": detected_topic,
                    "filter_used": data["qfilter"],
                    "rewritten_human_query": data["rewritten_human_query"],
                    "rewritten_query": data["rewritten_query"],
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
def policy_ask_stream(request):
    return policy_ask(request)
