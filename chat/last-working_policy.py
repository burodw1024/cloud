# chat/views_policy.py
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

JsonT = Union[Dict[str, Any], List[Any]]


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
    # Qdrant
    QDRANT_URL: str = _env("QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")
    POLICY_COLLECTION: str = _env("POLICY_COLLECTION", "buro_policy")
    QDRANT_TIMEOUT: float = _env_float("QDRANT_TIMEOUT", "25")

    # Ollama embeddings
    OLLAMA_URL: str = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    EMBED_MODEL: str = _env("EMBED_MODEL", "nomic-embed-text")
    EMBED_TIMEOUT: float = _env_float("EMBED_TIMEOUT", "120")

    # Retrieval
    TOP_K: int = _env_int("POLICY_TOP_K", "5")
    SCORE_THRESHOLD: float = _env_float("POLICY_SCORE_THRESHOLD", "0.15")  # <-- important

CFG = Cfg()


# ---------------------------
# Helpers
# ---------------------------
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
        raise RuntimeError(f"Invalid JSON from {url}: {e}. Body={r.text[:1200]}")


# ---------------------------
# Embedding
# ---------------------------
def _ollama_embed(text: str) -> List[float]:
    data = _http_json(
        "POST",
        f"{CFG.OLLAMA_URL}/api/embeddings",
        timeout=CFG.EMBED_TIMEOUT,
        json_body={"model": CFG.EMBED_MODEL, "prompt": text},
    )
    if not isinstance(data, dict):
        raise RuntimeError("Embedding response is not a JSON object.")
    emb = data.get("embedding")
    if not isinstance(emb, list) or not emb:
        raise RuntimeError(f"Embedding missing/empty from Ollama response: {data}")
    return [float(x) for x in emb]


# ---------------------------
# Detect vector name (named vectors compatibility)
# ---------------------------
def _detect_vector_name(collection: str) -> Optional[str]:
    info = _http_json("GET", f"{CFG.QDRANT_URL}/collections/{collection}", timeout=CFG.QDRANT_TIMEOUT)
    if not isinstance(info, dict):
        return None

    result = info.get("result") or {}
    params = ((result.get("config") or {}).get("params") or {})
    vectors = params.get("vectors")

    # Unnamed vector
    if isinstance(vectors, dict) and "size" in vectors and "distance" in vectors:
        return None

    # Named vectors
    if isinstance(vectors, dict):
        # common default name
        if "default" in vectors:
            return "default"
        # pick first vector key
        for k, v in vectors.items():
            if isinstance(v, dict) and "size" in v:
                return str(k)
    return None


# ---------------------------
# Topic detection (your JSONL topic_en/topic_bn)
# ---------------------------
TOPIC_RULES = [
    # (topic_key, match_keywords)
    ("staff_classification", ["regular staff", "contract staff", "classification", "শ্রেণী", "শ্রেণিবিভাগ", "কর্মীদের শ্রেণী", "নিয়মিত কর্মী", "চুক্তিভিত্তিক"]),
    ("departments", ["department", "departments", "বিভাগ", "সংস্থার বিভাগ", "ডিপার্টমেন্ট"]),
    ("grades_posts", ["grade", "grades", "গ্রেড", "পদ বিন্যাস", "পদবী", "designation", "grade structure"]),
    ("education_qualification", ["education", "qualification", "যোগ্যতা", "শিক্ষাগত", "স্নাতক", "স্নাতকোত্তর", "hsc", "ssc"]),
    ("recruitment_policy", ["recruitment", "hiring", "appointment", "নিয়োগ", "নিয়োগের নীতিমালা", "বিজ্ঞপ্তি", "আবেদন", "age limit", "বয়সসীমা"]),
    ("recruitment_board", ["recruitment board", "committee", "নিয়োগ বোর্ড", "নিয়োগ কমিটি", "গভর্ণিং বডি"]),
    ("joining_rules", ["joining", "join", "posting", "যোগদান", "নিয়োগপত্র", "কর্মস্থল", "দ্বিতীয় চাকুরী"]),
    ("transfer_policy", ["transfer", "posting change", "বদলী", "পোস্টিং", "৫০ কিমি", "নোটিশ", "ডিসেম্বর বদলী"]),
    ("job_description", ["job description", "duty", "responsibility", "দায়িত্ব", "পরিধি", "জব ডেসক্রিপশন"]),
    ("workdays_hours", ["office hour", "working days", "কর্মদিবস", "অফিস সময়", "রবিবার", "বৃহস্পতিবার", "শুক্রবার", "শনিবার"]),
    ("attendance", ["attendance", "late", "হাজিরা", "উপস্থিতি", "বিলম্ব", "১৫ মিনিট", "৩০ মিনিট"]),
    ("probation", ["probation", "confirmation", "শিক্ষানবিশ", "স্থায়ীকরণ", "৬ মাস", "৩ মাস"]),
    ("responsibilities", ["conduct", "behavior", "দায়িত্ববোধ", "জেন্ডার", "স্বচ্ছতা", "সম্পদ ব্যবহার"]),
]


