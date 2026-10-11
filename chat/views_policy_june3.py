# chat/views_policy.py

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Tuple

import requests
from django.http import (
    HttpRequest,
    HttpResponseBadRequest,
    JsonResponse,
    StreamingHttpResponse,
)
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from chat.dynamic_config import (
    get_ollama_url,
    get_qdrant_url,
    get_setting,
)

logger = logging.getLogger(__name__)


POLICY_STREAM_DEBUG = os.getenv("POLICY_STREAM_DEBUG", "true").lower() == "true"
POLICY_FOLLOWUP_LIMIT = int(os.getenv("POLICY_FOLLOWUP_LIMIT", "5"))
POLICY_TOP_K = int(os.getenv("POLICY_TOP_K", "5"))
POLICY_CONTEXT_MAX_CHARS = int(os.getenv("POLICY_CONTEXT_MAX_CHARS", "16000"))

POLICY_DEFAULT_EMPLOYEE_TYPE = os.getenv(
    "POLICY_DEFAULT_EMPLOYEE_TYPE",
    "GENERAL_EMPLOYEE",
)

DEFAULT_EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text:latest")
DEFAULT_POLICY_MODEL = os.getenv(
    "POLICY_ANSWER_MODEL",
    os.getenv("OLLAMA_MODEL", "aya-23-8b:q5"),
)


# ============================================================
# Basic helpers
# ============================================================

def clean(value: Any) -> str:
    if value is None:
        return ""

    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def is_bn(text: str) -> bool:
    text = text or ""
    return any("\u0980" <= ch <= "\u09ff" for ch in text)


def get_policy_collection() -> str:
    return get_setting(
        "QDRANT_COLLECTION_POLICY",
        os.getenv("POLICY_COLLECTION", "buro_policy"),
    )


def get_embed_model() -> str:
    return get_setting("EMBED_MODEL", DEFAULT_EMBED_MODEL)


def get_policy_answer_model() -> str:
    return get_setting("POLICY_ANSWER_MODEL", DEFAULT_POLICY_MODEL)


@lru_cache(maxsize=8)
def _get_qdrant_client_for_url(url: str) -> QdrantClient:
    return QdrantClient(url=url)


def get_qdrant_client() -> QdrantClient:
    qdrant_url = get_qdrant_url(check_health=True)

    if not qdrant_url:
        raise RuntimeError("Qdrant URL is empty. Please configure QDRANT_URL.")

    return _get_qdrant_client_for_url(qdrant_url)


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


def _unique_clean(items: List[Any], limit: int | None = None) -> List[str]:
    out: List[str] = []
    seen = set()

    for item in items or []:
        txt = clean(item)

        if not txt:
            continue

        key = txt.lower()

        if key in seen:
            continue

        seen.add(key)
        out.append(txt)

        if limit and len(out) >= limit:
            break

    return out


def _string_to_list(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return _unique_clean(value)

    if isinstance(value, tuple):
        return _unique_clean(list(value))

    if isinstance(value, dict):
        return _unique_clean(list(value.values()))

    text = clean(value)

    if not text:
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            return _string_to_list(parsed)
        except Exception:
            pass

    text = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace(";", "\n")
        .replace("|", "\n")
    )

    return _unique_clean([x.strip() for x in text.split("\n")])


def _point_payload(point: Any) -> Dict[str, Any]:
    if point is None:
        return {}

    if isinstance(point, dict):
        return point.get("payload") or {}

    return getattr(point, "payload", None) or {}


def _point_score(point: Any) -> Any:
    if point is None:
        return None

    if isinstance(point, dict):
        return point.get("score")

    return getattr(point, "score", None)


def _payload_text(payload: Dict[str, Any]) -> str:
    """
    Return actual policy text from Qdrant payload.
    embed_text is used as last fallback only.
    """
    for key in [
        "text",
        "policy_text",
        "content",
        "body",
        "rules",
        "clause_text",
        "chunk_text",
        "page_content",
    ]:
        value = clean(payload.get(key))
        if value:
            return value

    return clean(payload.get("embed_text"))


# ============================================================
# Normalization and route detection
# ============================================================

