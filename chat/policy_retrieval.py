from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from .policy_topic_router import (
    build_exact_topic_filter,
    clean,
    detect_best_topic,
    filter_hits_by_topic_family,
    format_policy_answer,
    normalize_for_match,
    rewrite_human_question,
    topic_text_matches,
)


def _point_payload(point: Any) -> Dict[str, Any]:
    if point is None:
        return {}
    if isinstance(point, dict):
        return point.get("payload") or {}
    return getattr(point, "payload", None) or {}


def _point_score(point: Any) -> float:
    if point is None:
        return 0.0
    if isinstance(point, dict):
        return float(point.get("score") or 0.0)
    return float(getattr(point, "score", 0.0) or 0.0)


def _payload_text(payload: Dict[str, Any]) -> str:
    for key in ("text", "content", "body", "chunk_text"):
        val = clean(payload.get(key))
        if val:
            return val
    return ""


def _scroll_points(qdrant: QdrantClient, collection: str, qfilter: qm.Filter, limit: int = 20) -> List[Any]:
    try:
        points, _next = qdrant.scroll(
            collection_name=collection,
            scroll_filter=qfilter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return list(points or [])
    except Exception:
        return []


def _collect_ctx_and_citations(points: List[Any]) -> Tuple[str, List[Dict[str, Any]]]:
    texts: List[str] = []
    citations: List[Dict[str, Any]] = []
    seen = set()

    for i, p in enumerate(points or [], start=1):
        payload = _point_payload(p)
        txt = _payload_text(payload)
        if not txt:
            continue

        norm_txt = normalize_for_match(txt)
        if norm_txt in seen:
            continue
        seen.add(norm_txt)

        texts.append(txt)
        citations.append(
            {
                "idx": i,
                "topic_id": clean(payload.get("topic_id") or payload.get("id")),
                "topic_bn": clean(payload.get("topic_bn")),
                "topic_en": clean(payload.get("topic_en")),
                "clause_range": clean(payload.get("clause_range")),
                "source_file": clean(payload.get("source_file")),
                "score": 1.0,
            }
        )

    return "\n\n".join(texts).strip(), citations


def fetch_topic_payload_text(
    qdrant: QdrantClient,
    collection: str,
    detected_topic: Optional[Dict[str, Any]],
    limit: int = 20,
) -> Tuple[str, List[Dict[str, Any]], str]:
    """
    Exact topic fetch with tolerant fallback:
    1) topic_id exact
    2) topic_bn exact
    3) topic_en exact
    4) topic_id as text match
    Returns: ctx, citations, matched_by
    """
    if not detected_topic:
        return "", [], ""

    topic_id = clean(detected_topic.get("topic_id"))
    topic_bn = clean(detected_topic.get("topic_bn"))
    topic_en = clean(detected_topic.get("topic_en"))

    # 1) exact topic_id value
    if topic_id:
        points = _scroll_points(
            qdrant,
            collection,
            qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="topic_id",
                        match=qm.MatchValue(value=topic_id),
                    )
                ]
            ),
            limit=limit,
        )
        ctx, citations = _collect_ctx_and_citations(points)
        if ctx:
            return ctx, citations, "topic_id"

    # 2) exact topic_bn
    if topic_bn:
        points = _scroll_points(
            qdrant,
            collection,
            qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="topic_bn",
                        match=qm.MatchValue(value=topic_bn),
                    )
                ]
            ),
            limit=limit,
        )
        ctx, citations = _collect_ctx_and_citations(points)
        if ctx:
            return ctx, citations, "topic_bn"

    # 3) exact topic_en
    if topic_en:
        points = _scroll_points(
            qdrant,
            collection,
            qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="topic_en",
                        match=qm.MatchValue(value=topic_en),
                    )
                ]
            ),
            limit=limit,
        )
        ctx, citations = _collect_ctx_and_citations(points)
        if ctx:
            return ctx, citations, "topic_en"

    # 4) topic_id text match fallback
    if topic_id:
        points = _scroll_points(
            qdrant,
            collection,
            qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="topic_id",
                        match=qm.MatchText(text=topic_id),
                    )
                ]
            ),
            limit=limit,
        )
        ctx, citations = _collect_ctx_and_citations(points)
        if ctx:
            return ctx, citations, "topic_id_text"

    return "", [], ""


def build_rewritten_query(question_raw: str, detected_topic: Optional[Dict[str, Any]]) -> str:
    parts = [rewrite_human_question(question_raw)]
    if detected_topic:
        if clean(detected_topic.get("topic_bn")):
            parts.append(clean(detected_topic.get("topic_bn")))
        if clean(detected_topic.get("topic_en")):
            parts.append(clean(detected_topic.get("topic_en")))
    return " | ".join([p for p in parts if p])