# Map to EXACT topic_en values used in your JSONL
TOPIC_EN_MAP = {
    "staff_classification": "Staff classification",
    "departments": "Organizational departments",
    "grades_posts": "Grade structure and post hierarchy",
    "education_qualification": "Minimum education qualification by grade",
    "recruitment_policy": "Recruitment policy rules",
    "recruitment_board": "Recruitment board and committee",
    "joining_rules": "Appointment, posting and joining rules",
    "transfer_policy": "Transfer and posting policy",
    "job_description": "Job description and duty scope",
    "workdays_hours": "Working days and office hours",
    "attendance": "Attendance recording and late rules",
    "probation": "Probation period and confirmation",
    "responsibilities": "Employee responsibilities and workplace conduct",
}


def _detect_topic(question: str) -> Optional[str]:
    ql = (question or "").lower()
    for topic_key, kws in TOPIC_RULES:
        for kw in kws:
            if kw.lower() in ql or kw in question:
                return topic_key
    return None


def _rewrite_query(question: str, topic_key: Optional[str]) -> str:
    """
    Rewrite to force better embedding alignment.
    """
    if topic_key == "staff_classification":
        return "কর্মীদের শ্রেণী বিভাগ ১.১.১ নিয়মিত কর্মী ১.১.২ চুক্তিভিত্তিক কর্মী ১.১.৩ সেবা/খন্ডকালীন ১.১.৪ প্রকল্প কর্মী ১.১.৫ পরামর্শক ১.১.৬ উপদেষ্টা"
    if topic_key == "departments":
        return "সংস্থার বিভাগসমূহ ১.২.১ থেকে ১.২.১২ বিভাগ তালিকা"
    if topic_key == "grades_posts":
        return "গ্রেডভুক্তকরণ ও পদ বিন্যাস ১৬টি গ্রেড তালিকা"
    if topic_key == "transfer_policy":
        return "বদলী নীতি ১.৮ বদলী নোটিশ ৫০ কিমি ১০০ কিমি ডিসেম্বর বদলী"
    return question


# ---------------------------
# Qdrant filter (Correct schema)
# ---------------------------
def _build_qdrant_filter(topic_key: Optional[str]) -> Optional[dict]:
    """
    Qdrant expects:
      filter = {"must": [ { "key": "...", "match": {"value": "..."} }, ... ],
                "must_not": [ ... ] }
    """
    if not topic_key:
        return None

    topic_en = TOPIC_EN_MAP.get(topic_key)
    if not topic_en:
        return None

    # Hard lock to one topic
    return {
        "must": [
            {"key": "topic_en", "match": {"value": topic_en}},
        ]
    }


# ---------------------------
# Qdrant search
# ---------------------------
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
    res = (data.get("result") if isinstance(data, dict) else None)
    return res if isinstance(res, list) else []


