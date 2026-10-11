from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any, Dict, List

import requests
from django.http import HttpRequest, HttpResponseBadRequest, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from qdrant_client import QdrantClient

from .policy_retrieval import build_policy_response
from .policy_topic_router import (
    build_topic_list_response,
    clean,
    suggest_questions,
)

logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://192.168.1.176:31633")
POLICY_COLLECTION = os.getenv("POLICY_COLLECTION", "buro_policy")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.144:31134")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
POLICY_TOPIC_CATALOG_LIMIT = int(os.getenv("POLICY_TOPIC_CATALOG_LIMIT", "500"))
POLICY_STREAM_DEBUG = os.getenv("POLICY_STREAM_DEBUG", "true").lower() == "true"


def is_bn(text: str) -> bool:
    text = text or ""
    for ch in text:
        if "\u0980" <= ch <= "\u09ff":
            return True
    return False


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL)


def parse_json_body(request: HttpRequest) -> Dict[str, Any]:
    try:
        if request.body:
            return json.loads(request.body.decode("utf-8"))
    except Exception:
        return {}
    return {}


def extract_question(data: Dict[str, Any]) -> str:
    return clean(
        data.get("question")
        or data.get("message")
        or data.get("query")
        or data.get("text")
        or ""
    )


def ollama_embed(text: str) -> List[float]:
    text = clean(text)
    if not text:
        return []

    payload = {
        "model": EMBED_MODEL,
        "prompt": text,
    }

    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json=payload,
            timeout=(10, 180),
        )
        resp.raise_for_status()
        data = resp.json()
        vec = data.get("embedding") or []
        if isinstance(vec, list) and vec:
            return vec
    except Exception as e:
        logger.exception("Ollama embedding failed: %s", e)

    return []


def _point_payload(point: Any) -> Dict[str, Any]:
    if point is None:
        return {}
    if isinstance(point, dict):
        return point.get("payload") or {}
    return getattr(point, "payload", None) or {}


def get_topic_catalog(collection: str) -> List[Dict[str, Any]]:
    qdrant = get_qdrant_client()

    try:
        points, _next = qdrant.scroll(
            collection_name=collection,
            limit=POLICY_TOPIC_CATALOG_LIMIT,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        logger.exception("Failed to load topic catalog: %s", e)
        return []

    out: List[Dict[str, Any]] = []
    seen = set()

    for p in points or []:
        payload = _point_payload(p)
        topic_id = clean(payload.get("topic_id"))
        topic_bn = clean(payload.get("topic_bn"))
        topic_en = clean(payload.get("topic_en"))
        clause_range = clean(payload.get("clause_range"))
        source_file = clean(payload.get("source_file"))

        key = (topic_id, topic_bn, topic_en, clause_range, source_file)
        if key in seen:
            continue
        seen.add(key)

        if topic_id or topic_bn or topic_en:
            out.append(
                {
                    "topic_id": topic_id,
                    "topic_bn": topic_bn,
                    "topic_en": topic_en,
                    "clause_range": clause_range,
                    "source_file": source_file,
                }
            )

    return out


def normalize_citations(citations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for c in citations or []:
        out.append(
            {
                "idx": c.get("idx"),
                "topic_id": clean(c.get("topic_id")),
                "topic_bn": clean(c.get("topic_bn")),
                "topic_en": clean(c.get("topic_en")),
                "clause_range": clean(c.get("clause_range")),
                "source_file": clean(c.get("source_file")),
                "score": c.get("score"),
            }
        )
    return out


def build_success_payload(question: str, collection: str | None = None) -> Dict[str, Any]:
    collection_name = clean(collection) or POLICY_COLLECTION

    topic_list_payload = build_topic_list_response(
        question_raw=question,
        collection=collection_name,
        get_topic_catalog_func=get_topic_catalog,
        is_bn_func=is_bn,
    )
    if topic_list_payload:
        related_questions = topic_list_payload.get("related_questions") or []
        payload = {
            "ok": True,
            "question": question,
            "answer": topic_list_payload.get("answer", ""),
            "detected_topic": {},
            "citations": [],
            "suggestions": related_questions,
            "related_questions": related_questions,
            "topic_list_mode": bool(topic_list_payload.get("topic_list_mode", True)),
            "topics_count": int(topic_list_payload.get("topics_count") or 0),
        }
        if POLICY_STREAM_DEBUG:
            payload["debug"] = {
                "mode": "topic_list",
                "collection": collection_name,
                "topics_count": payload["topics_count"],
            }
        return payload

    query_vector = ollama_embed(question)
    qdrant = get_qdrant_client()

    result = build_policy_response(
        qdrant=qdrant,
        collection=collection_name,
        question_raw=question,
        query_vector=query_vector,
        get_topic_catalog_func=get_topic_catalog,
        is_bn_func=is_bn,
    )

    detected_topic = result.get("detected_topic") or {}
    citations = normalize_citations(result.get("citations") or [])
    suggestions = suggest_questions(detected_topic, question)

    payload = {
        "ok": True,
        "question": question,
        "answer": result.get("answer", ""),
        "detected_topic": detected_topic,
        "citations": citations,
        "suggestions": suggestions,
        "related_questions": suggestions,
        "topic_list_mode": False,
        "topics_count": 0,
    }

    if POLICY_STREAM_DEBUG:
        payload["debug"] = result.get("debug") or {}

    return payload


@csrf_exempt
@require_POST
def policy_ask(request: HttpRequest):
    data = parse_json_body(request)
    question = extract_question(data)
    collection = clean(data.get("collection")) or POLICY_COLLECTION

    if not question:
        return HttpResponseBadRequest("Missing question")

    try:
        payload = build_success_payload(question, collection=collection)
        return JsonResponse(payload, json_dumps_params={"ensure_ascii": False})
    except Exception as e:
        logger.exception("policy_ask failed: %s", e)
        return JsonResponse(
            {
                "ok": False,
                "error": str(e),
                "answer": "A clear policy excerpt was not found.",
            },
            status=500,
            json_dumps_params={"ensure_ascii": False},
        )


@csrf_exempt
@require_POST
def policy_ask_stream(request: HttpRequest):
    data = parse_json_body(request)
    question = extract_question(data)
    collection = clean(data.get("collection")) or POLICY_COLLECTION

    if not question:
        return HttpResponseBadRequest("Missing question")

    def event_stream():
        try:
            start_payload = {
                "ok": True,
                "event": "start",
                "question": question,
            }
            yield f"data: {json.dumps(start_payload, ensure_ascii=False)}\n\n"

            payload = build_success_payload(question, collection=collection)
            answer_payload = {
                "ok": True,
                "event": "answer",
                "answer": payload.get("answer", ""),
                "detected_topic": payload.get("detected_topic") or {},
                "citations": payload.get("citations") or [],
                "suggestions": payload.get("suggestions") or [],
                "related_questions": payload.get("related_questions") or [],
                "topic_list_mode": bool(payload.get("topic_list_mode", False)),
                "topics_count": int(payload.get("topics_count") or 0),
            }
            if POLICY_STREAM_DEBUG:
                answer_payload["debug"] = payload.get("debug") or {}

            yield f"data: {json.dumps(answer_payload, ensure_ascii=False)}\n\n"

            end_payload = {
                "ok": True,
                "event": "end",
            }
            yield f"data: {json.dumps(end_payload, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.exception("policy_ask_stream failed: %s", e)
            err_payload = {
                "ok": False,
                "event": "error",
                "error": str(e),
                "answer": "A clear policy excerpt was not found.",
            }
            yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