def normalize_policy_question(question: str) -> str:
    q = clean(question)

    if not q:
        return q

    lower_q = q.lower()

    replacements = {
        "eave policy": "leave policy",
        "leav policy": "leave policy",
        "leve policy": "leave policy",
        "leave polic": "leave policy",
        "leave polcy": "leave policy",
        "leave plolicy": "leave policy",
        "ypes of leave": "types of leave",
        "type of leave": "types of leave",
        "types leave": "types of leave",
        "leave type": "leave types",
        "leaves type": "leave types",
        "hr eave policy": "hr leave policy",
        "buro eave policy": "buro leave policy",
        "buro bangladesh eave policy": "buro bangladesh leave policy",
        "chuti policy": "ছুটি policy",
        "chutir policy": "ছুটির policy",
        "chuti niyom": "ছুটির নিয়ম",
        "chutir niyom": "ছুটির নিয়ম",
        "chutir prokarved": "ছুটির প্রকারভেদ",
        "chutir prokarbhed": "ছুটির প্রকারভেদ",
        "chutir prokar": "ছুটির প্রকারভেদ",
    }

    for wrong, right in replacements.items():
        lower_q = lower_q.replace(wrong, right)

    if "eave" in lower_q and "policy" in lower_q:
        lower_q = lower_q.replace("eave", "leave")

    return clean(lower_q)


def _detect_requested_employee_type(question: str) -> str:
    q = clean(question).lower()

    if any(
        term in q
        for term in [
            "প্রকল্প",
            "প্রকল্প কর্মী",
            "project",
            "project staff",
            "project employee",
        ]
    ):
        return "PROJECT_EMPLOYEE"

    if any(
        term in q
        for term in [
            "শিক্ষানবীশ",
            "প্রবেশন",
            "probation",
            "probationary",
            "trainee",
        ]
    ):
        return "PROBATIONARY_EMPLOYEE"

    if any(
        term in q
        for term in [
            "সাধারণ কর্মী",
            "নিয়মিত কর্মী",
            "নিয়মিত কর্মী",
            "regular employee",
            "general employee",
            "permanent employee",
        ]
    ):
        return "GENERAL_EMPLOYEE"

    return POLICY_DEFAULT_EMPLOYEE_TYPE


