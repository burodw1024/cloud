from __future__ import annotations

import io
import json
import re
import uuid
from typing import Any, Dict, List, Optional

import requests
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST, require_http_methods

QDRANT_URL = "http://192.168.1.176:31633"

#QDRANT_URL = "http://10.50.1.29:31635"

COLLECTION = "buro_policy"
OLLAMA_URL = "http://192.168.1.144:31134"
EMBED_MODEL = "nomic-embed-text"


def _clean(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _slugify_topic_id(topic_en: str, clause_range: str) -> str:
    s = (topic_en or "untitled").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    c = (clause_range or "").strip().replace(".", "_").replace("-", "_")
    if c:
        return f"policy_{c}_{s}"
    return f"policy_{s}"


def _qdrant_collection_info() -> dict:
    r = requests.get(f"{QDRANT_URL}/collections/{COLLECTION}", timeout=30)
    r.raise_for_status()
    return r.json()


def _detect_vector_name() -> Optional[str]:
    data = _qdrant_collection_info()
    vectors = (
        ((data.get("result") or {}).get("config") or {})
        .get("params", {})
        .get("vectors")
    )

    if isinstance(vectors, dict):
        if "size" in vectors and "distance" in vectors:
            return None
        if "default" in vectors:
            return "default"
        for k, v in vectors.items():
            if isinstance(v, dict) and "size" in v:
                return str(k)
    return None


def _ollama_embed(text: str) -> List[float]:
    resp = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    embedding = data.get("embedding")
    if not isinstance(embedding, list) or not embedding:
        raise RuntimeError(f"Invalid embedding response: {data}")
    return embedding


def _make_vector(embedding: List[float]) -> Any:
    vector_name = _detect_vector_name()
    if vector_name:
        return {vector_name: embedding}
    return embedding


def _scroll_points(limit: int = 300, with_vector: bool = False) -> List[dict]:
    r = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
        json={
            "limit": limit,
            "with_payload": True,
            "with_vector": with_vector,
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    return ((data.get("result") or {}).get("points") or [])


def _find_point_by_topic_id(topic_id: str) -> Optional[dict]:
    r = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
        json={
            "filter": {
                "must": [
                    {"key": "topic_id", "match": {"value": topic_id}},
                ]
            },
            "limit": 50,
            "with_payload": True,
            "with_vector": True,
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    points = ((data.get("result") or {}).get("points") or [])
    if points:
        return points[0]

    points = _scroll_points(limit=500, with_vector=True)
    for p in points:
        payload = p.get("payload") or {}
        current_topic_id = (
            _clean(payload.get("topic_id"))
            or _clean(payload.get("id"))
            or _slugify_topic_id(
                _clean(payload.get("topic_en")),
                _clean(payload.get("clause_range")),
            )
        )
        if current_topic_id == topic_id:
            return p
    return None


def _normalize_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    topic_bn = _clean(raw.get("topic_bn"))
    topic_en = _clean(raw.get("topic_en"))
    clause_range = _clean(raw.get("clause_range"))
    source_file = _clean(raw.get("source_file"))
    domain = _clean(raw.get("domain")) or "HR Policy"
    language = _clean(raw.get("language")) or "bn"
    search_en = _clean(raw.get("search_en"))
    text = _clean(raw.get("text"))

    keywords_bn = raw.get("keywords_bn") or []
    keywords_en = raw.get("keywords_en") or []

    if isinstance(keywords_bn, str):
        keywords_bn = [x.strip() for x in keywords_bn.splitlines() if x.strip()]
    if isinstance(keywords_en, str):
        keywords_en = [x.strip() for x in keywords_en.splitlines() if x.strip()]

    topic_id = _clean(raw.get("topic_id")) or _slugify_topic_id(topic_en, clause_range)

    return {
        "topic_id": topic_id,
        "topic_bn": topic_bn,
        "topic_en": topic_en,
        "clause_range": clause_range,
        "source_file": source_file,
        "domain": domain,
        "language": language,
        "search_en": search_en,
        "keywords_bn": keywords_bn,
        "keywords_en": keywords_en,
        "text": text,
    }


def _upsert_point(
    point_id: Any,
    payload: Dict[str, Any],
    embedding: Optional[List[float]] = None,
    vector: Any = None,
) -> None:
    if vector is None:
        if embedding is None:
            text = _clean(payload.get("text"))
            if not text:
                raise RuntimeError("Text is required for embedding.")
            embedding = _ollama_embed(text)
        vector = _make_vector(embedding)

    body = {
        "points": [
            {
                "id": point_id,
                "vector": vector,
                "payload": payload,
            }
        ]
    }

    r = requests.put(
        f"{QDRANT_URL}/collections/{COLLECTION}/points?wait=true",
        json=body,
        timeout=120,
    )
    r.raise_for_status()


@staff_member_required
def policy_topics_list(request: HttpRequest) -> HttpResponse:
    points = _scroll_points(limit=500, with_vector=False)

    rows: List[Dict[str, Any]] = []
    seen = set()

    for p in points:
        payload = p.get("payload") or {}

        topic_id = (
            _clean(payload.get("topic_id"))
            or _clean(payload.get("id"))
            or _slugify_topic_id(
                _clean(payload.get("topic_en")),
                _clean(payload.get("clause_range")),
            )
        )

        key = (
            topic_id,
            _clean(payload.get("clause_range")),
            _clean(payload.get("source_file")),
        )
        if key in seen:
            continue
        seen.add(key)

        rows.append(
            {
                "point_id": p.get("id"),
                "topic_id": topic_id,
                "topic_bn": _clean(payload.get("topic_bn")),
                "topic_en": _clean(payload.get("topic_en")),
                "clause_range": _clean(payload.get("clause_range")),
                "source_file": _clean(payload.get("source_file")),
                "language": _clean(payload.get("language")),
                "domain": _clean(payload.get("domain")),
            }
        )

    rows.sort(key=lambda x: (x["topic_id"], x["clause_range"], x["source_file"]))
    return render(
        request,
        "admin/policy_admin/topics_list.html",
        {"rows": rows},
    )


@staff_member_required
def policy_topic_new(request: HttpRequest) -> HttpResponse:
    vector_name = _detect_vector_name() or "unnamed"

    if request.method == "POST":
        payload = _normalize_payload(
            {
                "topic_id": request.POST.get("topic_id"),
                "topic_bn": request.POST.get("topic_bn"),
                "topic_en": request.POST.get("topic_en"),
                "clause_range": request.POST.get("clause_range"),
                "source_file": request.POST.get("source_file"),
                "domain": request.POST.get("domain"),
                "language": request.POST.get("language"),
                "search_en": request.POST.get("search_en"),
                "keywords_bn": request.POST.get("keywords_bn"),
                "keywords_en": request.POST.get("keywords_en"),
                "text": request.POST.get("text"),
            }
        )

        if not payload["text"]:
            return render(
                request,
                "admin/policy_admin/topic_new.html",
                {
                    "row": {
                        **payload,
                        "keywords_bn": "\n".join(payload.get("keywords_bn") or []),
                        "keywords_en": "\n".join(payload.get("keywords_en") or []),
                    },
                    "vector_name": vector_name,
                    "error": "Text is required.",
                },
            )

        existing = _find_point_by_topic_id(payload["topic_id"])
        point_id = existing.get("id") if existing else str(uuid.uuid4())

        _upsert_point(point_id=point_id, payload=payload)
        return redirect("policy_topics_list")

    return render(
        request,
        "admin/policy_admin/topic_new.html",
        {
            "row": {
                "topic_id": "",
                "topic_bn": "",
                "topic_en": "",
                "clause_range": "",
                "source_file": "",
                "domain": "HR Policy",
                "language": "bn",
                "search_en": "",
                "keywords_bn": "",
                "keywords_en": "",
                "text": "",
            },
            "vector_name": vector_name,
        },
    )


@staff_member_required
def policy_topic_edit(request: HttpRequest, topic_id: str) -> HttpResponse:
    point = _find_point_by_topic_id(topic_id)
    if not point:
        raise Http404("Policy topic not found.")

    vector_name = _detect_vector_name() or "unnamed"
    payload = point.get("payload") or {}
    point_id = point.get("id")
    existing_vector = point.get("vector")

    current_topic_id = (
        _clean(payload.get("topic_id"))
        or _clean(payload.get("id"))
        or _slugify_topic_id(
            _clean(payload.get("topic_en")),
            _clean(payload.get("clause_range")),
        )
    )

    if request.method == "POST":
        action = request.POST.get("action", "update")

        new_payload = _normalize_payload(
            {
                "topic_id": request.POST.get("topic_id") or current_topic_id,
                "topic_bn": request.POST.get("topic_bn"),
                "topic_en": request.POST.get("topic_en"),
                "clause_range": request.POST.get("clause_range"),
                "source_file": request.POST.get("source_file"),
                "domain": request.POST.get("domain"),
                "language": request.POST.get("language"),
                "search_en": request.POST.get("search_en"),
                "keywords_bn": request.POST.get("keywords_bn"),
                "keywords_en": request.POST.get("keywords_en"),
                "text": request.POST.get("text"),
            }
        )

        old_text = _clean(payload.get("text"))
        new_text = _clean(new_payload.get("text"))

        if action == "reembed":
            _upsert_point(point_id=point_id, payload=new_payload)
        elif action == "update":
            if new_text and (new_text != old_text or not existing_vector):
                _upsert_point(point_id=point_id, payload=new_payload)
            else:
                _upsert_point(point_id=point_id, payload=new_payload, vector=existing_vector)

        return redirect("policy_topics_list")

    kb = payload.get("keywords_bn") or []
    ke = payload.get("keywords_en") or []

    row = {
        "topic_id": current_topic_id,
        "topic_bn": _clean(payload.get("topic_bn")),
        "topic_en": _clean(payload.get("topic_en")),
        "clause_range": _clean(payload.get("clause_range")),
        "source_file": _clean(payload.get("source_file")),
        "domain": _clean(payload.get("domain")),
        "language": _clean(payload.get("language")),
        "search_en": _clean(payload.get("search_en")),
        "keywords_bn": kb if isinstance(kb, str) else "\n".join(kb),
        "keywords_en": ke if isinstance(ke, str) else "\n".join(ke),
        "text": _clean(payload.get("text")),
    }

    return render(
        request,
        "admin/policy_admin/topic_edit.html",
        {
            "topic_id": current_topic_id,
            "row": row,
            "vector_name": vector_name,
        },
    )


@staff_member_required
def policy_topic_clone(request: HttpRequest, topic_id: str) -> HttpResponse:
    point = _find_point_by_topic_id(topic_id)
    if not point:
        raise Http404("Policy topic not found.")

    payload = dict(point.get("payload") or {})
    text = _clean(payload.get("text"))
    if not text:
        raise Http404("Cannot clone topic without text.")

    old_topic_id = (
        _clean(payload.get("topic_id"))
        or _clean(payload.get("id"))
        or _slugify_topic_id(
            _clean(payload.get("topic_en")),
            _clean(payload.get("clause_range")),
        )
    )

    new_topic_id = f"{old_topic_id}_copy"
    payload["topic_id"] = new_topic_id

    if payload.get("topic_en"):
        payload["topic_en"] = f"{_clean(payload.get('topic_en'))} Copy"
    if payload.get("topic_bn"):
        payload["topic_bn"] = f"{_clean(payload.get('topic_bn'))} Copy"

    new_point_id = str(uuid.uuid4())
    _upsert_point(point_id=new_point_id, payload=payload)

    return redirect("policy_topic_edit", topic_id=new_topic_id)


@staff_member_required
@require_POST
def policy_topic_delete(request: HttpRequest, topic_id: str) -> HttpResponse:
    r = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
        json={
            "filter": {
                "must": [
                    {"key": "topic_id", "match": {"value": topic_id}},
                ]
            },
            "limit": 100,
            "with_payload": True,
            "with_vector": False,
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    points = ((data.get("result") or {}).get("points") or [])

    if not points:
        fallback = _find_point_by_topic_id(topic_id)
        if not fallback:
            raise Http404("Topic not found")
        ids = [fallback.get("id")]
    else:
        ids = [p.get("id") for p in points if p.get("id") is not None]

    if not ids:
        raise Http404("Topic not found")

    dr = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/delete?wait=true",
        json={"points": ids},
        timeout=60,
    )
    dr.raise_for_status()

    return redirect("policy_topics_list")


@staff_member_required
@require_http_methods(["GET", "POST"])
def policy_topics_bulk_import(request: HttpRequest) -> HttpResponse:
    vector_name = _detect_vector_name() or "unnamed"
    context: Dict[str, Any] = {"vector_name": vector_name}

    if request.method == "POST":
        jsonl_text = (request.POST.get("jsonl_text") or "").strip()

        if not jsonl_text:
            context["err"] = "No JSONL text provided."
            return render(request, "admin/policy_admin/bulk_import.html", context)

        report: List[Dict[str, Any]] = []
        inserted = 0
        updated = 0

        try:
            lines = io.StringIO(jsonl_text).read().splitlines()
        except Exception as e:
            context["err"] = f"Could not read JSONL text: {e}"
            return render(request, "admin/policy_admin/bulk_import.html", context)

        for i, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("Each JSONL line must be a JSON object.")

                payload = _normalize_payload(raw)

                if not payload.get("text"):
                    raise ValueError("Field 'text' is required.")

                existing = _find_point_by_topic_id(payload["topic_id"])

                if existing:
                    _upsert_point(point_id=existing.get("id"), payload=payload)
                    updated += 1
                    status = "updated"
                else:
                    _upsert_point(point_id=str(uuid.uuid4()), payload=payload)
                    inserted += 1
                    status = "imported"

                report.append(
                    {
                        "line": i,
                        "topic_id": payload.get("topic_id", "-"),
                        "status": status,
                        "error": "",
                    }
                )

            except Exception as e:
                report.append(
                    {
                        "line": i,
                        "topic_id": "-",
                        "status": "failed",
                        "error": str(e),
                    }
                )

        context["msg"] = f"Import completed. Inserted: {inserted}, Updated: {updated}"
        context["report"] = report

    return render(request, "admin/policy_admin/bulk_import.html", context)
