#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BURO Production-Grade Version-Aware RAG Console

Design goals
------------
- No hardcoded policy clause IDs, policy years, amendment names, policy values,
  expected answers, or topic-specific rules.
- Version-aware current / explicit-version / previous / compare behavior.
- Conversation-aware follow-ups without contaminating unrelated new questions.
- Hybrid retrieval: dense vector + local BM25-like lexical search + RRF fusion.
- Cross-encoder reranking of fused candidates before evidence selection.
- LLM evidence selection from authoritative policy records.
- Final answer generated ONLY from authoritative_text.
- Deterministic citation validation + LLM grounding audit.
- Automatic repair once; deterministic recovery for false NOT_FOUND when direct evidence exists.
- Fail closed only when neither validated generation nor direct authoritative evidence can support an answer.
- Standard-library HTTP client only: no `requests` dependency.

Required payload fields
-----------------------
logical_clause_id
version
version_rank
is_current
status
authoritative_text

Optional retrieval-helper fields
--------------------------------
embedding_text, topic, topic_id, aliases, keywords,
direct_questions, scenario_questions, followup_questions
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(
    os.environ.get(
        "BURO_RAG_HOME",
        "/home/datawarehouse/rag/latest-rag/BURO-version-aware-RAG-ready-bundle",
    )
)
QDRANT_URL = os.environ.get("QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3:latest")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-r1:14b")
QUERY_REWRITE_MODEL = os.environ.get("QUERY_REWRITE_MODEL", "qwen3.5:9b").strip()
QUERY_REWRITE_TIMEOUT = int(os.environ.get("QUERY_REWRITE_TIMEOUT", "300"))
ENV_COLLECTION = os.environ.get("COLLECTION", "").strip()
COLLECTION_PREFIX = os.environ.get(
    "COLLECTION_PREFIX", "buro_service_rules_versioned_v"
)

VECTOR_K = int(os.environ.get("VECTOR_K", "40"))
LEXICAL_K = int(os.environ.get("LEXICAL_K", "40"))

# True cross-encoder reranking is served by a rerank-capable HTTP server
# (for example llama.cpp llama-server started with --reranking). Standard
# Ollama /api/embed is NOT a cross-encoder rerank endpoint.
RERANK_URL = os.environ.get("RERANK_URL", "").strip().rstrip("/")
RERANK_MODEL = os.environ.get(
    "RERANK_MODEL", "qllama/bge-reranker-v2-m3:f16"
).strip()
RERANK_CANDIDATES = int(os.environ.get("RERANK_CANDIDATES", "40"))
# RERANK_POOL is retained as a backward-compatible environment alias.
RERANK_TOP_N = int(
    os.environ.get("RERANK_TOP_N", os.environ.get("RERANK_POOL", "14"))
)
RERANK_TIMEOUT = int(os.environ.get("RERANK_TIMEOUT", "180"))
RERANK_MAX_CHARS = int(os.environ.get("RERANK_MAX_CHARS", "4000"))
RERANK_RETRY_MAX_CHARS = int(os.environ.get("RERANK_RETRY_MAX_CHARS", "2200"))
RERANK_REQUIRED = os.environ.get("RERANK_REQUIRED", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
RERANK_ENABLED = (
    bool(RERANK_URL)
    and os.environ.get("RERANK_ENABLED", "true").strip().lower()
    not in {"0", "false", "no", "off"}
)

EVIDENCE_K = int(os.environ.get("EVIDENCE_K", "4"))
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "180"))
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "600"))

BN_TO_ASCII = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

# Routing words are language mechanics, not policy facts.
COMPARE_TERMS = (
    "compare",
    "comparison",
    "difference",
    "different",
    "changed",
    "change",
    "versus",
    " vs ",
    "পার্থক্য",
    "পরিবর্তন",
    "বনাম",
    "আগে এবং এখন",
    "আগে ও এখন",
    "আগের এবং বর্তমান",
    "পুরনো এবং বর্তমান",
)
PREVIOUS_TERMS = (
    "previous",
    "previously",
    "earlier",
    "old rule",
    "before",
    "আগেরটা",
    "আগের নিয়ম",
    "আগের নিয়ম",
    "পুরনো নিয়ম",
    "পুরনো নিয়ম",
    "আগে কী",
    "পূর্বের",
    "আগের policy",
    "আগের পলিসি",
)
CURRENT_TERMS = (
    "current",
    "currently",
    "latest",
    "now",
    "present rule",
    "বর্তমান",
    "সর্বশেষ",
    "এখনকার",
    "এখন কী",
)
ANAPHORA_TERMS = (
    "এক্ষেত্রে",
    "এই ক্ষেত্রে",
    "তার ক্ষেত্রে",
    "তাদের ক্ষেত্রে",
    "ওটার",
    "ওটা",
    "সেটার",
    "সেটা",
    "আগেরটা",
    "আগের নিয়ম",
    "আগের নিয়ম",
    "এখন কী",
    "what about",
    "in that case",
    "for them",
    "for him",
    "for her",
    "previous one",
    "compare it",
    "how about",
)

GENERIC_STOPWORDS = {
    "current",
    "currently",
    "policy",
    "policies",
    "rule",
    "rules",
    "buro",
    "service",
    "according",
    "under",
    "what",
    "which",
    "how",
    "the",
    "for",
    "or",
    "and",
    "বর্তমান",
    "নীতিমালা",
    "অনুযায়ী",
    "অনুযায়ী",
    "ধারা",
    "বল",
    "বলো",
    "কি",
    "কী",
    "এবং",
    "বা",
    "একজন",
    "এর",
    "হলে",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Route:
    intent: str  # current | version | previous | compare
    version: str | None = None
    clause_hint: str | None = None


@dataclass
class TurnState:
    question: str
    standalone: str
    answer: str
    clauses: list[str] = field(default_factory=list)
    intent: str = "current"
    version: str | None = None


@dataclass
class QueryUnderstanding:
    intent: str = "direct"
    tone: str = "informational"
    language: str = "bn"
    topic: str = ""
    category: str = ""
    action: str = ""
    user_facts: dict[str, Any] = field(default_factory=dict)
    needs_history: bool = False
    standalone_question: str = ""
    retrieval_query: str = ""
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# HTTP / JSON helpers
# ---------------------------------------------------------------------------

def normalize_digits(text: str) -> str:
    return (text or "").translate(BN_TO_ASCII)


def http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: int = HTTP_TIMEOUT,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{method} {url}: HTTP {exc.code}: {body[:1200]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url}: {exc}") from exc


def get_json(url: str, timeout: int = HTTP_TIMEOUT) -> dict[str, Any]:
    return http_json("GET", url, None, timeout)


def post_json(
    url: str, payload: dict[str, Any], timeout: int = HTTP_TIMEOUT
) -> dict[str, Any]:
    return http_json("POST", url, payload, timeout)


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def ollama_generate(
    prompt: str,
    *,
    json_mode: bool = False,
    timeout: int = LLM_TIMEOUT,
    model: str | None = None,
) -> str:
    body: dict[str, Any] = {
        "model": model or LLM_MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {"temperature": 0},
    }
    if json_mode:
        body["format"] = "json"

    data = post_json(f"{OLLAMA_URL}/api/generate", body, timeout=timeout)
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return str(data.get("response") or "")


def ollama_embed(text: str) -> list[float]:
    data = post_json(
        f"{OLLAMA_URL}/api/embed",
        {
            "model": EMBED_MODEL,
            "input": text,
            "keep_alive": OLLAMA_KEEP_ALIVE,
        },
        timeout=HTTP_TIMEOUT,
    )
    try:
        vector = data["embeddings"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"Embedding failed: {json.dumps(data, ensure_ascii=False)[:1000]}"
        ) from exc
    if not isinstance(vector, list) or not vector:
        raise RuntimeError("Embedding model returned an empty vector.")
    return vector


# ---------------------------------------------------------------------------
# Cross-encoder reranker helpers
# ---------------------------------------------------------------------------

# Runtime state is set by health_check() or the first rerank request.  When
# RERANK_REQUIRED=false, a failed reranker is disabled for the rest of the
# process and the existing RRF ordering remains available as a safe fallback.
_RERANK_RUNTIME_AVAILABLE: bool | None = None
_RERANK_RUNTIME_ERROR = ""


def rerank_endpoint() -> str:
    """Return the configured rerank endpoint URL."""
    if not RERANK_URL:
        return ""
    lowered = RERANK_URL.lower()
    if lowered.endswith("/rerank") or lowered.endswith("/reranking"):
        return RERANK_URL
    return f"{RERANK_URL}/v1/rerank"


def rerank_document_text(point: dict[str, Any]) -> str:
    """
    Build the text scored by the cross-encoder.

    AUTHORITATIVE_TEXT remains the dominant content. Topic metadata is included
    only as a short retrieval aid; final evidence selection and answer grounding
    still operate on AUTHORITATIVE_TEXT.
    """
    payload = point.get("payload") or {}
    topic = re.sub(r"\s+", " ", str(payload.get("topic") or "")).strip()
    authoritative = re.sub(
        r"\s+",
        " ",
        str(payload.get("authoritative_text") or ""),
    ).strip()

    parts: list[str] = []
    if topic:
        parts.append(f"Topic: {topic[:300]}")
    if authoritative:
        parts.append(f"Policy text: {authoritative}")

    text = "\n".join(parts).strip()
    return text[:RERANK_MAX_CHARS]


def parse_rerank_results(
    data: Any,
    document_count: int,
) -> list[tuple[int, float]]:
    """
    Parse common /v1/rerank response shapes.

    llama.cpp uses:
      {"results": [{"index": 0, "relevance_score": 1.23}, ...]}

    Some compatible servers use "score" instead of "relevance_score" or
    return the result array directly. Higher scores are always treated as more
    relevant; absolute score thresholds are intentionally not used.
    """
    if isinstance(data, dict):
        raw_results = data.get("results")
        if raw_results is None:
            raw_results = data.get("data")
    elif isinstance(data, list):
        raw_results = data
    else:
        raw_results = None

    if not isinstance(raw_results, list):
        raise RuntimeError(
            "Reranker returned no results array: "
            + json.dumps(data, ensure_ascii=False)[:1200]
        )

    parsed: list[tuple[int, float]] = []
    seen: set[int] = set()

    for row in raw_results:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        if not (0 <= index < document_count) or index in seen:
            continue

        raw_score = row.get("relevance_score")
        if raw_score is None:
            raw_score = row.get("score")
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score):
            continue

        seen.add(index)
        parsed.append((index, score))

    if not parsed:
        raise RuntimeError(
            "Reranker returned no usable index/score pairs: "
            + json.dumps(data, ensure_ascii=False)[:1200]
        )

    parsed.sort(key=lambda item: item[1], reverse=True)
    return parsed


def rerank_score_documents(
    query: str,
    documents: list[str],
) -> list[tuple[int, float]]:
    """Score query/document pairs through a true cross-encoder rerank API."""
    if not RERANK_ENABLED:
        return []
    if not documents:
        return []

    endpoint = rerank_endpoint()
    if not endpoint:
        raise RuntimeError("RERANK_URL is empty.")

    payload: dict[str, Any] = {
        "model": RERANK_MODEL,
        "query": query,
        "documents": documents,
        # Request all scores; the client performs the final sort/cut so it can
        # preserve the original RRF tail as a fallback.
        "top_n": len(documents),
    }

    data = post_json(endpoint, payload, timeout=RERANK_TIMEOUT)
    return parse_rerank_results(data, len(documents))