def detect_policy_route(question: str) -> Dict[str, str]:
    """
    This is only for retrieval hints.
    Final answer is generated dynamically from retrieved policy context.
    """
    q = clean(question).lower()
    employee_type = _detect_requested_employee_type(question)

    route = {
        "category": "",
        "subtopic": "",
        "employee_type": employee_type,
        "expanded_query": question,
    }

    separation_terms = [
        "কর্ম বিচ্ছেদ",
        "চাকুরী বিচ্ছেদ",
        "চাকরি বিচ্ছেদ",
        "অব্যাহতি",
        "পদত্যাগ",
        "বরখাস্ত",
        "termination",
        "separation",
        "employment separation",
        "resignation",
        "dismissal",
    ]

    leave_types_terms = [
        "types of leave",
        "leave types",
        "type of leave",
        "kinds of leave",
        "list of leave",
        "leave list",
        "ছুটির প্রকারভেদ",
        "ছুটির ধরন",
        "ছুটির ধরণ",
        "ছুটির প্রকার",
        "কত ধরনের ছুটি",
        "কি কি ছুটি",
        "কী কী ছুটি",
    ]

    sick_terms = [
        "sick leave",
        "medical leave",
        "illness leave",
        "অসুস্থতার ছুটি",
        "অসুস্থতাজনিত ছুটি",
        "অসুস্থতা জনিত ছুটি",
        "অসুস্থ",
    ]

    annual_terms = [
        "annual leave",
        "earned leave",
        "yearly leave",
        "বার্ষিক ছুটি",
        "বাৎসরিক ছুটি",
    ]

    casual_terms = [
        "casual leave",
        "নৈমিত্তিক ছুটি",
    ]

    maternity_terms = [
        "maternity leave",
        "মাতৃত্বকালীন ছুটি",
        "মাতৃত্বজনিত ছুটি",
    ]

    paternity_terms = [
        "paternity leave",
        "পিতৃত্বকালীন ছুটি",
        "পিতৃত্বজনিত ছুটি",
    ]

    unpaid_terms = [
        "leave without pay",
        "without pay",
        "unpaid leave",
        "বিনা বেতনে ছুটি",
        "বিনা বেতনে",
    ]

    general_leave_terms = [
        "leave policy",
        "hr leave policy",
        "leave rules",
        "leave rule",
        "ছুটির নিয়ম",
        "ছুটির নিয়মাবলী",
        "ছুটির নীতিমালা",
        "ছুটি নীতিমালা",
    ]

    overtime_terms = [
        "অধিকাল ভাতা",
        "overtime",
        "overtime allowance",
        "ছুটির দিনে কাজ",
    ]

    if any(t in q for t in separation_terms):
        route.update(
            {
                "category": "EMPLOYMENT_END",
                "subtopic": "WORK_SEPARATION",
                "expanded_query": (
                    "কর্ম বিচ্ছেদ চাকুরী বিচ্ছেদ চাকরি বিচ্ছেদ অব্যাহতি পদত্যাগ "
                    "termination employment separation resignation dismissal ৪.১ 4.1"
                ),
            }
        )
        return route

    if any(t in q for t in leave_types_terms):
        route.update(
            {
                "category": "LEAVE",
                "subtopic": "LEAVE_TYPES",
                "expanded_query": (
                    "ছুটির প্রকারভেদ leave types types of leave kinds of leave "
                    "۵.۲ ৫.২ 5.2 নৈমিত্তিক ছুটি বার্ষিক ছুটি অসুস্থতাজনিত ছুটি "
                    "maternity leave paternity leave casual leave annual leave sick leave"
                ),
            }
        )
        return route

    if any(t in q for t in sick_terms):
        route.update(
            {
                "category": "LEAVE",
                "subtopic": "SICK_LEAVE",
                "expanded_query": (
                    "অসুস্থতাজনিত ছুটি অসুস্থতার ছুটি sick leave medical leave "
                    "illness leave employee sick leave entitlement কত দিন"
                ),
            }
        )
        return route

    if any(t in q for t in annual_terms):
        route.update(
            {
                "category": "LEAVE",
                "subtopic": "ANNUAL_LEAVE",
                "expanded_query": (
                    "বার্ষিক ছুটি বাৎসরিক ছুটি annual leave earned leave yearly leave "
                    "employee leave entitlement বছরে কত দিন"
                ),
            }
        )
        return route

    if any(t in q for t in casual_terms):
        route.update(
            {
                "category": "LEAVE",
                "subtopic": "CASUAL_LEAVE",
                "expanded_query": "নৈমিত্তিক ছুটি casual leave employee leave entitlement কত দিন",
            }
        )
        return route

    if any(t in q for t in maternity_terms):
        route.update(
            {
                "category": "LEAVE",
                "subtopic": "MATERNITY_LEAVE",
                "expanded_query": "মাতৃত্বকালীন ছুটি মাতৃত্বজনিত ছুটি maternity leave কত দিন",
            }
        )
        return route

    if any(t in q for t in paternity_terms):
        route.update(
            {
                "category": "LEAVE",
                "subtopic": "PATERNITY_LEAVE",
                "expanded_query": "পিতৃত্বকালীন ছুটি পিতৃত্বজনিত ছুটি paternity leave কত দিন",
            }
        )
        return route

    if any(t in q for t in unpaid_terms):
        route.update(
            {
                "category": "LEAVE",
                "subtopic": "LEAVE_WITHOUT_PAY",
                "expanded_query": "বিনা বেতনে ছুটি leave without pay unpaid leave কত দিন",
            }
        )
        return route

    if any(t in q for t in overtime_terms):
        route.update(
            {
                "category": "ALLOWANCE",
                "subtopic": "OVERTIME_ALLOWANCE",
                "expanded_query": "অধিকাল ভাতা overtime allowance অফিস সময়ের পরে কাজ ছুটির দিনে কাজ",
            }
        )
        return route

    if any(t in q for t in general_leave_terms):
        route.update(
            {
                "category": "LEAVE",
                "subtopic": "GENERAL_LEAVE_RULES",
                "expanded_query": (
                    "HR Policy ছুটির নিয়মাবলী leave policy leave rules general leave rules "
                    "leave application leave approval unauthorized absence leave without pay"
                ),
            }
        )
        return route

    if "leave" in q or "ছুটি" in q or "ছুটির" in q:
        route.update(
            {
                "category": "LEAVE",
                "subtopic": "GENERAL_LEAVE_RULES",
                "expanded_query": (
                    "HR Policy ছুটির নিয়মাবলী leave policy leave rules general leave rules "
                    "leave application leave approval unauthorized absence leave without pay"
                ),
            }
        )
        return route

    return route