# ---------------------------
# Output formatting
# ---------------------------
def _extract_numbered_lines(text: str, max_lines: int = 25) -> List[str]:
    """
    Keep clause-like lines: ১.২.১ / 1.2.1 etc
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    out: List[str] = []

    # Bangla digits + dot pattern
    pat_bn = re.compile(r"^[০-৯]+(\.[০-৯]+){1,3}")
    pat_en = re.compile(r"^[0-9]+(\.[0-9]+){1,3}")

    for ln in lines:
        if pat_bn.search(ln) or pat_en.search(ln):
            out.append(ln)

    if not out:
        return lines[: min(max_lines, len(lines))]
    return out[: min(max_lines, len(out))]


def _format_answer(question_raw: str, topic_key: Optional[str], hits: List[dict]) -> str:
    if not hits:
        return "ডেটা অপর্যাপ্ত।"

    # sort by score desc
    def _score(h: dict) -> float:
        try:
            return float(h.get("score") or 0.0)
        except Exception:
            return 0.0

    hits = sorted(hits, key=_score, reverse=True)

    # collect texts
    chunks: List[str] = []
    for h in hits:
        payload = h.get("payload") or {}
        txt = _clean(
            payload.get("text")
            or payload.get("content")
            or payload.get("chunk")
            or payload.get("prompt")
        )
        if txt:
            chunks.append(txt)

    if not chunks:
        return "ডেটা অপর্যাপ্ত।"

    # Department list: show ONLY numbered dept lines
    if topic_key == "departments":
        merged = "\n".join(chunks[:2])
        lines = _extract_numbered_lines(merged, max_lines=40)
        out = ["সংস্থার বিভাগসমূহ:"]
        for i, ln in enumerate(lines, 1):
            out.append(f"{i}. {ln}")
        return "\n".join(out)

    # Staff classification: keep ১.১.১–১.১.৬
    if topic_key == "staff_classification":
        merged = "\n".join(chunks[:2])
        lines = _extract_numbered_lines(merged, max_lines=30)
        out = ["কর্মীদের শ্রেণী বিভাগ (Classification of Staff):"]
        for i, ln in enumerate(lines, 1):
            out.append(f"{i}. {ln}")
        return "\n".join(out)

    # Default: show top 2 chunks only (policy should be short)
    out = ["প্রাসঙ্গিক নীতিমালা অংশ:"]
    for i, c in enumerate(chunks[:2], 1):
        out.append(f"{i}. {c}")
    return "\n".join(out)


# ---------------------------
# API endpoint
# ---------------------------
@csrf_exempt
@require_POST
def policy_ask(request):
    body = _json_body(request)
    question_raw = _clean(body.get("question") or body.get("message"))
    if not question_raw:
        return JsonResponse({"ok": False, "error": "question is required"}, status=400)

    collection = _clean(body.get("collection")) or CFG.POLICY_COLLECTION
    top_k = int(body.get("top_k") or CFG.TOP_K)

    try:
        # detect topic
        topic_key = _detect_topic(question_raw)

        # rewrite for embedding quality
        question_for_embed = _rewrite_query(question_raw, topic_key)

        # embed
        qvec = _ollama_embed(question_for_embed)

        # hard filter
        qfilter = _build_qdrant_filter(topic_key)

        # search
        hits = _qdrant_search(collection, qvec, top_k=top_k, qfilter=qfilter)

        # threshold filter
        if CFG.SCORE_THRESHOLD > 0:
            hits2: List[dict] = []
            for h in hits:
                try:
                    if float(h.get("score") or 0.0) >= float(CFG.SCORE_THRESHOLD):
                        hits2.append(h)
                except Exception:
                    pass
            hits = hits2

        answer = _format_answer(question_raw, topic_key, hits)

        return JsonResponse(
            {
                "ok": True,
                "collection": collection,
                "top_k": top_k,
                "topic_detected": topic_key,
                "answer": answer,
                "debug": {
                    "rewritten_query": question_for_embed,
                    "filter_used": qfilter,
                    "score_threshold": CFG.SCORE_THRESHOLD,
                },
                "matches": [
                    {"id": h.get("id"), "score": h.get("score"), "payload": h.get("payload")}
                    for h in hits[:10]
                ],
            }
        )

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Qdrant search error: {e}"}, status=502)