def rerank_candidates(
    question: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Rerank the strongest RRF candidates with bge-reranker-v2-m3.

    The returned list keeps every input candidate. Scored records come first in
    descending cross-encoder order; any unscored records retain their prior RRF
    order. This preserves recall and all existing deterministic fallback logic.
    """
    global _RERANK_RUNTIME_AVAILABLE, _RERANK_RUNTIME_ERROR

    if not RERANK_ENABLED or not candidates:
        return candidates
    if _RERANK_RUNTIME_AVAILABLE is False and not RERANK_REQUIRED:
        return candidates

    limit = min(max(1, RERANK_CANDIDATES), len(candidates))
    scoring_pool = candidates[:limit]
    documents = [rerank_document_text(point) for point in scoring_pool]

    # Never send empty records to a reranker. Keep them in the RRF tail.
    valid_indexes = [i for i, doc in enumerate(documents) if doc]
    if not valid_indexes:
        return candidates

    compact_documents = [documents[i] for i in valid_indexes]

    try:
        compact_scores = rerank_score_documents(question, compact_documents)
    except Exception as exc:
        # llama.cpp rerank/embedding mode requires each query+document pair to fit
        # inside the physical batch. If a long atomic record exceeds that limit,
        # retry once with a smaller deterministic text window before disabling the
        # reranker for the process. The authoritative full text remains untouched
        # for evidence selection, answer generation, and validation.
        message_text = str(exc).lower()
        oversized = (
            "too large to process" in message_text
            or "physical batch size" in message_text
            or "batch size" in message_text
        )
        if oversized and RERANK_RETRY_MAX_CHARS > 0:
            retry_documents = [
                doc[:RERANK_RETRY_MAX_CHARS]
                for doc in compact_documents
            ]
            try:
                compact_scores = rerank_score_documents(question, retry_documents)
            except Exception as retry_exc:
                exc = retry_exc
            else:
                _RERANK_RUNTIME_AVAILABLE = True
                _RERANK_RUNTIME_ERROR = ""
                exc = None

        if exc is not None:
            _RERANK_RUNTIME_AVAILABLE = False
            _RERANK_RUNTIME_ERROR = str(exc)
            message = (
                "Cross-encoder reranker unavailable; falling back to RRF ordering. "
                f"RERANK_URL={RERANK_URL!r}; error={exc}. "
                "RERANK_URL must point to a true /v1/rerank server, not standard "
                "Ollama /api/embed."
            )
            if RERANK_REQUIRED:
                raise RuntimeError(message) from exc
            print(f"WARNING: {message}", file=sys.stderr)
            return candidates

    _RERANK_RUNTIME_AVAILABLE = True
    _RERANK_RUNTIME_ERROR = ""

    scored_original: list[tuple[int, float]] = []
    for compact_index, score in compact_scores:
        if 0 <= compact_index < len(valid_indexes):
            scored_original.append((valid_indexes[compact_index], score))

    scored_original.sort(key=lambda item: item[1], reverse=True)

    out: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()

    for rank, (original_index, score) in enumerate(scored_original, start=1):
        point = dict(scoring_pool[original_index])
        point["_rerank_score"] = score
        point["_rerank_rank"] = rank
        out.append(point)
        seen_indexes.add(original_index)

    # Preserve any candidate not returned/scored by the rerank server.
    for index, point in enumerate(scoring_pool):
        if index not in seen_indexes:
            out.append(point)

    # Preserve candidates outside the rerank window in their RRF order.
    out.extend(candidates[limit:])
    return out


def parse_json_object(raw: str) -> dict[str, Any]:
    """Parse strict JSON, with a safe fallback for models that wrap it."""
    raw = (raw or "").strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


# ---------------------------------------------------------------------------
# Collection / version discovery
# ---------------------------------------------------------------------------

def discover_collection(cli_collection: str | None) -> str:
    if cli_collection:
        return cli_collection
    if ENV_COLLECTION:
        return ENV_COLLECTION

    marker = ROOT / "CURRENT_COLLECTION"
    if marker.exists():
        name = marker.read_text(encoding="utf-8").strip()
        if name:
            return name

    data = get_json(f"{QDRANT_URL}/collections")
    names = [
        str(item.get("name") or "")
        for item in data.get("result", {}).get("collections", [])
    ]

    pattern = re.compile(re.escape(COLLECTION_PREFIX) + r"(\d+)$")
    releases: list[tuple[int, str]] = []
    for name in names:
        match = pattern.fullmatch(name)
        if match:
            releases.append((int(match.group(1)), name))

    if releases:
        return max(releases)[1]
    if len(names) == 1:
        return names[0]

    raise RuntimeError(
        "Cannot determine active Qdrant collection. "
        "Set CURRENT_COLLECTION, COLLECTION, or use --collection."
    )


def scroll_all_payloads(collection: str) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    offset: Any = None

    while True:
        body: dict[str, Any] = {
            "limit": 1000,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset

        data = post_json(
            f"{QDRANT_URL}/collections/{collection}/points/scroll",
            body,
        )
        result = data.get("result", {}) or {}
        batch = result.get("points", []) or []
        points.extend(batch)

        offset = result.get("next_page_offset")
        if offset is None or not batch:
            break

    return points


def discover_versions(points: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    for point in points:
        payload = point.get("payload") or {}
        version = str(payload.get("version") or "").strip()
        if not version:
            continue
        try:
            rank = int(payload.get("version_rank") or 0)
        except (TypeError, ValueError):
            rank = 0

        meta = found.setdefault(
            version,
            {
                "version": version,
                "version_rank": rank,
                "records": 0,
                "current_records": 0,
            },
        )
        meta["version_rank"] = max(meta["version_rank"], rank)
        meta["records"] += 1
        if payload.get("is_current") is True:
            meta["current_records"] += 1

    return sorted(
        found.values(),
        key=lambda row: (row["version_rank"], row["version"]),
    )


def health_check(collection: str) -> tuple[int | None, int, str]:
    global _RERANK_RUNTIME_AVAILABLE, _RERANK_RUNTIME_ERROR

    meta = get_json(f"{QDRANT_URL}/collections/{collection}")
    vectors = (
        meta.get("result", {})
        .get("config", {})
        .get("params", {})
        .get("vectors", {})
    )
    qdrant_dim = vectors.get("size") if isinstance(vectors, dict) else None

    embed_dim = len(ollama_embed("BURO policy health check"))
    if qdrant_dim and qdrant_dim != embed_dim:
        raise RuntimeError(
            f"Embedding dimension {embed_dim} != Qdrant vector size {qdrant_dim}"
        )

    tags = get_json(f"{OLLAMA_URL}/api/tags")
    visible = {
        str(item.get("name") or item.get("model") or "")
        for item in tags.get("models", [])
    }
    if LLM_MODEL not in visible:
        raise RuntimeError(
            f"LLM model {LLM_MODEL!r} is not visible from Ollama. "
            f"Visible models: {sorted(visible)}"
        )
    if QUERY_REWRITE_MODEL not in visible:
        raise RuntimeError(
            f"Query rewrite model {QUERY_REWRITE_MODEL!r} is not visible from Ollama. "
            f"Visible models: {sorted(visible)}"
        )

    # Warm the final answer model.
    warm = ollama_generate(
        "Reply only with: LLM_OK",
        timeout=LLM_TIMEOUT,
        model=LLM_MODEL,
    )
    if not warm.strip():
        raise RuntimeError("LLM warm-up returned an empty response.")

    # Warm the dedicated query-understanding model separately.
    rewrite_warm = ollama_generate(
        'Return JSON only: {"status":"QUERY_OK"}',
        json_mode=True,
        timeout=QUERY_REWRITE_TIMEOUT,
        model=QUERY_REWRITE_MODEL,
    )
    if not rewrite_warm.strip():
        raise RuntimeError("Query rewrite model warm-up returned an empty response.")

    rerank_status = "disabled"
    if RERANK_ENABLED:
        try:
            probe = rerank_score_documents(
                "BURO policy reranker health check",
                [
                    "BURO policy reranker health check",
                    "Unrelated weather forecast text",
                ],
            )
            if not probe:
                raise RuntimeError("Reranker health check returned no scores.")
            _RERANK_RUNTIME_AVAILABLE = True
            _RERANK_RUNTIME_ERROR = ""
            rerank_status = "ready"
        except Exception as exc:
            _RERANK_RUNTIME_AVAILABLE = False
            _RERANK_RUNTIME_ERROR = str(exc)
            rerank_status = f"unavailable: {exc}"
            if RERANK_REQUIRED:
                raise RuntimeError(
                    "Required cross-encoder reranker health check failed. "
                    f"RERANK_URL={RERANK_URL!r}: {exc}"
                ) from exc
            print(
                "WARNING: reranker health check failed; RRF fallback remains active: "
                f"{exc}",
                file=sys.stderr,
            )

    return qdrant_dim, embed_dim, rerank_status


# ---------------------------------------------------------------------------
# Text / query processing
# ---------------------------------------------------------------------------

def normalize_lexeme(token: str) -> str:
    """Light linguistic normalization for retrieval only; never changes policy text."""
    token = token.lower().strip()

    # Conservative English morphology.
    if re.fullmatch(r"[a-z]+(?:-[a-z]+)?", token):
        if token.endswith("'s") and len(token) > 4:
            token = token[:-2]
        elif token.endswith("ies") and len(token) > 5:
            token = token[:-3] + "y"
        elif token.endswith("s") and not token.endswith("ss") and len(token) > 5:
            token = token[:-1]
        return token

    # Conservative Bangla case/suffix normalization.
    if re.fullmatch(r"[\u0980-\u09ff]+", token):
        suffixes = (
            "গুলোতে", "গুলোর", "গুলিকে", "গুলোর", "গুলো",
            "সমূহের", "সমূহ", "দেরকে", "দের", "টির", "টিকে", "টি",
            "গুলির", "গুলি", "য়ের", "ের", "কে", "তে", "রা",
        )
        for suffix in suffixes:
            if token.endswith(suffix) and len(token) - len(suffix) >= 3:
                token = token[:-len(suffix)]
                break
        # Handles forms such as কর্মীর -> কর্মী, conservatively.
        if token.endswith("র") and len(token) >= 5:
            token = token[:-1]
        return token

    return token


def tokenize(text: str) -> list[str]:
    text = normalize_digits(text).lower()
    raw = re.findall(
        r"[a-z]+(?:-[a-z]+)?|\d+(?:\.\d+)*|[\u0980-\u09ff]+",
        text,
    )
    tokens = [normalize_lexeme(token) for token in raw]
    return [
        token
        for token in tokens
        if len(token) > 1 and token not in GENERIC_STOPWORDS
    ]


def extract_clause_hint(text: str) -> str | None:
    refs = re.findall(
        r"(?<!\d)(\d+(?:\.\d+){1,4})(?!\d)",
        normalize_digits(text),
    )
    return refs[0] if refs else None


def is_probably_followup(question: str) -> bool:
    q = question.strip().lower()
    if not q:
        return False

    if any(term in q for term in ANAPHORA_TERMS):
        return True

    words = q.split()
    if len(words) <= 6:
        # Very short questions are contextual only when they do not carry
        # enough lexical content to specify a complete new subject/action.
        content_tokens = tokenize(q)
        return len(set(content_tokens)) <= 3

    return False


def contextualize_followup(question: str, last: TurnState | None) -> str:
    if last is None or not is_probably_followup(question):
        return question

    prompt = f"""
Rewrite NEW QUESTION as a standalone BURO policy question.

Strict rules:
- Preserve the user's language.
- Preserve the NEW QUESTION's new category/condition/qualifier.
- Carry forward the missing policy subject/action/process from PREVIOUS QUESTION.
- Example behavior conceptually: if PREVIOUS QUESTION asks about one category's
  rule for an action and NEW QUESTION asks "what about another category?",
  retain the same action but use the new category.
- If NEW QUESTION asks previous/old, preserve that historical intent.
- If NEW QUESTION asks compare/change, preserve that comparison intent.
- Do NOT invent policy facts, numbers, clauses, versions, years, entitlements,
  conditions, amounts, percentages, or answers.
- Return JSON only.

PREVIOUS QUESTION:
{last.standalone}

NEW QUESTION:
{question}

Return:
{{"standalone":"..."}}
"""
    try:
        obj = parse_json_object(ollama_generate(
            prompt,
            json_mode=True,
            timeout=QUERY_REWRITE_TIMEOUT,
            model=QUERY_REWRITE_MODEL,
        ))
        standalone = str(obj.get("standalone") or "").strip()
    except Exception:
        standalone = ""

    if not standalone:
        # Safe textual fallback preserves both the previous subject and new qualifier.
        return f"{last.standalone} — Follow-up: {question}"

    # Guard against a rewrite that drops the previous subject/action entirely.
    previous_terms = set(tokenize(last.standalone))
    new_terms = set(tokenize(question))
    rewritten_terms = set(tokenize(standalone))

    inherited_terms = previous_terms - new_terms
    if inherited_terms and not (inherited_terms & rewritten_terms):
        return f"{last.standalone} — Follow-up: {question}"

    return standalone


def match_explicit_version(
    text: str,
    versions: list[dict[str, Any]],
) -> str | None:
    q = normalize_digits(text).lower()

    # Exact discovered version labels are authoritative.
    exact = [
        version
        for version in versions
        if str(version["version"]).lower() in q
    ]
    if exact:
        return max(exact, key=lambda row: row["version_rank"])["version"]

    # A user-supplied year is resolved only against actually discovered versions.
    years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", q)
    for year in years:
        candidates = [
            version
            for version in versions
            if year in normalize_digits(str(version["version"]))
        ]
        if candidates:
            return max(
                candidates,
                key=lambda row: row["version_rank"],
            )["version"]

    # "base" is resolved only if a discovered version actually says base.
    if re.search(r"\bbase\b|মূল নীতি|মূল policy", q):
        candidates = [
            version
            for version in versions
            if "base" in str(version["version"]).lower()
        ]
        if candidates:
            return min(
                candidates,
                key=lambda row: row["version_rank"],
            )["version"]

    return None


def contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(term in text for term in terms)


QUERY_INTENTS = {
    "direct",
    "scenario",
    "followup",
    "comparison",
    "procedure",
    "eligibility",
    "calculation",
    "current",
    "previous",
    "version_comparison",
    "ambiguous",
    "out_of_scope",
}

QUERY_TONES = {
    "informational",
    "confirmation_seeking",
    "decision_seeking",
    "analytical",
    "urgent",
    "confused",
    "formal",
    "casual",
}


def understand_query(
    question: str,
    last: TurnState | None,
) -> QueryUnderstanding:
    """
    Dedicated NLU/rewrite stage.

    QUERY_REWRITE_MODEL may understand, classify, and rewrite language, but it is
    explicitly forbidden from answering policy questions or inventing policy facts.
    """
    previous = last.standalone if last is not None else ""

    prompt = f"""
You are the Query Understanding Engine for a high-stakes BURO policy RAG system.

You NEVER answer the policy question.
You ONLY understand, classify, extract USER facts, resolve conversational meaning,
and rewrite the question for retrieval.

CURRENT QUESTION:
{question}

PREVIOUS QUESTION (may be empty):
{previous}

Tasks:
1. Detect intent.
2. Detect conversational tone.
3. Detect language: bn, en, or mixed.
4. Identify the policy topic/subject if stated.
5. Identify employee/category/entity if stated.
6. Identify action/process if stated.
7. Extract facts explicitly supplied by the USER.
8. If this is a follow-up and history exists, rewrite it as a complete standalone question.
9. For a scenario, preserve user facts in standalone_question but create retrieval_query
   for the GOVERNING policy rule.
10. retrieval_query must be short, precise, and suitable for embedding/BM25/reranking.

Allowed intents:
direct, scenario, followup, comparison, procedure, eligibility, calculation,
current, previous, version_comparison, ambiguous, out_of_scope

Allowed tones:
informational, confirmation_seeking, decision_seeking, analytical,
urgent, confused, formal, casual

CRITICAL RULES:
- Do NOT answer the policy question.
- Do NOT invent policy facts.
- Do NOT invent clause IDs, versions, years, notice periods, amounts, percentages,
  eligibility conditions, entitlements, exclusions, or outcomes.
- Facts explicitly supplied by the user are USER FACTS, not POLICY FACTS.
- A hypothetical user number/duration/amount is NOT automatically a policy requirement.
- For scenarios, retrieval_query should retrieve the governing rule rather than force
  the hypothetical value into the query.
- Preserve exact employee/category distinctions.
- Preserve current/previous/comparison intent.
- For follow-ups, use PREVIOUS QUESTION only to restore omitted subject/action/category.
- If there is no usable history and the question depends on missing context,
  set intent="ambiguous" and needs_history=true.
- Return JSON only.

Return exactly:
{{
  "intent": "direct",
  "tone": "informational",
  "language": "bn",
  "topic": "",
  "category": "",
  "action": "",
  "user_facts": {{}},
  "needs_history": false,
  "standalone_question": "",
  "retrieval_query": "",
  "confidence": 0.0
}}
"""

    try:
        raw = ollama_generate(
            prompt,
            json_mode=True,
            timeout=QUERY_REWRITE_TIMEOUT,
            model=QUERY_REWRITE_MODEL,
        )
        obj = parse_json_object(raw)
    except Exception:
        obj = {}

    intent = str(obj.get("intent") or "direct").strip().lower()
    if intent not in QUERY_INTENTS:
        intent = "direct"

    tone = str(obj.get("tone") or "informational").strip().lower()
    if tone not in QUERY_TONES:
        tone = "informational"

    language = str(obj.get("language") or "").strip().lower()
    if language not in {"bn", "en", "mixed"}:
        language = "bn" if looks_bangla(question) else "en"

    user_facts = obj.get("user_facts", {})
    if not isinstance(user_facts, dict):
        user_facts = {}

    standalone = str(obj.get("standalone_question") or "").strip()
    retrieval = str(obj.get("retrieval_query") or "").strip()

    # Deterministic safe fallback if the rewrite model returns malformed/empty output.
    if not standalone:
        standalone = contextualize_followup(question, last)

    if not retrieval:
        retrieval = compact_policy_query(standalone) or standalone

    # A retrieval query is not allowed to be an answer-like paragraph.
    if len(retrieval) > 500:
        retrieval = compact_policy_query(standalone) or standalone[:500]

    try:
        confidence = float(obj.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    needs_history = bool(obj.get("needs_history"))
    if intent == "followup" and last is None:
        needs_history = True

    return QueryUnderstanding(
        intent=intent,
        tone=tone,
        language=language,
        topic=str(obj.get("topic") or "").strip(),
        category=str(obj.get("category") or "").strip(),
        action=str(obj.get("action") or "").strip(),
        user_facts=user_facts,
        needs_history=needs_history,
        standalone_question=standalone,
        retrieval_query=retrieval,
        confidence=confidence,
    )


def route_question(
    original: str,
    standalone: str,
    versions: list[dict[str, Any]],
) -> Route:
    """
    Route using explicit intent in the user's ORIGINAL turn first.

    This is crucial for follow-ups. A rewritten standalone question may inherit
    words such as "current/বর্তমান" from the previous turn, but an explicit
    new-turn request such as "আগের নিয়ম কী ছিল?" must still route to previous.
    """
    original_q = normalize_digits(original).lower()
    standalone_q = normalize_digits(standalone).lower()
    combined_q = original_q + " " + standalone_q

    clause_hint = (
        extract_clause_hint(original)
        or extract_clause_hint(standalone)
    )

    original_version = match_explicit_version(original_q, versions)
    combined_version = match_explicit_version(combined_q, versions)

    # Explicit comparison in the new user turn wins.
    if contains_any(original_q, COMPARE_TERMS):
        return Route(
            "compare",
            version=original_version or combined_version,
            clause_hint=clause_hint,
        )

    # Explicit historical/previous intent in the new turn wins over inherited
    # "current" wording from the prior question.
    if contains_any(original_q, PREVIOUS_TERMS):
        return Route(
            "previous",
            clause_hint=clause_hint,
        )

    # Explicit version request in the new turn.
    if original_version:
        return Route(
            "version",
            version=original_version,
            clause_hint=clause_hint,
        )

    # For rewritten follow-ups, allow comparison/previous/version intent that
    # exists only in the standalone form.
    if contains_any(standalone_q, COMPARE_TERMS):
        return Route(
            "compare",
            version=combined_version,
            clause_hint=clause_hint,
        )

    if contains_any(standalone_q, PREVIOUS_TERMS):
        return Route(
            "previous",
            clause_hint=clause_hint,
        )

    if combined_version:
        return Route(
            "version",
            version=combined_version,
            clause_hint=clause_hint,
        )

    return Route("current", clause_hint=clause_hint)


def expand_queries(question: str) -> list[str]:
    """
    Generate retrieval paraphrases only. The model is forbidden to invent facts.
    Original question is always retained even if expansion fails.
    """
    prompt = f"""
Create up to 3 compact semantic search queries for retrieving the exact BURO
policy rule needed by the QUESTION.

Rules:
- Preserve the exact employee/category/action/benefit/topic in the question.
- You may add Bangla/English synonyms for the SAME concept.
- Do NOT answer the policy question.
- Do NOT invent policy facts, clause IDs, years, amendments, notice periods,
  money amounts, percentages, eligibility conditions, or outcomes.
- Preserve user-provided scenario facts in at least one query when relevant.
- ALSO produce a compact governing-rule query that removes conversational framing
  and hypothetical values when those values are what the policy must evaluate.
- A user-supplied number/amount/duration is NOT necessarily a policy requirement.
  Retrieve the governing rule needed to test that scenario fact.
- Return JSON only.

QUESTION:
{question}

Return:
{{"queries":["...","..."]}}
"""
    queries = [question.strip()]
    try:
        obj = parse_json_object(ollama_generate(
            prompt,
            json_mode=True,
            timeout=QUERY_REWRITE_TIMEOUT,
            model=QUERY_REWRITE_MODEL,
        ))
        extra = obj.get("queries", [])
        if isinstance(extra, list):
            for item in extra:
                item = str(item or "").strip()
                if item and item not in queries:
                    queries.append(item)
                if len(queries) >= 3:
                    break
    except Exception:
        pass
    return queries


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def record_key(point: dict[str, Any]) -> str:
    payload = point.get("payload") or {}
    return str(
        payload.get("record_id")
        or point.get("id")
        or payload.get("qdrant_point_id")
        or ""
    )


def local_filter(
    points: Iterable[dict[str, Any]],
    *,
    intent: str,
    version: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for point in points:
        payload = point.get("payload") or {}
        if intent == "current":
            if payload.get("is_current") is not True:
                continue
        elif intent == "version":
            if str(payload.get("version") or "") != str(version or ""):
                continue
        out.append(point)
    return out


def qdrant_filter(intent: str, version: str | None) -> dict[str, Any]:
    if intent == "current":
        return {"must": [{"key": "is_current", "match": {"value": True}}]}
    if intent == "version":
        return {"must": [{"key": "version", "match": {"value": version}}]}
    return {}


def vector_search(
    collection: str,
    query: str,
    *,
    intent: str,
    version: str | None,
    limit: int = VECTOR_K,
) -> list[dict[str, Any]]:
    body = {
        "query": ollama_embed(query),
        "filter": qdrant_filter(intent, version),
        "limit": limit,
        "with_payload": True,
        "with_vector": False,
    }
    data = post_json(
        f"{QDRANT_URL}/collections/{collection}/points/query",
        body,
    )
    return (data.get("result") or {}).get("points", []) or []


def lexical_rank(
    corpus: list[dict[str, Any]],
    queries: list[str],
    limit: int = LEXICAL_K,
) -> list[dict[str, Any]]:
    """
    BM25-like lexical search over authoritative text plus retrieval helper fields.
    This is intentionally independent from vector retrieval so a semantically
    weak embedding cannot completely hide an exact operative clause.
    """
    query_tokens: list[str] = []
    for query in queries:
        query_tokens.extend(tokenize(query))
    qtf = Counter(query_tokens)

    docs: list[list[str]] = []
    for point in corpus:
        payload = point.get("payload") or {}
        helpers: list[str] = []
        for field_name in (
            "keywords",
            "aliases",
            "direct_questions",
            "scenario_questions",
            "followup_questions",
        ):
            value = payload.get(field_name)
            if isinstance(value, list):
                helpers.extend(str(item) for item in value)

        blob = " ".join(
            [
                str(payload.get("authoritative_text") or ""),
                str(payload.get("embedding_text") or ""),
                str(payload.get("topic") or ""),
                str(payload.get("topic_id") or ""),
                " ".join(helpers),
            ]
        )
        docs.append(tokenize(blob))

    n_docs = max(1, len(docs))
    df = Counter()
    for doc in docs:
        for token in set(doc):
            df[token] += 1

    avg_len = (
        sum(len(doc) for doc in docs) / n_docs
        if docs
        else 1.0
    )
    k1 = 1.2
    b = 0.75

    scored: list[dict[str, Any]] = []
    for point, doc in zip(corpus, docs):
        tf = Counter(doc)
        doc_len = max(1, len(doc))
        score = 0.0

        for term, query_count in qtf.items():
            if term not in tf:
                continue
            idf = math.log(
                1.0 + (n_docs - df[term] + 0.5) / (df[term] + 0.5)
            )
            denom = tf[term] + k1 * (
                1 - b + b * doc_len / max(avg_len, 1.0)
            )
            score += (
                idf
                * ((tf[term] * (k1 + 1)) / denom)
                * min(query_count, 2)
            )

        if score > 0:
            item = dict(point)
            item["_lexical_score"] = score
            scored.append(item)

    scored.sort(
        key=lambda item: float(item.get("_lexical_score") or 0.0),
        reverse=True,
    )
    return scored[:limit]


def reciprocal_rank_fusion(
    rankings: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """
    Generic RRF. It does not depend on policy topic, clause ID, or policy values.
    """
    fused: dict[str, dict[str, Any]] = {}

    for ranking in rankings:
        for rank, point in enumerate(ranking, start=1):
            key = record_key(point)
            if not key:
                continue
            entry = fused.setdefault(
                key,
                {
                    "point": point,
                    "rrf": 0.0,
                    "best_vector_score": None,
                    "best_lexical_score": None,
                },
            )
            entry["rrf"] += 1.0 / (60.0 + rank)

            if point.get("score") is not None:
                value = float(point["score"])
                if (
                    entry["best_vector_score"] is None
                    or value > entry["best_vector_score"]
                ):
                    entry["best_vector_score"] = value

            if point.get("_lexical_score") is not None:
                value = float(point["_lexical_score"])
                if (
                    entry["best_lexical_score"] is None
                    or value > entry["best_lexical_score"]
                ):
                    entry["best_lexical_score"] = value

    out: list[dict[str, Any]] = []
    for entry in fused.values():
        point = dict(entry["point"])
        point["_rrf_score"] = entry["rrf"]
        point["_best_vector_score"] = entry["best_vector_score"]
        point["_best_lexical_score"] = entry["best_lexical_score"]
        out.append(point)

    out.sort(key=lambda item: float(item["_rrf_score"]), reverse=True)
    return out


def hybrid_candidates(
    collection: str,
    all_points: list[dict[str, Any]],
    question: str,
    *,
    intent: str,
    version: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    queries = expand_queries(question)

    # Always add a deterministic policy-focused query for scenario robustness.
    # Example conceptually:
    #   "I am X, I want to do Y with 30 days; is it allowed?"
    # becomes retrieval concepts like:
    #   "X Y notice"
    # without inventing what the required notice period is.
    focused_query = compact_policy_query(question)
    if focused_query and focused_query not in queries:
        queries.append(focused_query)

    rankings: list[list[dict[str, Any]]] = []
    for query in queries:
        try:
            rankings.append(
                vector_search(
                    collection,
                    query,
                    intent=intent,
                    version=version,
                )
            )
        except Exception:
            # Lexical retrieval remains available even if one vector request fails.
            continue

    filtered_corpus = local_filter(
        all_points,
        intent=intent,
        version=version,
    )
    rankings.append(lexical_rank(filtered_corpus, queries))
    fused = reciprocal_rank_fusion(rankings)

    # Keep a broad recall set from hybrid retrieval, then apply the true
    # cross-encoder reranker. The reranked list still contains the full RRF
    # tail so deterministic fallbacks are never starved of candidates.
    keep = max(RERANK_CANDIDATES, RERANK_TOP_N, VECTOR_K, LEXICAL_K)
    candidates = fused[:keep]
    candidates = rerank_candidates(question, candidates)
    return candidates, queries



# Generic conversational/scenario words that should not dominate retrieval
# directness. This list contains language mechanics only, not policy facts.
SCENARIO_NOISE_TERMS = {
    # Bangla conversational framing
    "আমি", "আমার", "আমাকে", "আমরা", "আমাদের",
    "একজন", "হিসেবে", "ধরি", "ধরা", "ধরে",
    "চাই", "চাচ্ছি", "চাচ্ছে", "করতে", "করব", "করবো",
    "পারি", "পারব", "পারবো", "যদি", "তাহলে", "তবে",
    "এটা", "সেটা", "তা", "এই", "ওই", "কি", "কী",
    "অনুযায়ী", "অনুযায়ী", "নীতিমালা", "নীতি",
    "বলুন", "জানান", "জানতে", "চাই",
    # English conversational framing
    "i", "me", "my", "we", "our", "want", "would", "like",
    "can", "could", "may", "if", "then", "according", "policy",
    "please", "tell", "explain", "whether", "allowed",
}


def policy_focus_tokens(question: str) -> list[str]:
    """
    Return policy-bearing tokens for retrieval/directness.

    Scenario numbers and conversational framing are intentionally excluded here.
    They remain present in the ORIGINAL question for final reasoning and numeric
    validation, so this does not discard the user's scenario facts.
    """
    tokens = tokenize(question)
    out: list[str] = []

    for token in tokens:
        norm = normalize_digits(str(token)).strip().lower()

        if not norm:
            continue

        # A hypothetical number such as "30 days" is a fact to evaluate against
        # policy; it should not be required to appear in the governing clause.
        if re.fullmatch(r"\d+(?:\.\d+)*", norm):
            continue

        if norm in SCENARIO_NOISE_TERMS:
            continue

        if norm not in out:
            out.append(norm)

    # If aggressive cleanup leaves too little signal, retain the normal tokenizer.
    return out if len(out) >= 2 else tokenize(question)


def compact_policy_query(question: str) -> str:
    """
    Deterministic retrieval-only query containing the policy-bearing concepts.
    No policy facts, values, clauses, or expected answers are added.
    """
    return " ".join(policy_focus_tokens(question)).strip()


def deterministic_directness(
    question: str,
    point: dict[str, Any],
) -> tuple[float, int, float]:
    """
    Generic evidence directness based only on lexical concepts in the question
    and AUTHORITATIVE_TEXT. Used as a safety fallback when the LLM selector
    is uncertain. It contains no policy-specific clause/value/topic knowledge.
    """
    payload = point.get("payload") or {}
    authoritative = str(payload.get("authoritative_text") or "")

    q_terms = set(policy_focus_tokens(question))
    d_terms = set(tokenize(authoritative))

    if not q_terms or not d_terms:
        return 0.0, 0, 0.0

    overlap = len(q_terms & d_terms)
    coverage = overlap / max(1, len(q_terms))

    # RRF is only a weak tie-breaker; authoritative lexical overlap dominates.
    rrf = float(point.get("_rrf_score") or 0.0)
    score = float(overlap) + (coverage * 2.5) + (rrf * 2.0)
    return score, overlap, coverage


def deterministic_fallback_evidence(
    question: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Safe fallback for selector false-negatives.

    A record qualifies only when AUTHORITATIVE_TEXT itself has meaningful
    lexical overlap with the user's question. Ranking helpers alone cannot
    make a record qualify.
    """
    scored: list[tuple[float, int, float, dict[str, Any]]] = []
    for point in candidates:
        score, overlap, coverage = deterministic_directness(question, point)
        qualifies = (
            (overlap >= 2 and coverage >= 0.30)
            or overlap >= 3
        )
        if qualifies:
            scored.append((score, overlap, coverage, point))

    if not scored:
        return []

    scored.sort(key=lambda item: item[0], reverse=True)
    top_score, _, _, top = scored[0]
    selected = [top]

    # Add only very close same-family evidence. This avoids contaminating a
    # precise question with adjacent but different policy actions.
    top_clause = str((top.get("payload") or {}).get("logical_clause_id") or "")
    top_parts = top_clause.split(".")
    top_family = ".".join(top_parts[:2]) if len(top_parts) >= 2 else top_clause

    for score, _, _, point in scored[1:]:
        if len(selected) >= min(EVIDENCE_K, 2):
            break
        clause = str((point.get("payload") or {}).get("logical_clause_id") or "")
        parts = clause.split(".")
        family = ".".join(parts[:2]) if len(parts) >= 2 else clause
        if family == top_family and score >= (top_score * 0.82):
            selected.append(point)

    return selected



def select_evidence(
    question: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not candidates:
        return []

    deterministic = deterministic_fallback_evidence(question, candidates)
    pool = candidates[:RERANK_TOP_N]

    if deterministic:
        anchor = deterministic[0]
        anchor_key = record_key(anchor)
        if anchor_key and all(record_key(point) != anchor_key for point in pool):
            pool = [anchor] + pool[: max(0, RERANK_TOP_N - 1)]

    def candidate_text(candidate_pool: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for index, point in enumerate(candidate_pool):
            payload = point.get("payload") or {}
            authoritative = re.sub(
                r"\s+",
                " ",
                str(payload.get("authoritative_text") or ""),
            )[:1800]
            topic = re.sub(
                r"\s+",
                " ",
                str(payload.get("topic") or ""),
            )[:220]
            score, overlap, coverage = deterministic_directness(question, point)

            lines.append(
                f"ID={index}\n"
                f"CLAUSE={payload.get('logical_clause_id')}\n"
                f"VERSION={payload.get('version')}\n"
                f"CURRENT={payload.get('is_current')}\n"
                f"TOPIC={topic}\n"
                f"LEXICAL_OVERLAP={overlap}\n"
                f"LEXICAL_COVERAGE={coverage:.3f}\n"
                f"RRF_SCORE={float(point.get('_rrf_score') or 0.0):.6f}\n"
                f"RERANK_SCORE={point.get('_rerank_score')}\n"
                f"RERANK_RANK={point.get('_rerank_rank')}\n"
                f"AUTHORITATIVE_TEXT={authoritative}\n"
            )
        return "\n".join(lines)

    def parse_selection(
        obj: dict[str, Any],
        candidate_pool: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        if not obj:
            return None

        raw = obj.get("selected")
        if not isinstance(raw, list):
            return None

        if obj.get("direct") is False and not raw:
            return []

        indexes: list[int] = []
        for value in raw:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(candidate_pool) and index not in indexes:
                indexes.append(index)
            if len(indexes) >= EVIDENCE_K:
                break

        if not indexes:
            return []
        return [candidate_pool[index] for index in indexes]

    prompt = f"""
You are the evidence selector for a high-stakes organizational policy RAG system.

QUESTION:
{question}

CANDIDATES:
{candidate_text(pool)}

Select at most {EVIDENCE_K} records whose AUTHORITATIVE_TEXT directly supports
the exact answer.

Strict rules:
1. Match the exact subject, employee/category, action, benefit, condition, or process.
2. Prefer an operative rule containing the actual requirement/entitlement/condition.
3. Never substitute a different action/category/topic.
4. Do not reject a record merely because the user's wording differs morphologically
   or mixes Bangla and English.
5. For a SCENARIO, user-supplied facts such as a proposed number, duration, amount,
   date, percentage, or action do NOT need to appear verbatim in AUTHORITATIVE_TEXT.
   Select the governing policy rule that determines whether the proposed scenario
   is compliant, non-compliant, conditional, or otherwise governed.
6. A number stated by the user is not automatically the policy requirement. Prefer
   the operative record that states the actual policy requirement.
7. If a candidate's authoritative text plainly governs the requested category/action,
   treat it as direct evidence even when the hypothetical scenario value differs.
8. Return JSON only.

Return:
{{"selected":[0],"direct":true,"reason":"short reason"}}
"""
    selected: list[dict[str, Any]] | None
    try:
        selected = parse_selection(
            parse_json_object(ollama_generate(prompt, json_mode=True)),
            pool,
        )
    except Exception:
        selected = None

    if selected is None:
        retry_pool = pool[: min(8, len(pool))]
        retry_prompt = f"""
Select direct policy evidence.

QUESTION:
{question}

CANDIDATES:
{candidate_text(retry_pool)}

Choose records only when AUTHORITATIVE_TEXT directly governs the exact
subject/category/action/process in the question.
For scenario questions, the user's proposed number/duration/amount does not need
to be written in the policy; select the operative rule needed to evaluate it.
Do not treat the user's hypothetical value as a policy requirement.
Do not substitute a related action/category/topic.
Return JSON only:
{{"selected":[0],"direct":true}}
"""
        try:
            selected = parse_selection(
                parse_json_object(
                    ollama_generate(retry_prompt, json_mode=True)
                ),
                retry_pool,
            )
        except Exception:
            selected = None

    # Selector false-negative protection:
    # if authoritative text itself strongly overlaps the question, do not throw
    # that evidence away merely because the LLM selector said "none".
    if not selected:
        return deterministic

    if deterministic:
        anchor = deterministic[0]
        anchor_key = record_key(anchor)
        selected_keys = {record_key(point) for point in selected}

        # Ensure the strongest deterministic direct record is present.
        if anchor_key and anchor_key not in selected_keys:
            selected = [anchor] + selected

    # Deduplicate and cap.
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for point in selected:
        key = record_key(point)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(point)
        if len(out) >= EVIDENCE_K:
            break
    return out


def scroll_clause(
    collection: str,
    clause: str,
    *,
    current_only: bool = False,
    version: str | None = None,
) -> list[dict[str, Any]]:
    must: list[dict[str, Any]] = [
        {"key": "logical_clause_id", "match": {"value": clause}}
    ]
    if current_only:
        must.append({"key": "is_current", "match": {"value": True}})
    if version is not None:
        must.append({"key": "version", "match": {"value": version}})

    data = post_json(
        f"{QDRANT_URL}/collections/{collection}/points/scroll",
        {
            "filter": {"must": must},
            "limit": 1000,
            "with_payload": True,
            "with_vector": False,
        },
    )
    points = (data.get("result") or {}).get("points", []) or []
    points.sort(
        key=lambda point: int(
            (point.get("payload") or {}).get("version_rank") or 0
        )
    )
    return points


def unique_clauses(records: Iterable[dict[str, Any]]) -> list[str]:
    clauses: list[str] = []
    for point in records:
        clause = str(
            (point.get("payload") or {}).get("logical_clause_id") or ""
        )
        if clause and clause not in clauses:
            clauses.append(clause)
    return clauses


def previous_records_for_clause(
    collection: str,
    clause: str,
) -> list[dict[str, Any]]:
    """
    Return the immediately preceding NON-CURRENT version of a logical clause.

    We never infer "previous" by taking the second-largest rank across current
    and historical records. Instead:
      1. explicitly exclude is_current=true,
      2. choose the highest version_rank among remaining historical records.

    This remains amendment-independent and works with any future number of
    amendments as long as ingestion maintains version_rank/is_current.
    """
    all_versions = scroll_clause(collection, clause)

    historical = [
        point
        for point in all_versions
        if (point.get("payload") or {}).get("is_current") is not True
    ]
    if not historical:
        return []

    def rank_of(point: dict[str, Any]) -> int:
        try:
            return int(
                (point.get("payload") or {}).get("version_rank") or 0
            )
        except (TypeError, ValueError):
            return 0

    highest_historical_rank = max(rank_of(point) for point in historical)

    return [
        point
        for point in historical
        if rank_of(point) == highest_historical_rank
    ]


def all_versions_for_clauses(
    collection: str,
    clauses: Iterable[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for clause in clauses:
        for point in scroll_clause(collection, clause):
            key = record_key(point)
            if key and key not in seen:
                seen.add(key)
                out.append(point)

    out.sort(
        key=lambda point: (
            int((point.get("payload") or {}).get("version_rank") or 0),
            str((point.get("payload") or {}).get("logical_clause_id") or ""),
        )
    )
    return out


def merge_candidate_points(
    primary: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Merge exact conversation-anchor records with hybrid candidates.

    Anchors do not automatically become evidence; they only enter the selector pool.
    This prevents a short follow-up from losing the prior policy subject while still
    allowing the new category/condition to choose a different applicable record.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Put anchors first so they cannot fall outside the selector window.
    for point in anchors + primary:
        key = record_key(point)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(point)

    return out


def current_anchor_records(
    collection: str,
    clauses: Iterable[str],
) -> list[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for clause in clauses:
        for point in scroll_clause(
            collection,
            clause,
            current_only=True,
        ):
            key = record_key(point)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            anchors.append(point)
    return anchors




def is_scenario_like_question(question: str) -> bool:
    q = normalize_digits(question or "").lower()

    scenario_markers = (
        "আমি ", "আমার ", "আমাকে ", "আমরা ",
        "যদি ", "ধরি ", "ধরে ", "চাই", "করতে চাই",
        "পারি", "পারব", "হলে", "তাহলে",
        "i ", "my ", "we ", "if ", "suppose", "assume",
        "want to", "can i", "would like",
    )

    marker_hit = any(marker in q for marker in scenario_markers)
    numeric_hit = bool(re.search(r"(?<!\w)\d+(?:\.\d+)?(?!\w)", q))
    return marker_hit or numeric_hit


def select_evidence_robust(
    original_question: str,
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """
    For scenario questions, select the governing policy evidence using a compact
    policy-focused query, while preserving the original scenario for final
    answer generation and validation.
    """
    if not candidates:
        return [], original_question

    focused = compact_policy_query(original_question)

    if is_scenario_like_question(original_question) and focused:
        selected = select_evidence(focused, candidates)
        if selected:
            return selected, focused

    selected = select_evidence(original_question, candidates)
    if selected:
        return selected, original_question

    if focused and focused != original_question:
        selected = select_evidence(focused, candidates)
        if selected:
            return selected, focused

    return [], focused or original_question



def build_governing_rule_query(question: str) -> str:
    """
    Build a retrieval-only question for the governing rule.

    This function is forbidden from inventing policy facts, values, clause IDs,
    versions, entitlements, or expected outcomes.
    """
    focused = compact_policy_query(question)

    prompt = f"""
Rewrite the USER SCENARIO into ONE compact BURO policy retrieval question.

Goal:
Retrieve the governing policy rule needed to evaluate the scenario.

Strict rules:
- Preserve the employee/category, action, subject, benefit, condition, or process.
- Remove conversational framing such as "I want", "can I", "what if".
- A number/amount/duration supplied by the user is a proposed scenario fact,
  NOT automatically a policy requirement.
- If the user's number is what must be checked against policy, do not force that
  number into the rewritten retrieval question.
- Do NOT answer the question.
- Do NOT invent any policy value, clause, version, year, amount, duration,
  percentage, eligibility rule, or expected outcome.
- Return JSON only.

USER SCENARIO:
{question}

Return:
{{"query":"..."}}
"""

    try:
        obj = parse_json_object(ollama_generate(prompt, json_mode=True))
        query = str(obj.get("query") or "").strip()
        if query:
            return query
    except Exception:
        pass

    return focused or question


def scenario_fresh_retrieval(
    collection: str,
    all_points: list[dict[str, Any]],
    original_question: str,
    *,
    intent: str,
    version: str | None,
) -> tuple[list[dict[str, Any]], list[str], str]:
    """
    Run a completely fresh retrieval pass using a governing-rule query.

    Candidate generation is repeated; this is different from merely re-running
    evidence selection over a scenario-contaminated candidate pool.
    """
    governing_query = build_governing_rule_query(original_question)

    if not governing_query:
        return [], [], ""

    candidates, queries = hybrid_candidates(
        collection,
        all_points,
        governing_query,
        intent=intent,
        version=version,
    )

    # Select using the governing query, because hypothetical scenario values
    # should not be required in authoritative text.
    evidence = select_evidence(governing_query, candidates)

    return evidence, queries, governing_query


def resolve_current_evidence(
    collection: str,
    all_points: list[dict[str, Any]],
    question: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    if is_scenario_like_question(question):
        fresh_evidence, fresh_queries, governing_query = scenario_fresh_retrieval(
            collection,
            all_points,
            question,
            intent="current",
            version=None,
        )

        scenario_queries: list[str] = []
        if governing_query:
            scenario_queries.append(
                f"scenario governing query: {governing_query}"
            )
        scenario_queries.extend(
            f"scenario governing retrieval: {q}"
            for q in fresh_queries
        )

        if fresh_evidence:
            return fresh_evidence, scenario_queries

        candidates, fallback_queries = hybrid_candidates(
            collection,
            all_points,
            question,
            intent="current",
            version=None,
        )
        evidence, selector_query = select_evidence_robust(
            question,
            candidates,
        )
        if selector_query and selector_query != question:
            fallback_queries = fallback_queries + [
                f"evidence selector query: {selector_query}"
            ]
        return evidence, scenario_queries + [
            f"scenario original fallback: {q}"
            for q in fallback_queries
        ]

    candidates, queries = hybrid_candidates(
        collection,
        all_points,
        question,
        intent="current",
        version=None,
    )
    evidence, selector_query = select_evidence_robust(
        question,
        candidates,
    )
    if selector_query and selector_query != question:
        queries = queries + [f"evidence selector query: {selector_query}"]
    return evidence, queries


def choose_evidence(
    collection: str,
    all_points: list[dict[str, Any]],
    question: str,
    route: Route,
    last: TurnState | None,
    contextual_anchor_clauses: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    # User supplied an exact clause: deterministic exact lookup.
    if route.clause_hint:
        if route.intent == "current":
            return (
                scroll_clause(
                    collection,
                    route.clause_hint,
                    current_only=True,
                ),
                [f"exact clause {route.clause_hint}"],
            )
        if route.intent == "version":
            return (
                scroll_clause(
                    collection,
                    route.clause_hint,
                    version=route.version,
                ),
                [f"exact clause {route.clause_hint}"],
            )
        if route.intent == "compare":
            return (
                scroll_clause(collection, route.clause_hint),
                [f"all versions of clause {route.clause_hint}"],
            )
        if route.intent == "previous":
            return (
                previous_records_for_clause(
                    collection,
                    route.clause_hint,
                ),
                [f"previous version of clause {route.clause_hint}"],
            )

    # Conversational previous/compare must operate on the exact clause(s)
    # from the previous turn. This prevents subject drift.
    if route.intent in ("previous", "compare") and last and last.clauses:
        if route.intent == "previous":
            evidence: list[dict[str, Any]] = []
            for clause in last.clauses[:2]:
                evidence.extend(
                    previous_records_for_clause(collection, clause)
                )
            return evidence, ["conversation clause history"]

        return (
            all_versions_for_clauses(
                collection,
                last.clauses[:2],
            ),
            ["conversation clause comparison"],
        )

    # Explicit version search.
    if route.intent == "version":
        candidates, queries = hybrid_candidates(
            collection,
            all_points,
            question,
            intent="version",
            version=route.version,
        )
        return select_evidence(question, candidates), queries

    # Previous/compare without usable conversation state:
    # resolve the applicable CURRENT logical clause first, then move through
    # its version history. This is much safer than a fresh historical search.
    if route.intent in ("previous", "compare"):
        current_evidence, queries = resolve_current_evidence(
            collection,
            all_points,
            question,
        )
        clauses = unique_clauses(current_evidence)[:2]

        if route.intent == "previous":
            evidence = []
            for clause in clauses:
                evidence.extend(
                    previous_records_for_clause(collection, clause)
                )
            return evidence, queries

        return (
            all_versions_for_clauses(collection, clauses),
            queries,
        )

    # Normal/current contextual search.
    if is_scenario_like_question(question) and not contextual_anchor_clauses:
        scenario_evidence, scenario_queries, governing_query = scenario_fresh_retrieval(
            collection,
            all_points,
            question,
            intent="current",
            version=None,
        )

        debug_queries: list[str] = []
        if governing_query:
            debug_queries.append(
                f"scenario governing query: {governing_query}"
            )
        debug_queries.extend(
            f"scenario governing retrieval: {q}"
            for q in scenario_queries
        )

        if scenario_evidence:
            return scenario_evidence, debug_queries

    candidates, queries = hybrid_candidates(
        collection,
        all_points,
        question,
        intent="current",
        version=None,
    )

    if contextual_anchor_clauses:
        anchors = current_anchor_records(
            collection,
            contextual_anchor_clauses[:2],
        )

        direct_anchor = deterministic_fallback_evidence(
            question,
            anchors,
        )
        if direct_anchor:
            return (
                direct_anchor,
                queries + ["conversation clause anchor: direct match"],
            )

        candidates = merge_candidate_points(
            candidates,
            anchors,
        )
        queries = queries + ["conversation clause anchor"]

    evidence, selector_query = select_evidence_robust(
        question,
        candidates,
    )

    if selector_query and selector_query != question:
        queries = queries + [f"evidence selector query: {selector_query}"]

    return evidence, queries


# ---------------------------------------------------------------------------
# Answer generation / validation
# ---------------------------------------------------------------------------

def build_context(records: list[dict[str, Any]]) -> str:
    chunks: list[str] = []

    for index, point in enumerate(records):
        payload = point.get("payload") or {}
        chunks.append(
            f"RECORD_ID: {index}\n"
            f"CLAUSE: {payload.get('logical_clause_id')}\n"
            f"VERSION: {payload.get('version')}\n"
            f"VERSION_RANK: {payload.get('version_rank')}\n"
            f"CURRENT: {payload.get('is_current')}\n"
            f"STATUS: {payload.get('status')}\n"
            f"AUTHORITATIVE_TEXT:\n"
            f"{payload.get('authoritative_text') or ''}\n"
            f"---"
        )

    return "\n".join(chunks)


def generate_structured_answer(
    question: str,
    route: Route,
    evidence: list[dict[str, Any]],
    *,
    repair_feedback: str | None = None,
) -> dict[str, Any]:
    context = build_context(evidence)

    # Python 3.11-compatible: do not place a backslash escape inside
    # an f-string expression (PEP 701 relaxed this only in Python 3.12+).
    repair_feedback_block = (
        "PREVIOUS VALIDATION FEEDBACK:\n" + repair_feedback
        if repair_feedback
        else ""
    )

    prompt = f"""
You are the final answer engine for a high-stakes BURO organizational policy RAG system.

NON-NEGOTIABLE RULES:
1. Use ONLY AUTHORITATIVE_TEXT in POLICY CONTEXT.
2. Answer the exact subject/action/category asked. Do not switch to a related rule.
3. Do not use general knowledge.
4. A number supplied by the user in a scenario is NOT automatically a policy requirement.
   First identify the governing requirement from AUTHORITATIVE_TEXT, then compare the
   user's proposed scenario against it.
5. For scenario questions, explicitly answer whether the proposed situation is allowed,
   insufficient, excessive, conditional, or otherwise governed ONLY when that conclusion
   follows directly from the supplied AUTHORITATIVE_TEXT.
6. Preserve policy numbers and conditions exactly.
7. Every POLICY claim must cite one or more RECORD_ID values that directly support it.
   User-supplied scenario facts come from QUESTION and do not need to be present in
   AUTHORITATIVE_TEXT. When comparing the scenario to policy, cite the RECORD_ID that
   supports the governing policy requirement.
8. In the human-readable answer, mention the supporting clause and policy version.
9. Do not cite a clause/version not present in POLICY CONTEXT, except a cross-reference
   literally written inside AUTHORITATIVE_TEXT.
9. For historical questions, answer only from the historical evidence supplied.
10. For comparison questions, clearly separate each version and state the actual change.
11. Before setting "not_found": true, inspect EVERY supplied record. If at least one
    AUTHORITATIVE_TEXT directly answers the exact requested subject/category/action/attribute,
    you MUST answer from that record and MUST NOT return not_found.
12. Evidence for a different category or scope does not invalidate a direct matching record.
    Use only the record(s) applicable to the exact category/condition asked.
13. Set "not_found": true only when none of the supplied AUTHORITATIVE_TEXT directly supports
    the requested rule. Never treat disagreement between different scopes/categories as absence.
14. Answer in the user's language. Use clear professional Bangla when the user asks in Bangla.
15. Be concise, precise, and management-ready.

ROUTED_INTENT: {route.intent}
TARGET_VERSION: {route.version or ""}

POLICY CONTEXT:
{context}

QUESTION:
{question}

{repair_feedback_block}

Return JSON only:
{{
  "not_found": false,
  "answer": "final user-facing answer",
  "claims": [
    {{
      "text": "one substantive claim",
      "record_ids": [0]
    }}
  ]
}}
"""

    obj = parse_json_object(ollama_generate(prompt, json_mode=True))
    if not obj:
        return {
            "not_found": True,
            "answer": "Not found in retrieved policy context.",
            "claims": [],
        }

    not_found = bool(obj.get("not_found"))
    answer = str(obj.get("answer") or "").strip()
    claims = obj.get("claims", [])
    if not isinstance(claims, list):
        claims = []

    if not_found or not answer:
        return {
            "not_found": True,
            "answer": "Not found in retrieved policy context.",
            "claims": [],
        }

    return {
        "not_found": False,
        "answer": answer,
        "claims": claims,
    }


def clause_refs(text: str) -> set[str]:
    return set(
        re.findall(
            r"(?<!\d)(\d+(?:\.\d+){1,4})(?!\d)",
            normalize_digits(text),
        )
    )


def deterministic_validation(
    question: str,
    result: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    answer = str(result.get("answer") or "")
    claims = result.get("claims", [])
    valid_record_ids = set(range(len(evidence)))

    payload_clauses: set[str] = set()
    context_crossrefs: set[str] = set()
    context_numbers: set[str] = set()
    version_numbers: set[str] = set()

    number_pattern = r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])"

    for point in evidence:
        payload = point.get("payload") or {}
        clause = normalize_digits(
            str(payload.get("logical_clause_id") or "")
        )
        if clause:
            payload_clauses.add(clause)

        authoritative = str(payload.get("authoritative_text") or "")
        context_crossrefs |= clause_refs(authoritative)
        context_numbers |= set(
            re.findall(number_pattern, normalize_digits(authoritative))
        )
        version_numbers |= set(
            re.findall(
                r"\d+",
                normalize_digits(str(payload.get("version") or "")),
            )
        )

    allowed_clauses = set(payload_clauses) | context_crossrefs
    for clause in list(payload_clauses):
        parts = clause.split(".")
        if len(parts) >= 2:
            allowed_clauses.add(".".join(parts[:2]))
        if len(parts) >= 3:
            allowed_clauses.add(".".join(parts[:3]))

    cited_clauses = clause_refs(answer)
    bad_citations = sorted(cited_clauses - allowed_clauses)

    bad_claim_record_ids: list[Any] = []
    claim_without_support = False

    if not result.get("not_found"):
        for claim in claims:
            if not isinstance(claim, dict):
                claim_without_support = True
                continue
            ids = claim.get("record_ids", [])
            if not isinstance(ids, list) or not ids:
                claim_without_support = True
                continue
            for value in ids:
                try:
                    rid = int(value)
                except (TypeError, ValueError):
                    bad_claim_record_ids.append(value)
                    continue
                if rid not in valid_record_ids:
                    bad_claim_record_ids.append(value)

        if not claims:
            claim_without_support = True

    question_numbers = set(
        re.findall(number_pattern, normalize_digits(question))
    )
    answer_numbers = set(
        re.findall(number_pattern, normalize_digits(answer))
    )

    # Clause components are identifiers, not policy numeric claims.
    for clause in cited_clauses:
        for part in clause.split("."):
            answer_numbers.discard(part)

    source_numbers = (
        context_numbers | question_numbers | version_numbers
    )

    def decimal_value(value: str) -> Decimal | None:
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError):
            return None

    # Allow simple arithmetic explicitly derivable from evidence/question,
    # such as notice shortfall = required days - days already given.
    derived_values: set[Decimal] = set()
    numeric_sources = [
        value
        for value in (decimal_value(x) for x in source_numbers)
        if value is not None
    ]
    # Bound pairwise derivation to a reasonable small set.
    numeric_sources = numeric_sources[:40]
    for left in numeric_sources:
        for right in numeric_sources:
            derived_values.add(left + right)
            derived_values.add(left - right)
            derived_values.add(abs(left - right))
            derived_values.add(left * right)
            if right != 0:
                try:
                    derived_values.add(left / right)
                except Exception:
                    pass

    suspicious_numbers: list[str] = []
    for number in sorted(answer_numbers - source_numbers):
        value = decimal_value(number)
        if value is None or value not in derived_values:
            suspicious_numbers.append(number)

    issues: list[str] = []

    if result.get("not_found") and evidence:
        strongest = max(
            (deterministic_directness(question, point) for point in evidence),
            default=(0.0, 0, 0.0),
            key=lambda item: item[0],
        )
        _, direct_overlap, direct_coverage = strongest
        if (
            (direct_overlap >= 2 and direct_coverage >= 0.30)
            or direct_overlap >= 3
        ):
            issues.append("not_found_despite_direct_evidence")

    if bad_citations:
        issues.append("invented_or_unsupported_clause")
    if not result.get("not_found") and payload_clauses and not cited_clauses:
        issues.append("missing_clause_citation")
    if bad_claim_record_ids:
        issues.append("invalid_claim_record_id")
    if claim_without_support:
        issues.append("claim_without_record_support")
    if suspicious_numbers:
        issues.append("unsupported_numeric_literal")

    return {
        "status": "PASS" if not issues else "FAIL",
        "issues": issues,
        "payload_clauses": sorted(payload_clauses),
        "cited_clauses": sorted(cited_clauses),
        "bad_citations": bad_citations,
        "bad_claim_record_ids": bad_claim_record_ids,
        "suspicious_numbers": suspicious_numbers,
    }


def grounding_audit(
    question: str,
    result: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    context = build_context(evidence)

    prompt = f"""
Audit this high-stakes policy RAG answer.

QUESTION:
{question}

POLICY EVIDENCE:
{context}

PROPOSED ANSWER JSON:
{json.dumps(result, ensure_ascii=False)}

Check all of the following:
- The answer addresses the exact question.
- It does not switch employee category, action, benefit, or process.
- POLICY facts, requirements, entitlements, exclusions, clause/version statements,
  and policy-side numbers must be directly supported by cited RECORD_ID evidence.
- Facts explicitly supplied by the USER in QUESTION are valid scenario inputs and
  do NOT need to appear in AUTHORITATIVE_TEXT.
- A conclusion may compare a user-supplied scenario fact (for example a proposed
  number, duration, amount, date, percentage, or action) against a policy rule from
  cited evidence.
- Simple arithmetic/comparison derived only from QUESTION facts plus cited policy
  evidence is allowed.
- Do NOT mark an answer unsupported merely because the user's hypothetical value is
  absent from AUTHORITATIVE_TEXT.
- Do mark it unsupported if the answer invents a policy requirement, changes a
  category/action, or introduces a factual value absent from both QUESTION and evidence.
- Policy numbers, conditions, entitlements, exclusions, and version statements are faithful.
- No unsupported general knowledge was added.
- If evidence is insufficient to establish the governing policy rule, the answer should be not_found.

Return JSON only:
{{
  "status":"PASS",
  "reason":"short explanation",
  "unsupported_claims":[]
}}
"""
    obj = parse_json_object(ollama_generate(prompt, json_mode=True))
    status = str(obj.get("status") or "FAIL").upper()
    if status not in {"PASS", "FAIL"}:
        status = "FAIL"

    unsupported = obj.get("unsupported_claims", [])
    if not isinstance(unsupported, list):
        unsupported = [str(unsupported)]

    return {
        "status": status,
        "reason": str(obj.get("reason") or ""),
        "unsupported_claims": unsupported,
    }


def looks_bangla(text: str) -> bool:
    bangla = len(re.findall(r"[\u0980-\u09ff]", text or ""))
    latin = len(re.findall(r"[A-Za-z]", text or ""))
    return bangla >= latin


def strongest_direct_evidence(
    question: str,
    evidence: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, float, int, float]:
    best_point = None
    best = (0.0, 0, 0.0)
    for point in evidence:
        score, overlap, coverage = deterministic_directness(question, point)
        if score > best[0]:
            best_point = point
            best = (score, overlap, coverage)
    return best_point, best[0], best[1], best[2]


def is_strong_direct_match(
    question: str,
    evidence: list[dict[str, Any]],
) -> bool:
    _, _, overlap, coverage = strongest_direct_evidence(question, evidence)
    return (
        (overlap >= 3 and coverage >= 0.40)
        or (overlap >= 4)
    )


def has_direct_evidence(
    question: str,
    evidence: list[dict[str, Any]],
) -> bool:
    """
    Use the SAME direct-evidence threshold as deterministic_validation().

    This alignment fixes the previous contradictory state where validation could
    emit `not_found_despite_direct_evidence` but the final fallback used a stricter
    threshold and still returned Not Found.
    """
    _, _, overlap, coverage = strongest_direct_evidence(question, evidence)
    return (
        (overlap >= 2 and coverage >= 0.30)
        or overlap >= 3
    )


def strongest_direct_evidence_index(
    question: str,
    evidence: list[dict[str, Any]],
) -> int | None:
    """Return the index of the strongest authoritative record for this question."""
    best_index: int | None = None
    best_score = float("-inf")
    for index, point in enumerate(evidence):
        score, _, _ = deterministic_directness(question, point)
        if score > best_score:
            best_index = index
            best_score = score
    return best_index


def build_extractive_result(
    question: str,
    route: Route,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Build a validator-compatible result using exact authoritative text only.

    For non-comparison questions only the strongest matching RECORD_ID is cited;
    this avoids claiming that adjacent-category evidence supports the answer.
    """
    answer = build_extractive_answer(question, route, evidence)

    if not evidence or answer == "Not found in retrieved policy context.":
        return {
            "not_found": True,
            "answer": "Not found in retrieved policy context.",
            "claims": [],
        }

    if route.intent == "compare":
        record_ids = list(range(len(evidence)))
    else:
        best_index = strongest_direct_evidence_index(question, evidence)
        record_ids = [best_index] if best_index is not None else [0]

    return {
        "not_found": False,
        "answer": answer,
        "claims": [
            {
                "text": answer,
                "record_ids": record_ids,
            }
        ],
    }


def validation_detects_false_not_found(validation: dict[str, Any]) -> bool:
    issues = validation.get("issues", [])
    return (
        isinstance(issues, list)
        and "not_found_despite_direct_evidence" in issues
    )


def build_extractive_answer(
    question: str,
    route: Route,
    evidence: list[dict[str, Any]],
) -> str:
    """
    Deterministic source-grounded fallback.

    This never invents or paraphrases policy facts. It surfaces the exact
    authoritative_text with clause/version metadata. For management use, this
    is safer than returning Not Found when strong direct evidence exists but
    the LLM generation/audit layer is unstable.
    """
    if not evidence:
        return "Not found in retrieved policy context."

    # For compare, preserve all retrieved versions in version-rank order.
    if route.intent == "compare":
        ordered = sorted(
            evidence,
            key=lambda p: int((p.get("payload") or {}).get("version_rank") or 0),
        )
        chunks = []
        for point in ordered:
            payload = point.get("payload") or {}
            clause = str(payload.get("logical_clause_id") or "")
            version = str(payload.get("version") or "")
            text = str(payload.get("authoritative_text") or "").strip()
            if looks_bangla(question):
                chunks.append(
                    f"ধারা {clause} — Version {version}:\n{text}"
                )
            else:
                chunks.append(
                    f"Clause {clause} — Version {version}:\n{text}"
                )
        return "\n\n".join(chunks)

    # For current/version/previous direct questions, use the strongest evidence.
    point, _, _, _ = strongest_direct_evidence(question, evidence)
    if point is None:
        point = evidence[0]

    payload = point.get("payload") or {}
    clause = str(payload.get("logical_clause_id") or "")
    version = str(payload.get("version") or "")
    text = str(payload.get("authoritative_text") or "").strip()

    if looks_bangla(question):
        return f"ধারা {clause} (Version: {version}) অনুযায়ী:\n{text}"
    return f"According to Clause {clause} (Version: {version}):\n{text}"



def answer_with_validation(
    question: str,
    route: Route,
    evidence: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    if not evidence:
        return (
            "Not found in retrieved policy context.",
            {
                "status": "FAIL_CLOSED",
                "mode": "no_evidence",
                "reason": "No authoritative policy record was selected.",
            },
        )

    def deterministic_recovery(
        *,
        trigger: str,
        previous_deterministic: dict[str, Any],
        previous_grounding: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        """
        Recover from a false NOT_FOUND using exact authoritative text.

        No LLM paraphrase is trusted on this path. The result is accepted only if
        deterministic citation/number/record validation passes.
        """
        if not has_direct_evidence(question, evidence):
            return None

        extractive_result = build_extractive_result(
            question,
            route,
            evidence,
        )
        extractive_validation = deterministic_validation(
            question,
            extractive_result,
            evidence,
        )

        if extractive_validation["status"] != "PASS":
            return None

        return extractive_result["answer"], {
            "status": "PASS",
            "mode": "deterministic_direct_evidence_recovery",
            "reason": (
                "Generation returned/behaved like NOT_FOUND even though selected "
                "authoritative evidence directly matched the question. Exact "
                "authoritative_text was returned instead."
            ),
            "trigger": trigger,
            "deterministic": extractive_validation,
            "previous_generation": {
                "deterministic": previous_deterministic,
                "grounding": previous_grounding,
            },
        }

    # First attempt: management-quality paraphrase.
    result = generate_structured_answer(question, route, evidence)
    deterministic = deterministic_validation(question, result, evidence)
    grounding = grounding_audit(question, result, evidence)

    if (
        deterministic["status"] == "PASS"
        and grounding["status"] == "PASS"
        and not result.get("not_found")
    ):
        return result["answer"], {
            "status": "PASS",
            "mode": "validated",
            "deterministic": deterministic,
            "grounding": grounding,
        }

    # Critical false-NOT_FOUND path. If deterministic validation already proved
    # that direct evidence exists, do not spend another generation turn asking the
    # same model to reconsider. Recover immediately from the authoritative text.
    if result.get("not_found") and validation_detects_false_not_found(deterministic):
        recovered = deterministic_recovery(
            trigger="first_generation_not_found_despite_direct_evidence",
            previous_deterministic=deterministic,
            previous_grounding=grounding,
        )
        if recovered is not None:
            return recovered

    # Second attempt: repair the same evidence-grounded answer.
    feedback = json.dumps(
        {
            "deterministic": deterministic,
            "grounding": grounding,
            "instruction": (
                "Use only the selected AUTHORITATIVE_TEXT for POLICY facts. "
                "Facts explicitly stated by the user in QUESTION are valid scenario "
                "inputs and may be compared with the cited policy rule. Do not require "
                "a user's proposed number/duration/amount to appear in policy text. "
                "Do not return not_found if any supplied record directly governs "
                "the exact subject/category/action/attribute. Evidence for another "
                "category does not invalidate the applicable record. Do not invent "
                "or alter clause numbers, versions, categories, actions, conditions, "
                "or policy numbers."
            ),
        },
        ensure_ascii=False,
    )

    repaired = generate_structured_answer(
        question,
        route,
        evidence,
        repair_feedback=feedback,
    )
    deterministic2 = deterministic_validation(
        question,
        repaired,
        evidence,
    )
    grounding2 = grounding_audit(
        question,
        repaired,
        evidence,
    )

    if (
        deterministic2["status"] == "PASS"
        and grounding2["status"] == "PASS"
        and not repaired.get("not_found")
    ):
        return repaired["answer"], {
            "status": "PASS",
            "mode": "validated_after_repair",
            "deterministic": deterministic2,
            "grounding": grounding2,
        }

    # Same recovery after repair. This directly addresses the observed case:
    # payload clauses existed, both validator layers recognized relevant policy
    # evidence, but the generation still said Not Found.
    if repaired.get("not_found") and validation_detects_false_not_found(deterministic2):
        recovered = deterministic_recovery(
            trigger="repaired_generation_not_found_despite_direct_evidence",
            previous_deterministic=deterministic2,
            previous_grounding=grounding2,
        )
        if recovered is not None:
            return recovered

    # Final deterministic safety fallback for any other unstable generation/audit
    # failure. This preserves the original stricter threshold for cases that were
    # not explicitly classified as false NOT_FOUND.
    if is_strong_direct_match(question, evidence):
        extractive_result = build_extractive_result(
            question,
            route,
            evidence,
        )
        extractive_validation = deterministic_validation(
            question,
            extractive_result,
            evidence,
        )

        if extractive_validation["status"] == "PASS":
            return extractive_result["answer"], {
                "status": "PASS",
                "mode": "deterministic_extractive_fallback",
                "reason": (
                    "LLM answer/audit was unstable, so exact authoritative_text "
                    "was returned instead."
                ),
                "deterministic": extractive_validation,
                "previous_generation": {
                    "deterministic": deterministic2,
                    "grounding": grounding2,
                },
            }

    # Fail closed only after generation, repair, and direct-evidence recovery all
    # fail to establish a safe answer.
    return (
        "Not found in retrieved policy context.",
        {
            "status": "FAIL_CLOSED",
            "mode": "validation_failed_and_no_direct_recovery",
            "reason": (
                "Retrieved evidence could not be safely established as a direct "
                "answer after generation, repair, and deterministic recovery."
            ),
            "deterministic": deterministic2,
            "grounding": grounding2,
        },
    )


# ---------------------------------------------------------------------------
# Console / workflow
# ---------------------------------------------------------------------------

def print_versions(versions: list[dict[str, Any]]) -> None:
    for row in versions:
        print(
            f"  rank={row['version_rank']:<4} "
            f"version={row['version']:<30} "
            f"records={row['records']:<5} "
            f"current_records={row['current_records']}"
        )


def print_evidence(records: list[dict[str, Any]]) -> None:
    if not records:
        print("  <none>")
        return

    for index, point in enumerate(records, start=1):
        payload = point.get("payload") or {}
        rerank_score = point.get("_rerank_score")
        rerank_rank = point.get("_rerank_rank")
        rerank_text = ""
        if rerank_score is not None:
            rerank_text = (
                f" rerank_rank={rerank_rank} "
                f"rerank_score={float(rerank_score):.6f}"
            )

        print(
            f"  {index}. "
            f"clause={payload.get('logical_clause_id')} "
            f"version={payload.get('version')} "
            f"current={payload.get('is_current')} "
            f"status={payload.get('status')}"
            f"{rerank_text}"
        )


def context_free_question_is_complete(question: str) -> bool:
    """
    Determine whether a question can be answered without prior conversation.

    This classifier is about language completeness only. It is forbidden from
    supplying policy facts. A deterministic fallback is used if the LLM fails.
    """
    # Clearly substantial questions can skip the classifier.
    if len(set(tokenize(question))) >= 5:
        return True

    prompt = f"""
Classify whether this BURO policy question is SELF-CONTAINED.

A self-contained question identifies enough of the policy subject/action/process
to retrieve the intended rule without needing a previous conversation.

Examples of INCOMPLETE language patterns:
- "what about temporary employees?"
- "in their case?"
- "what was the previous rule?"
when there is no previous conversation.

A short question can still be COMPLETE if it names a clear policy topic/action.

Do NOT answer the policy question.
Do NOT invent policy facts.
Return JSON only.

QUESTION:
{question}

Return:
{{"complete":true,"reason":"short reason"}}
"""
    try:
        obj = parse_json_object(ollama_generate(
            prompt,
            json_mode=True,
            timeout=QUERY_REWRITE_TIMEOUT,
            model=QUERY_REWRITE_MODEL,
        ))
        if isinstance(obj.get("complete"), bool):
            return bool(obj["complete"])
    except Exception:
        pass

    # Conservative language-only fallback.
    q = question.strip().lower()
    if any(term in q for term in ANAPHORA_TERMS):
        return False
    if re.search(r"(ক্ষেত্রে\s*(কি|কী)|what\s+about|in\s+that\s+case)", q):
        return False
    return len(set(tokenize(question))) >= 3


def ambiguity_response(question: str) -> str:
    if looks_bangla(question):
        return (
            "প্রশ্নটি কোন নীতির/বিষয়ের ক্ষেত্রে তা স্পষ্ট নয়। "
            "অনুগ্রহ করে বিষয় বা কার্যক্রমটি উল্লেখ করুন—আমি তখন সংশ্লিষ্ট "
            "বর্তমান নীতিমালার সঠিক ধারা ও version থেকে উত্তর দেব।"
        )
    return (
        "The policy subject is not clear from this question. "
        "Please specify the rule or action you mean, and I will answer from "
        "the applicable current clause and version."
    )



def process_question(
    collection: str,
    all_points: list[dict[str, Any]],
    versions: list[dict[str, Any]],
    question: str,
    *,
    last: TurnState | None,
    debug: bool,
) -> TurnState:
    # A context-dependent fragment at the beginning of a session must not
    # trigger arbitrary retrieval. Ask for the missing subject instead.
    if last is None and not context_free_question_is_complete(question):
        answer = ambiguity_response(question)

        if debug:
            print("\n" + "=" * 78)
            print("QUESTION   :", question)
            print("INTENT     : ambiguous")
            print("EVIDENCE   : <none>")
            print("\nANSWER:")
            print(answer)
            print("\nVALIDATION:")
            print(json.dumps(
                {
                    "status": "NEEDS_CONTEXT",
                    "mode": "ambiguity_guard",
                    "reason": "Question is not self-contained without prior conversation.",
                },
                ensure_ascii=False,
                indent=2,
            ))
            print("=" * 78)
        else:
            print(answer)

        return TurnState(
            question=question,
            standalone=question,
            answer=answer,
            clauses=[],
            intent="ambiguous",
            version=None,
        )

    understanding = understand_query(question, last)

    # If the rewrite model explicitly says this depends on missing history, fail
    # safely rather than retrieving an arbitrary policy rule.
    if last is None and understanding.needs_history and understanding.intent in {"followup", "ambiguous"}:
        answer = ambiguity_response(question)

        if debug:
            print("\n" + "=" * 78)
            print("QUESTION   :", question)
            print("NLU        :", json.dumps(understanding.__dict__, ensure_ascii=False))
            print("INTENT     : ambiguous")
            print("EVIDENCE   : <none>")
            print("\nANSWER:")
            print(answer)
            print("=" * 78)
        else:
            print(answer)

        return TurnState(
            question=question,
            standalone=understanding.standalone_question or question,
            answer=answer,
            clauses=[],
            intent="ambiguous",
            version=None,
        )

    contextual = bool(
        last is not None
        and (
            understanding.intent == "followup"
            or understanding.needs_history
            or is_probably_followup(question)
        )
    )

    standalone = understanding.standalone_question or contextualize_followup(question, last)
    retrieval_query = understanding.retrieval_query or compact_policy_query(standalone) or standalone
    route = route_question(question, standalone, versions)

    anchor_clauses: list[str] = []
    if (
        contextual
        and route.intent == "current"
        and last is not None
        and last.clauses
    ):
        anchor_clauses = list(last.clauses)

    evidence, search_queries = choose_evidence(
        collection,
        all_points,
        retrieval_query,
        route,
        last,
        contextual_anchor_clauses=anchor_clauses,
    )
    answer, validation = answer_with_validation(
        standalone,
        route,
        evidence,
    )
    clauses = unique_clauses(evidence)

    if debug:
        print("\n" + "=" * 78)
        print("QUESTION   :", question)
        print("NLU MODEL  :", QUERY_REWRITE_MODEL)
        print("NLU        :", json.dumps(understanding.__dict__, ensure_ascii=False))
        if standalone != question:
            print("STANDALONE :", standalone)
        if retrieval_query != standalone:
            print("RETRIEVAL  :", retrieval_query)
        print("INTENT     :", route.intent)
        if anchor_clauses:
            print("ANCHOR     :", ", ".join(anchor_clauses))
        if route.version:
            print("VERSION    :", route.version)
        if route.clause_hint:
            print("CLAUSE     :", route.clause_hint)
        print("SEARCH     :", " | ".join(search_queries))
        if any(q.startswith("scenario governing query:") for q in search_queries):
            print("SCENARIO   : governing-rule retrieval used")
        print("EVIDENCE   :")
        print_evidence(evidence)
        print("\nANSWER:")
        print(answer)
        print("\nVALIDATION:")
        print(json.dumps(validation, ensure_ascii=False, indent=2))
        print("=" * 78)
    else:
        print(answer)

    return TurnState(
        question=question,
        standalone=standalone,
        answer=answer,
        clauses=clauses,
        intent=route.intent,
        version=route.version,
    )


# ---------------------------------------------------------------------------
# Self tests - no network required
# ---------------------------------------------------------------------------

def run_self_tests() -> None:
    assert normalize_digits("৭.৮.৯") == "7.8.9"

    contract = QueryUnderstanding(
        intent="scenario",
        tone="confirmation_seeking",
        language="bn",
        category="স্থায়ী কর্মী",
        action="পদত্যাগ",
        user_facts={"proposed_notice_days": 30},
        standalone_question="স্থায়ী কর্মী ৩০ দিনের নোটিশ দিয়ে পদত্যাগ করতে চাইলে তা গ্রহণযোগ্য কি?",
        retrieval_query="স্থায়ী কর্মীর পদত্যাগের নোটিশের নিয়ম",
        confidence=0.9,
    )
    assert contract.intent == "scenario"
    assert contract.user_facts["proposed_notice_days"] == 30
    assert "পদত্যাগ" in contract.retrieval_query

    # Generic scenario-retrieval regression: hypothetical numbers and conversational
    # framing must not dominate evidence directness.
    scenario_focus = policy_focus_tokens(
        "আমি স্থায়ী কর্মী। ৩০ দিনের নোটিশ দিয়ে পদত্যাগ করতে চাই। "
        "নীতিমালা অনুযায়ী আমি কি তা করতে পারি?"
    )
    assert "30" not in scenario_focus
    assert "৩০" not in scenario_focus
    assert any("পদত্যাগ" in token for token in scenario_focus)

    assert is_scenario_like_question(
        "আমি স্থায়ী কর্মী। ৩০ দিনের নোটিশ দিয়ে পদত্যাগ করতে চাই।"
    ) is True
    assert is_scenario_like_question(
        "স্থায়ী কর্মীর পদত্যাগের নিয়ম কী?"
    ) is False

    scenario_selector_query = compact_policy_query(
        "আমি স্থায়ী কর্মী। ৩০ দিনের নোটিশ দিয়ে পদত্যাগ করতে চাই। "
        "নীতিমালা অনুযায়ী আমি কি তা করতে পারি?"
    )
    assert "30" not in scenario_selector_query
    assert "৩০" not in scenario_selector_query
    assert "পদত্যাগ" in scenario_selector_query

    # Fresh-retrieval fallback must at least have a deterministic governing query
    # even if the LLM rewrite service is unavailable at runtime.
    assert compact_policy_query(
        "আমি স্থায়ী কর্মী। ৩০ দিনের নোটিশ দিয়ে পদত্যাগ করতে চাই।"
    )
    assert extract_clause_hint("ধারা ৭.৮.৯ কী?") == "7.8.9"
    assert extract_clause_hint("no clause here") is None

    # New substantive question should not be forced into old context.
    assert is_probably_followup(
        "নতুন একটি সম্পূর্ণ স্বতন্ত্র নীতিবিষয়ক প্রক্রিয়ার বর্তমান নিয়ম কী?"
    ) is False

    # Short anaphoric questions should use context.
    assert is_probably_followup("আগের নিয়ম কী ছিল?") is True
    assert is_probably_followup("অস্থায়ী কর্মীর ক্ষেত্রে কী?") is True

    fake_versions = [
        {
            "version": "alpha-base",
            "version_rank": 1,
            "records": 1,
            "current_records": 0,
        },
        {
            "version": "beta-amendment",
            "version_rank": 2,
            "records": 1,
            "current_records": 1,
        },
    ]

    route = route_question(
        "বর্তমান নিয়ম কী?",
        "বর্তমান নিয়ম কী?",
        fake_versions,
    )
    assert route.intent == "current"

    route = route_question(
        "আগের নিয়ম কী ছিল?",
        "আগের নিয়ম কী ছিল?",
        fake_versions,
    )
    assert route.intent == "previous"

    route = route_question(
        "আগে এবং এখন কী পরিবর্তন?",
        "আগে এবং এখন কী পরিবর্তন?",
        fake_versions,
    )
    assert route.intent == "compare"

    # Explicit "previous" in the original turn must override inherited
    # "current" wording in a rewritten standalone question.
    route = route_question(
        "আগের নিয়ম কী ছিল?",
        "বর্তমান পরীক্ষামূলক প্রক্রিয়ার আগের নিয়ম কী ছিল?",
        fake_versions,
    )
    assert route.intent == "previous"

    # Synthetic retrieval test: no BURO clause/value/topic is hardcoded.
    synthetic = {
        "id": "synthetic-1",
        "payload": {
            "logical_clause_id": "7.8.9",
            "authoritative_text": "স্থায়ী পরীক্ষামূলক কর্মী পদত্যাগ করতে চাইলে লিখিত আবেদন করবে।",
            "version": "synthetic-current",
            "is_current": True,
        },
        "_rrf_score": 0.03,
    }
    fallback = deterministic_fallback_evidence(
        "স্থায়ী কর্মীর পদত্যাগের নিয়ম কী?",
        [synthetic],
    )
    assert len(fallback) == 1

    assert is_strong_direct_match(
        "স্থায়ী কর্মীর পদত্যাগের নিয়ম কী?",
        [synthetic],
    ) is True

    synthetic_route = Route(intent="current")
    extractive = build_extractive_answer(
        "স্থায়ী কর্মীর পদত্যাগের নিয়ম কী?",
        synthetic_route,
        [synthetic],
    )
    assert "7.8.9" in extractive
    assert "স্থায়ী পরীক্ষামূলক কর্মী পদত্যাগ" in extractive

    # False-NOT_FOUND recovery regression test. The deterministic validator and
    # fallback must use aligned thresholds so direct evidence can never be
    # simultaneously classified as present and then rejected by the fallback.
    synthetic_not_found = {
        "not_found": True,
        "answer": "Not found in retrieved policy context.",
        "claims": [],
    }
    synthetic_validation = deterministic_validation(
        "স্থায়ী কর্মীর পদত্যাগের নিয়ম কী?",
        synthetic_not_found,
        [synthetic],
    )
    assert "not_found_despite_direct_evidence" in synthetic_validation["issues"]
    assert has_direct_evidence(
        "স্থায়ী কর্মীর পদত্যাগের নিয়ম কী?",
        [synthetic],
    ) is True
    synthetic_extractive_result = build_extractive_result(
        "স্থায়ী কর্মীর পদত্যাগের নিয়ম কী?",
        synthetic_route,
        [synthetic],
    )
    assert synthetic_extractive_result["not_found"] is False
    assert synthetic_extractive_result["claims"][0]["record_ids"] == [0]
    assert deterministic_validation(
        "স্থায়ী কর্মীর পদত্যাগের নিয়ম কী?",
        synthetic_extractive_result,
        [synthetic],
    )["status"] == "PASS"

    # Conversation anchor merge test.
    another = {
        "id": "synthetic-2",
        "payload": {
            "logical_clause_id": "2.3.4",
            "authoritative_text": "অন্য একটি পরীক্ষামূলক বিধান।",
            "version": "synthetic-current",
            "is_current": True,
        },
    }
    merged = merge_candidate_points([another], [synthetic])
    assert record_key(merged[0]) == record_key(synthetic)
    assert len(merged) == 2

    rerank_parsed = parse_rerank_results(
        {
            "results": [
                {"index": 1, "relevance_score": -1.0},
                {"index": 0, "relevance_score": 3.5},
            ]
        },
        2,
    )
    assert rerank_parsed == [(0, 3.5), (1, -1.0)]

    print("SELF-TEST PASS")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="BURO production-grade version-aware RAG console"
    )
    parser.add_argument("--collection")
    parser.add_argument("--question")
    parser.add_argument("--file")
    parser.add_argument("--show-versions", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        run_self_tests()
        return 0

    collection = discover_collection(args.collection)
    qdrant_dim, embed_dim, rerank_status = health_check(collection)
    all_points = scroll_all_payloads(collection)
    versions = discover_versions(all_points)

    print("=" * 78)
    print("BURO PRODUCTION VERSION-AWARE RAG")
    print("=" * 78)
    print("Qdrant      :", QDRANT_URL)
    print("Collection  :", collection)
    print("Ollama      :", OLLAMA_URL)
    print("Embedding   :", EMBED_MODEL, f"({embed_dim})")
    print("LLM         :", LLM_MODEL)
    print("Query model :", QUERY_REWRITE_MODEL)
    if RERANK_ENABLED:
        print("Reranker    :", RERANK_MODEL)
        print("Rerank URL  :", rerank_endpoint())
        print(
            "Rerank cfg  :",
            f"candidates={RERANK_CANDIDATES}",
            f"top_n={RERANK_TOP_N}",
            f"max_chars={RERANK_MAX_CHARS}",
            f"required={RERANK_REQUIRED}",
        )
        print("Rerank stat :", rerank_status)
    else:
        print("Reranker    : disabled (set RERANK_URL to enable)")
    print("Records     :", len(all_points))
    print("Versions    :", len(versions))
    print("Safety      : evidence-only + validation + direct-evidence recovery + fail-closed")
    print("=" * 78)

    if args.show_versions:
        print("\nDISCOVERED VERSIONS:")
        print_versions(versions)
        if not args.question and not args.file:
            return 0

    if args.question:
        process_question(
            collection,
            all_points,
            versions,
            args.question,
            last=None,
            debug=args.debug,
        )
        return 0

    if args.file:
        path = Path(args.file)
        if not path.exists():
            raise RuntimeError(f"Question file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            question = line.strip()
            if question and not question.startswith("#"):
                process_question(
                    collection,
                    all_points,
                    versions,
                    question,
                    last=None,
                    debug=args.debug,
                )
        return 0

    print("\nInteractive mode.")
    print("Commands: /quit  /versions  /debug on  /debug off")

    last: TurnState | None = None
    debug = args.debug

    while True:
        try:
            question = input("\nBURO question> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not question:
            continue
        if question in {"/quit", "/exit", "quit", "exit"}:
            break
        if question == "/versions":
            print_versions(versions)
            continue
        if question == "/debug on":
            debug = True
            print("Debug mode: ON")
            continue
        if question == "/debug off":
            debug = False
            print("Debug mode: OFF")
            continue

        new_state = process_question(
            collection,
            all_points,
            versions,
            question,
            last=last,
            debug=debug,
        )
        if new_state.intent != "ambiguous":
            last = new_state

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1)