def is_full_policy_request(question: str) -> bool:
    q = clean(question).lower()

    terms = [
        "full policy",
        "complete policy",
        "whole policy",
        "full clause",
        "exact clause",
        "complete clause",
        "show full",
        "show complete",
        "entire policy",
        "পুরো নীতিমালা",
        "সম্পূর্ণ নীতিমালা",
        "পুরো ধারা",
        "সম্পূর্ণ ধারা",
        "পুরো নিয়ম",
        "সম্পূর্ণ নিয়ম",
    ]

    return any(t in q for t in terms)


def is_list_or_reason_question(question: str) -> bool:
    q = clean(question).lower()

    terms = [
        "কারণ",
        "কারণগুলো",
        "কি কি",
        "কী কী",
        "তালিকা",
        "প্রকার",
        "ধরন",
        "ধরণ",
        "list",
        "types",
        "kinds",
        "causes",
        "reasons",
        "what are",
    ]

    return any(t in q for t in terms)


def extract_list_items_from_context(context: str, max_items: int = 15) -> List[str]:
    """
    Generic list extractor from retrieved policy text.
    Works for কারণ, প্রকারভেদ, list, types, etc.
    """
    lines = [clean(x) for x in (context or "").splitlines()]
    items: List[str] = []

    for line in lines:
        if not line:
            continue

        if line.startswith("[Source"):
            continue

        cleaned = re.sub(
            r"^(?:[-•*]+|ক্স|[0-9]+[.)]|[০-৯]+[.)]|\([^)]+\)|[কখগঘঙচছজঝঞটঠডঢণতথদধনপফবভমযরলশষসহ]\))\s*",
            "",
            line,
        ).strip()

        if cleaned == line:
            continue

        cleaned = clean(cleaned)

        if not cleaned:
            continue

        if len(cleaned) > 120:
            continue

        if cleaned not in items:
            items.append(cleaned)

        if len(items) >= max_items:
            break

    return items


# ============================================================
# Qdrant retrieval
# ============================================================

def _build_qdrant_filter(
    category: str = "",
    subtopic: str = "",
    employee_type: str = "",
    topic_id: str = "",
    topic_bn: str = "",
    topic_en: str = "",
) -> Filter:
    must = []

    if topic_id:
        must.append(FieldCondition(key="topic_id", match=MatchValue(value=topic_id)))

    if topic_bn:
        must.append(FieldCondition(key="topic_bn", match=MatchValue(value=topic_bn)))

    if topic_en:
        must.append(FieldCondition(key="topic_en", match=MatchValue(value=topic_en)))

    if category:
        must.append(FieldCondition(key="category", match=MatchValue(value=category)))

    if subtopic:
        must.append(FieldCondition(key="subtopic", match=MatchValue(value=subtopic)))

    if employee_type:
        must.append(
            FieldCondition(key="employee_type", match=MatchValue(value=employee_type))
        )

    return Filter(must=must)


def _scroll_first_payload(
    qdrant: QdrantClient,
    collection: str,
    q_filter: Filter,
) -> Dict[str, Any]:
    try:
        points, _next = qdrant.scroll(
            collection_name=collection,
            scroll_filter=q_filter,
            limit=5,
            with_payload=True,
            with_vectors=False,
        )

        for point in points or []:
            payload = _point_payload(point)

            if _payload_text(payload):
                return payload

    except Exception as e:
        logger.warning("Qdrant scroll failed: %s", e)

    return {}