def search_policy_hits(
    qdrant: QdrantClient,
    collection: str,
    query_vector: List[float],
    detected_topic: Optional[Dict[str, Any]],
    limit: int = 10,
    score_threshold: float = 0.20,
) -> List[Any]:
    qfilter = build_exact_topic_filter(detected_topic)

    kwargs: Dict[str, Any] = {
        "collection_name": collection,
        "query_vector": query_vector,
        "limit": limit,
        "with_payload": True,
        "with_vectors": False,
        "score_threshold": score_threshold,
    }

    if qfilter:
        kwargs["query_filter"] = qm.Filter(**qfilter)

    try:
        hits = qdrant.search(**kwargs)
    except Exception:
        return []

    hits = list(hits or [])
    hits = filter_hits_by_topic_family(hits, detected_topic)
    hits = [h for h in hits if topic_text_matches(h, detected_topic)]
    return hits


def hits_to_context_and_citations(hits: List[Any], max_items: int = 5) -> Tuple[str, List[Dict[str, Any]]]:
    ctx_parts: List[str] = []
    citations: List[Dict[str, Any]] = []
    seen = set()

    for idx, h in enumerate(hits[:max_items], start=1):
        payload = _point_payload(h)
        txt = _payload_text(payload)
        if not txt:
            continue

        norm_txt = normalize_for_match(txt)
        if norm_txt in seen:
            continue
        seen.add(norm_txt)

        ctx_parts.append(txt)
        citations.append(
            {
                "idx": idx,
                "topic_id": clean(payload.get("topic_id") or payload.get("id")),
                "topic_bn": clean(payload.get("topic_bn")),
                "topic_en": clean(payload.get("topic_en")),
                "clause_range": clean(payload.get("clause_range")),
                "source_file": clean(payload.get("source_file")),
                "score": _point_score(h),
            }
        )

    return "\n\n".join(ctx_parts).strip(), citations


def retrieve_policy_context(
    *,
    qdrant: QdrantClient,
    collection: str,
    question_raw: str,
    query_vector: Optional[List[float]],
    get_topic_catalog_func,
) -> Dict[str, Any]:
    detected_topic = detect_best_topic(question_raw, collection, get_topic_catalog_func)
    rewritten_human_query = rewrite_human_question(question_raw)
    rewritten_query = build_rewritten_query(question_raw, detected_topic)
    filter_used = build_exact_topic_filter(detected_topic)

    debug: Dict[str, Any] = {
        "detected_topic": detected_topic,
        "filter_used": filter_used,
        "rewritten_human_query": rewritten_human_query,
        "rewritten_query": rewritten_query,
        "reason": "",
        "retrieval_mode": "",
        "direct_match_by": "",
    }

    # direct deterministic fetch first
    if detected_topic:
        direct_ctx, direct_citations, matched_by = fetch_topic_payload_text(
            qdrant=qdrant,
            collection=collection,
            detected_topic=detected_topic,
            limit=20,
        )
        if direct_ctx:
            debug["reason"] = "direct_topic_payload"
            debug["retrieval_mode"] = "direct"
            debug["direct_match_by"] = matched_by
            return {
                "ctx": direct_ctx,
                "citations": direct_citations,
                "detected_topic": detected_topic,
                "debug": debug,
            }

    if not query_vector:
        debug["reason"] = "empty_query_vector"
        debug["retrieval_mode"] = "none"
        return {
            "ctx": "",
            "citations": [],
            "detected_topic": detected_topic,
            "debug": debug,
        }

    hits = search_policy_hits(
        qdrant=qdrant,
        collection=collection,
        query_vector=query_vector,
        detected_topic=detected_topic,
        limit=10,
        score_threshold=0.20,
    )
    ctx, citations = hits_to_context_and_citations(hits, max_items=5)

    if ctx:
        debug["reason"] = "vector_hits"
        debug["retrieval_mode"] = "vector"
    else:
        debug["reason"] = "empty_context"
        debug["retrieval_mode"] = "vector"

    return {
        "ctx": ctx,
        "citations": citations,
        "detected_topic": detected_topic,
        "debug": debug,
    }


def build_policy_response(
    *,
    qdrant: QdrantClient,
    collection: str,
    question_raw: str,
    query_vector: Optional[List[float]],
    get_topic_catalog_func,
    is_bn_func,
) -> Dict[str, Any]:
    result = retrieve_policy_context(
        qdrant=qdrant,
        collection=collection,
        question_raw=question_raw,
        query_vector=query_vector,
        get_topic_catalog_func=get_topic_catalog_func,
    )

    ctx = result["ctx"]
    citations = result["citations"]
    detected_topic = result["detected_topic"]
    debug = result["debug"]

    if not ctx.strip():
        return {
            "answer": "A clear policy excerpt was not found.",
            "debug": debug,
            "detected_topic": detected_topic,
            "citations": citations,
        }

    answer = format_policy_answer(
        question_raw=question_raw,
        ctx=ctx,
        citations=citations,
        detected_topic=detected_topic,
        is_bn_func=is_bn_func,
    )

    return {
        "answer": answer,
        "debug": debug,
        "detected_topic": detected_topic,
        "citations": citations,
    }