def get_direct_payload_for_route(
    qdrant: QdrantClient,
    collection: str,
    route: Dict[str, str],
) -> Dict[str, Any]:
    category = clean(route.get("category"))
    subtopic = clean(route.get("subtopic"))
    employee_type = clean(route.get("employee_type"))

    filters: List[Filter] = []

    if category and subtopic and employee_type:
        filters.append(
            _build_qdrant_filter(
                category=category,
                subtopic=subtopic,
                employee_type=employee_type,
            )
        )

    if category and subtopic:
        filters.append(_build_qdrant_filter(category=category, subtopic=subtopic))

    if subtopic == "LEAVE_TYPES":
        filters.extend(
            [
                _build_qdrant_filter(topic_id="policy_5_2_leave_types"),
                _build_qdrant_filter(topic_id="policy_5_2"),
                _build_qdrant_filter(topic_bn="ছুটির প্রকারভেদ"),
                _build_qdrant_filter(topic_en="Leave Types"),
            ]
        )

    if subtopic == "WORK_SEPARATION":
        filters.extend(
            [
                _build_qdrant_filter(topic_id="policy_4_1_work_separation"),
                _build_qdrant_filter(topic_id="policy_4_1"),
                _build_qdrant_filter(topic_bn="কর্ম বিচ্ছেদ"),
                _build_qdrant_filter(topic_en="Work Separation"),
                _build_qdrant_filter(topic_en="Employment Separation"),
                _build_qdrant_filter(topic_en="Separation from Service"),
            ]
        )

    for q_filter in filters:
        payload = _scroll_first_payload(qdrant, collection, q_filter)

        if payload:
            return payload

    return {}


def ollama_embed(text: str) -> List[float]:
    text = clean(text)

    if not text:
        return []

    ollama_url = get_ollama_url().rstrip("/")
    embed_model = get_embed_model()

    response = requests.post(
        f"{ollama_url}/api/embeddings",
        json={
            "model": embed_model,
            "prompt": text,
        },
        timeout=(10, 180),
    )
    response.raise_for_status()

    data = response.json()
    vector = data.get("embedding") or []

    if not isinstance(vector, list) or not vector:
        return []

    return vector


def qdrant_vector_search(
    qdrant: QdrantClient,
    collection: str,
    vector: List[float],
    top_k: int = POLICY_TOP_K,
) -> List[Any]:
    """
    Supports both named-vector and unnamed-vector Qdrant collections.
    """
    errors = []

    try:
        return qdrant.search(
            collection_name=collection,
            query_vector=("default", vector),
            limit=top_k,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        errors.append(str(e))

    try:
        return qdrant.search(
            collection_name=collection,
            query_vector=vector,
            limit=top_k,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        errors.append(str(e))

    logger.error("Qdrant vector search failed: %s", " | ".join(errors))
    return []


def build_citation(payload: Dict[str, Any], idx: int, score: Any = None) -> Dict[str, Any]:
    return {
        "idx": idx,
        "topic_id": clean(payload.get("topic_id")),
        "topic_bn": clean(payload.get("topic_bn")),
        "topic_en": clean(payload.get("topic_en")),
        "clause_range": clean(payload.get("clause_range")),
        "source_file": clean(payload.get("source_file")),
        "category": clean(payload.get("category")),
        "subtopic": clean(payload.get("subtopic")),
        "employee_type": clean(payload.get("employee_type")),
        "score": score,
    }


def get_clause_reference(citations: List[Dict[str, Any]], text: str = "") -> str:
    refs = []

    for citation in citations or []:
        ref = clean(citation.get("clause_range"))

        if ref:
            refs.append(ref)

    refs = _unique_clean(refs, limit=3)

    if refs:
        return ", ".join(refs)

    pattern = r"[0-9\u09E6-\u09EF]+\.[0-9\u09E6-\u09EF]+(?:\.[0-9\u09E6-\u09EF]+)?"
    found = re.findall(pattern, text or "")

    return ", ".join(_unique_clean(found, limit=5)) or "Not found"


def retrieve_policy_context(
    question: str,
    collection: str,
) -> Tuple[str, List[Dict[str, Any]], Dict[str, str], Dict[str, Any]]:
    qdrant = get_qdrant_client()

    question_norm = normalize_policy_question(question)
    route = detect_policy_route(question_norm)

    payloads: List[Tuple[Dict[str, Any], Any]] = []

    direct_payload = get_direct_payload_for_route(
        qdrant=qdrant,
        collection=collection,
        route=route,
    )

    if direct_payload:
        payloads.append((direct_payload, None))

    if not payloads:
        query_text = route.get("expanded_query") or question_norm
        vector = ollama_embed(query_text)

        if vector:
            points = qdrant_vector_search(
                qdrant=qdrant,
                collection=collection,
                vector=vector,
                top_k=POLICY_TOP_K,
            )

            for point in points or []:
                payload = _point_payload(point)
                text = _payload_text(payload)

                if text:
                    payloads.append((payload, _point_score(point)))

    context_parts: List[str] = []
    citations: List[Dict[str, Any]] = []
    seen_text = set()

    for idx, (payload, score) in enumerate(payloads, start=1):
        text = _payload_text(payload)

        if not text:
            continue

        text_key = text[:300].strip().lower()

        if text_key in seen_text:
            continue

        seen_text.add(text_key)

        citation = build_citation(payload, idx, score)
        citations.append(citation)

        title = (
            clean(payload.get("topic_bn"))
            or clean(payload.get("topic_en"))
            or clean(payload.get("topic_id"))
            or f"Source {idx}"
        )

        clause = clean(payload.get("clause_range"))

        header = f"[Source {idx}] {title}"

        if clause:
            header += f" | clause={clause}"

        context_parts.append(f"{header}\n{text}")

    context = "\n\n---\n\n".join(context_parts)
    context = context[:POLICY_CONTEXT_MAX_CHARS]

    active_topic = citations[0] if citations else {}

    return context, citations, route, active_topic


# ============================================================
# Ollama final answer generation
# ============================================================

def build_policy_prompt(
    question: str,
    context: str,
    citations: List[Dict[str, Any]],
    route: Dict[str, str],
) -> str:
    clause_ref = get_clause_reference(citations, context)
    output_lang = "Bangla" if is_bn(question) or is_bn(context) else "English"

    if is_full_policy_request(question):
        answer_style = (
            "The user asked for full/complete policy. Return the relevant policy text "
            "from the context as completely as possible. Do not compress to 3 bullets."
        )
    else:
        answer_style = (
            "Answer the exact user question. Keep it concise. "
            "If the user asks for causes/types/list, extract the list. "
            "If the user asks how many days, answer exact days if found. "
            "If exact information is not found, say clearly that it is not found in the provided policy text."
        )

    return f"""
You are an HR policy assistant.

You must answer using ONLY the policy text provided below.
Do not invent policy rules.
Do not use outside knowledge.
Do not answer from memory.

Language:
- Answer in {output_lang}.
- If the policy text is Bangla, keep policy terms in Bangla.

Answer style:
- {answer_style}
- Do not return unrelated policy text.
- If retrieved text does not answer the question, say it was not clearly found.
- Always include policy reference.
- Generate 3 useful follow-up questions related to the same topic.

User question:
{question}

Detected retrieval route:
{json.dumps(route, ensure_ascii=False)}

Policy reference:
{clause_ref}

Retrieved policy text:
{context}

Return exactly in this format:

Decision:
<short decision/title>

Answer:
<direct answer based only on retrieved policy text>

Policy reference:
{clause_ref}

Follow-up questions:
- <question 1>
- <question 2>
- <question 3>
""".strip()


def call_ollama_generate(prompt: str) -> str:
    ollama_url = get_ollama_url().rstrip("/")
    model = get_policy_answer_model()

    response = requests.post(
        f"{ollama_url}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "top_p": 0.9,
                "num_ctx": 8192,
            },
        },
        timeout=(10, 240),
    )
    response.raise_for_status()

    data = response.json()

    return clean(data.get("response"))


def fallback_answer(
    question: str,
    context: str,
    citations: List[Dict[str, Any]],
) -> str:
    clause_ref = get_clause_reference(citations, context)
    bn = is_bn(question) or is_bn(context)

    if not context:
        if bn:
            return (
                "Decision: স্পষ্টভাবে পাওয়া যায়নি\n\n"
                "Answer:\n"
                "নীতি/ধারা থেকে স্পষ্ট উত্তর পাওয়া যায়নি।\n\n"
                f"Policy reference:\n{clause_ref}\n\n"
                "Follow-up questions:\n"
                "- আপনি কোন নির্দিষ্ট নীতির কথা জানতে চান?\n"
                "- ধারা নম্বর জানা আছে কি?\n"
                "- বিস্তারিত নীতিমালা দেখতে চান?"
            )

        return (
            "Decision: Not clearly found\n\n"
            "Answer:\n"
            "A clear policy excerpt was not found.\n\n"
            f"Policy reference:\n{clause_ref}\n\n"
            "Follow-up questions:\n"
            "- Which specific policy do you want?\n"
            "- Do you know the clause number?\n"
            "- Do you want the full policy text?"
        )

    if is_list_or_reason_question(question):
        items = extract_list_items_from_context(context)

        if items:
            items_text = "\n".join(f"- {item}" for item in items)

            if bn:
                return (
                    "Decision: নীতিমালা অনুযায়ী তালিকা\n\n"
                    "Answer:\n"
                    f"{items_text}\n\n"
                    f"Policy reference:\n{clause_ref}\n\n"
                    "Follow-up questions:\n"
                    "- এই বিষয়ে বিস্তারিত কী?\n"
                    "- প্রতিটি পয়েন্টের ব্যাখ্যা কী?\n"
                    "- সংশ্লিষ্ট সম্পূর্ণ ধারা দেখান।"
                )

            return (
                "Decision: Policy list\n\n"
                "Answer:\n"
                f"{items_text}\n\n"
                f"Policy reference:\n{clause_ref}\n\n"
                "Follow-up questions:\n"
                "- Explain each point.\n"
                "- Show the full related clause.\n"
                "- Are there any exceptions?"
            )

    if is_full_policy_request(question):
        if bn:
            return (
                "Decision: সম্পূর্ণ নীতিমালা\n\n"
                "Policy details:\n"
                f"{context}\n\n"
                f"Policy reference:\n{clause_ref}\n\n"
                "Follow-up questions:\n"
                "- এই নীতির মূল শর্তগুলো কী?\n"
                "- সহজ করে ব্যাখ্যা করুন।\n"
                "- ৩টি পয়েন্টে বলুন।"
            )

        return (
            "Decision: Full policy\n\n"
            "Policy details:\n"
            f"{context}\n\n"
            f"Policy reference:\n{clause_ref}\n\n"
            "Follow-up questions:\n"
            "- What are the main conditions?\n"
            "- Explain simply.\n"
            "- Answer in 3 points."
        )

    if bn:
        return (
            "Decision: নীতিমালার প্রাসঙ্গিক অংশ\n\n"
            "Answer:\n"
            f"{context[:2500]}\n\n"
            f"Policy reference:\n{clause_ref}\n\n"
            "Follow-up questions:\n"
            "- এই বিষয়ে বিস্তারিত কী?\n"
            "- এই নীতির মূল শর্তগুলো কী?\n"
            "- এখানে কোনো ব্যতিক্রম আছে কি?"
        )

    return (
        "Decision: Relevant policy section\n\n"
        "Answer:\n"
        f"{context[:2500]}\n\n"
        f"Policy reference:\n{clause_ref}\n\n"
        "Follow-up questions:\n"
        "- What are the main conditions?\n"
        "- Are there any exceptions?\n"
        "- Do you want the full policy text?"
    )


# ============================================================
# Main payload builder
# ============================================================

def build_success_payload(
    question: str,
    collection: str | None = None,
) -> Dict[str, Any]:
    collection_name = clean(collection) or get_policy_collection()
    question_norm = normalize_policy_question(question)

    context, citations, route, active_topic = retrieve_policy_context(
        question=question_norm,
        collection=collection_name,
    )

    if not context:
        answer = fallback_answer(question_norm, context, citations)
    else:
        prompt = build_policy_prompt(
            question=question_norm,
            context=context,
            citations=citations,
            route=route,
        )

        try:
            answer = call_ollama_generate(prompt)

            # Important: Ollama may return HTTP 200 but empty response.
            # In that case, use deterministic fallback from retrieved policy text.
            if not clean(answer):
                answer = fallback_answer(question_norm, context, citations)

        except Exception as e:
            logger.exception("Ollama policy answer generation failed: %s", e)
            answer = fallback_answer(question_norm, context, citations)

    payload = {
        "ok": True,
        "question": question,
        "question_for_route": question_norm,
        "answer": answer,
        "raw_context": context,
        "route": route,
        "answer_mode": "llm_grounded",
        "validation": {
            "valid": bool(context),
            "reason": "llm_grounded_from_retrieved_context" if context else "no_context_found",
        },
        "detected_topic": active_topic,
        "active_topic": active_topic,
        "citations": citations,
        "suggestions": [],
        "related_questions": [],
        "must_followup_questions": [],
        "clarification_questions": [],
        "quick_actions": [
            "আরও বিস্তারিত দেখান",
            "সংশ্লিষ্ট ধারা দেখান",
            "সহজ করে ব্যাখ্যা করুন",
            "৩টি পয়েন্টে বলুন",
        ],
        "needs_clarification": False,
        "interactive": {
            "interactive_enabled": True,
            "active_topic": active_topic,
            "must_followup_questions": [],
            "clarification_questions": [],
            "quick_actions": [
                "আরও বিস্তারিত দেখান",
                "সংশ্লিষ্ট ধারা দেখান",
                "সহজ করে ব্যাখ্যা করুন",
                "৩টি পয়েন্টে বলুন",
            ],
            "needs_clarification": False,
            "default_employee_type": POLICY_DEFAULT_EMPLOYEE_TYPE,
            "route": route,
            "answer_mode": "llm_grounded",
        },
        "topic_groups": [],
        "topic_list_mode": False,
        "topics_count": 0,
    }

    if POLICY_STREAM_DEBUG:
        payload["debug"] = {
            "collection": collection_name,
            "qdrant_url": get_qdrant_url(check_health=False),
            "ollama_url": get_ollama_url(),
            "embed_model": get_embed_model(),
            "answer_model": get_policy_answer_model(),
            "route": route,
            "citations_count": len(citations),
            "context_chars": len(context or ""),
        }

    return payload


# ============================================================
# HTTP endpoints
# ============================================================

@csrf_exempt
@require_POST
def policy_ask(request: HttpRequest):
    data = parse_json_body(request)
    question = extract_question(data)
    collection = clean(data.get("collection")) or get_policy_collection()

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
                "route": {},
                "answer_mode": "error",
                "validation": {
                    "valid": False,
                    "reason": str(e),
                },
                "citations": [],
                "suggestions": [],
                "related_questions": [],
                "must_followup_questions": [],
                "clarification_questions": [],
                "quick_actions": [],
                "needs_clarification": False,
                "interactive": {},
            },
            status=500,
            json_dumps_params={"ensure_ascii": False},
        )


@csrf_exempt
@require_POST
def policy_ask_stream(request: HttpRequest):
    data = parse_json_body(request)
    question = extract_question(data)
    collection = clean(data.get("collection")) or get_policy_collection()

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
                "raw_context": payload.get("raw_context", ""),
                "question_for_route": payload.get("question_for_route", ""),
                "route": payload.get("route") or {},
                "answer_mode": payload.get("answer_mode") or "",
                "validation": payload.get("validation") or {},
                "detected_topic": payload.get("detected_topic") or {},
                "active_topic": payload.get("active_topic") or {},
                "citations": payload.get("citations") or [],
                "suggestions": payload.get("suggestions") or [],
                "related_questions": payload.get("related_questions") or [],
                "must_followup_questions": payload.get("must_followup_questions") or [],
                "clarification_questions": payload.get("clarification_questions") or [],
                "quick_actions": payload.get("quick_actions") or [],
                "needs_clarification": bool(payload.get("needs_clarification", False)),
                "interactive": payload.get("interactive") or {},
                "topic_groups": payload.get("topic_groups") or [],
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
                "route": {},
                "answer_mode": "error",
                "validation": {
                    "valid": False,
                    "reason": str(e),
                },
                "citations": [],
                "suggestions": [],
                "related_questions": [],
                "must_followup_questions": [],
                "clarification_questions": [],
                "quick_actions": [],
                "needs_clarification": False,
                "interactive": {},
            }
            yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n"

    response = StreamingHttpResponse(
        event_stream(),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"

    return response
