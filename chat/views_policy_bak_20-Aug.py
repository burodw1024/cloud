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


# ============================================================
# Config
# ============================================================

POLICY_CODE_VERSION = "policy_view_2026_06_11_topic_memory_v5_safe_followup"

POLICY_STREAM_DEBUG = os.getenv("POLICY_STREAM_DEBUG", "true").lower() == "true"
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

POLICY_VECTOR_NAME = os.getenv("POLICY_VECTOR_NAME", "").strip()
POLICY_SESSION_ACTIVE_TOPIC_KEY = "policy_last_active_topic"
POLICY_RAG_MEMORY_KEY = "policy_rag_followup_memory_v1"
POLICY_RAG_MEMORY_CONTEXT_MAX_CHARS = int(
    os.getenv("POLICY_RAG_MEMORY_CONTEXT_MAX_CHARS", "20000")
)
POLICY_RAG_MEMORY_ANSWER_MAX_CHARS = int(
    os.getenv("POLICY_RAG_MEMORY_ANSWER_MAX_CHARS", "6000")
)
POLICY_RAG_MEMORY_HISTORY_LIMIT = int(
    os.getenv("POLICY_RAG_MEMORY_HISTORY_LIMIT", "6")
)

# Explicit frontend modes. Recommended values from UI/API:
#   mode=primary    -> force fresh Qdrant/source retrieval
#   mode=followup   -> answer from saved topic context only
#   mode=auto       -> default: detect safely
POLICY_MODE_PRIMARY_VALUES = {"primary", "new", "new_search", "search", "reset", "fresh"}
POLICY_MODE_FOLLOWUP_VALUES = {"followup", "follow_up", "same_topic", "memory", "memory_only"}


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


def clean_model_answer(value: Any) -> str:
    text = clean(value)

    if not text:
        return ""

    bad_tokens = [
        "<EOS_TOKEN>",
        "<eos>",
        "</s>",
        "<|endoftext|>",
        "<|im_end|>",
        "<|end|>",
    ]

    for token in bad_tokens:
        text = text.replace(token, "")

    return clean(text)


def is_bn(text: str) -> bool:
    return any("\u0980" <= ch <= "\u09ff" for ch in (text or ""))


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
# Digit normalization
# ============================================================

_DIGIT_TO_ASCII: Dict[str, str] = {}

for i in range(10):
    _DIGIT_TO_ASCII[str(i)] = str(i)
    _DIGIT_TO_ASCII[chr(0x09E6 + i)] = str(i)  # Bengali: ০-৯
    _DIGIT_TO_ASCII[chr(0x0660 + i)] = str(i)  # Arabic-Indic: ٠-٩
    _DIGIT_TO_ASCII[chr(0x06F0 + i)] = str(i)  # Persian: ۰-۹


def normalize_clause_key(value: Any) -> str:
    """
    Convert English/Bangla/Arabic/Persian digits into ASCII digits.
    """
    text = clean(value)

    if not text:
        return ""

    text = "".join(_DIGIT_TO_ASCII.get(ch, ch) for ch in text)
    text = text.replace("٫", ".").replace("۔", ".").replace("．", ".")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def display_clause_bn(value: Any) -> str:
    """
    Convert ASCII digits to Bengali digits for display.
    """
    text = normalize_clause_key(value)
    out = []

    for ch in text:
        if ch.isdigit():
            out.append(chr(0x09E6 + int(ch)))
        else:
            out.append(ch)

    return "".join(out)


def extract_ascii_clause_from_text(text: str) -> str:
    """
    Extract 1.8, 1.3.1, or .4 from broken labels like .۴ শিক্ষাগত যোগ্যতা.
    """
    q = normalize_clause_key(text)
    found = re.findall(r"\d+(?:\.\d+){1,3}", q)

    if found:
        return found[0]

    broken = re.findall(r"(^|\s)\.(\d+)", q)

    if broken:
        return "." + broken[0][1]

    return ""


# ============================================================
# Exact policy topic catalog
# ============================================================

def topic(
    group_id: str,
    group_bn: str,
    clause: str,
    title_bn: str,
    aliases: List[str],
    question: str,
    exclude: List[str] | None = None,
    allow_topic_fallback: bool = False,
    alternate_clauses: List[str] | None = None,
) -> Dict[str, Any]:
    return {
        "group_id": group_id,
        "group_bn": group_bn,
        "clause": normalize_clause_key(clause),
        "title_bn": clean(title_bn),
        "aliases": aliases,
        "question": clean(question),
        "exclude": exclude or [],
        "allow_topic_fallback": allow_topic_fallback,
        "alternate_clauses": [
            normalize_clause_key(x) for x in (alternate_clauses or [])
        ],
    }


EXACT_POLICY_TOPICS: List[Dict[str, Any]] = [
    topic(
        "hr_policy",
        "মানবসম্পদ নীতিমালা",
        "1.3.1",
        "গ্রেড ও পদবিন্যাস",
        ["গ্রেড ও পদবিন্যাস", "গ্রেড", "পদবিন্যাস", "grade"],
        "۱.۳.۱ গ্রেড ও পদবিন্যাস দেখান",
    ),
    topic(
        "hr_policy",
        "মানবসম্পদ নীতিমালা",
        "1.4",
        "শিক্ষাগত যোগ্যতা",
        [
            "শিক্ষাগত যোগ্যতা",
            "যোগ্যতা",
            "educational qualification",
            "education qualification",
        ],
        "۱.۴ শিক্ষাগত যোগ্যতা দেখান",
    ),
    topic(
        "hr_policy",
        "মানবসম্পদ নীতিমালা",
        "1.5",
        "নিয়োগ",
        ["নিয়োগ", "নিয়োগ", "recruitment", "appointment", "hiring"],
        "۱.۵ নিয়োগ নীতিমালা দেখান",
        exclude=["প্রকল্প কর্মী", "project employee", "প্রকল্প কর্মীদের"],
    ),
    topic(
        "hr_policy",
        "মানবসম্পদ নীতিমালা",
        "1.8",
        "বদলি",
        ["বদলি", "বদলী", "transfer"],
        "۱.۸ বদলি নীতিমালা দেখান",
        exclude=[
            "বদলি/স্থানান্তর ভাতা",
            "স্থানান্তর ভাতা",
            "ভাতা",
            "allowance",
            "প্রকল্প কর্মী",
            "project employee",
            "project staff",
        ],
        allow_topic_fallback=True,
    ),
    topic(
        "hr_policy",
        "মানবসম্পদ নীতিমালা",
        "1.9",
        "কাজের বিবরণী",
        ["কাজের বিবরণী", "job description"],
        "۱.۹ কাজের বিবরণী দেখান",
    ),
    topic(
        "hr_policy",
        "মানবসম্পদ নীতিমালা",
        "1.10",
        "অফিস সময়",
        ["অফিস সময়", "office time"],
        "۱.۱۰ অফিস সময় দেখান",
    ),
    topic(
        "hr_policy",
        "মানবসম্পদ নীতিমালা",
        "1.11",
        "উপস্থিতি",
        ["উপস্থিতি", "attendance"],
        "۱.۱۱ উপস্থিতি নীতিমালা দেখান",
    ),
    topic(
        "hr_policy",
        "মানবসম্পদ নীতিমালা",
        "1.12",
        "প্রবেশন",
        ["প্রবেশন", "probation"],
        "۱.۱۲ প্রবেশন نীতিমালা দেখান",
    ),
    topic(
        "hr_policy",
        "মানবসম্পদ নীতিমালা",
        "1.13",
        "দায়িত্ব ও কর্তব্য",
        ["দায়িত্ব ও কর্তব্য", "দায়িত্ব", "কর্তব্য", "responsibility", "duties"],
        "۱.۱۳ দায়িত্ব ও কর্তব্য দেখান",
    ),

    topic(
        "promotion_policy",
        "পদোন্নতি নীতিমালা",
        "2.2",
        "পদোন্নতির সাধারণ নিয়ম",
        ["পদোন্নতির সাধারণ নিয়ম", "promotion rules"],
        "۲.۲ পদোন্নতির সাধারণ নিয়ম দেখান",
    ),
    topic(
        "promotion_policy",
        "পদোন্নতি নীতিমালা",
        "2.3",
        "পদোন্নতির যোগ্যতা",
        ["পদোন্নতির যোগ্যতা", "promotion eligibility"],
        "۲.۳ পদোন্নতির যোগ্যতা দেখান",
    ),
    topic(
        "promotion_policy",
        "পদোন্নতি নীতিমালা",
        "2.4",
        "দক্ষতা মূল্যায়ন পদ্ধতি",
        ["দক্ষতা মূল্যায়ন", "দক্ষতা মূল্যায়ন", "performance evaluation"],
        "۲.۴ দক্ষতা মূল্যায়ন পদ্ধতি ব্যাখ্যা করুন",
    ),

    topic(
        "discipline_policy",
        "শৃঙ্খলা নীতিমালা",
        "3.2",
        "অসদাচরণ",
        ["অসদাচরণ", "misconduct"],
        "۳.۲ অসদাচরণ কী",
    ),
    topic(
        "discipline_policy",
        "শৃঙ্খলা নীতিমালা",
        "3.3",
        "তদন্ত",
        ["তদন্ত", "investigation"],
        "۳.۳ তদন্ত প্রক্রিয়া দেখান",
    ),
    topic(
        "discipline_policy",
        "শৃঙ্খলা নীতিমালা",
        "3.4",
        "শাস্তিমূলক ব্যবস্থা",
        ["শাস্তিমূলক ব্যবস্থা", "disciplinary action"],
        "۳.۴ শাস্তিমূলক ব্যবস্থা দেখান",
    ),
    topic(
        "discipline_policy",
        "শৃঙ্খলা নীতিমালা",
        "3.5",
        "আপিল",
        ["আপিল", "appeal"],
        "۳.۵ আপিল প্রক্রিয়া দেখান",
    ),
    topic(
        "discipline_policy",
        "শৃঙ্খলা নীতিমালা",
        "3.6",
        "অন্যান্য শৃঙ্খলাজনিত বিষয়",
        ["শৃঙ্খলাজনিত", "disciplinary matters"],
        "۳.۶ অন্যান্য শৃঙ্খলাজনিত বিষয় দেখান",
    ),

    topic(
        "service_rules",
        "চাকরি বিধিমালা",
        "6.1",
        "সাধারণ নিয়ম",
        ["সাধারণ নিয়ম", "general rules"],
        "۶.۱ সাধারণ নিয়ম দেখান",
    ),
    topic(
        "service_rules",
        "চাকরি বিধিমালা",
        "6.2",
        "চাকরির শর্ত",
        ["চাকরির শর্ত", "service conditions"],
        "۶.۲ চাকরির শর্ত দেখান",
    ),
    topic(
        "service_rules",
        "চাকরি বিধিমালা",
        "6.3",
        "প্রভিডেন্ট ফান্ড",
        ["প্রভিডেন্ট", "provident fund"],
        "۶.۳ প্রভিডেন্ট ফান্ড বিস্তারিত দেখান",
    ),
    topic(
        "service_rules",
        "চাকরি বিধিমালা",
        "6.4",
        "গ্র্যাচুইটি / সুবিধা",
        ["গ্র্যাচুইটি", "gratuity", "benefits"],
        "۶.۴ গ্র্যাচুইটি বা সুবিধা দেখান",
    ),
    topic(
        "service_rules",
        "চাকরি বিধিমালা",
        "6.5",
        "ছুটি",
        [
            "ছুটি",
            "ছুটির",
            "ছুটির নিয়ম",
            "ছুটির নিয়মাবলী",
            "ছুটির নীতিমালা",
            "ছুটির প্রকারভেদ",
            "নৈমিত্তিক ছুটি",
            "বার্ষিক ছুটি",
            "বাৎসরিক ছুটি",
            "অসুস্থতাজনিত ছুটি",
            "মাতৃত্বজনিত ছুটি",
            "পিতৃত্বজনিত ছুটি",
            "বিনা বেতনে ছুটি",
            "leave",
            "leave policy",
            "leave rules",
            "leave types",
            "casual leave",
            "annual leave",
            "sick leave",
            "maternity leave",
            "paternity leave",
            "leave without pay",
        ],
        "ছুটির নিয়মাবলী দেখান",
        exclude=[
            "প্রকল্প কর্মীদের ছুটি",
            "প্রকল্প কর্মী",
            "project employee",
            "প্রকল্প কর্মীদের",
        ],
        allow_topic_fallback=True,
        alternate_clauses=[
            "5.1",
            "5.2",
            "5.2.1",
            "5.2.2",
            "5.2.3",
            "5.2.4",
            "5.2.5",
            "5.2.6",
            "5.2.7",
            "6.5",
        ],
    ),
    topic(
        "service_rules",
        "চাকরি বিধিমালা",
        "6.6",
        "বেতন-ভাতা",
        ["বেতন-ভাতা", "বেতন", "ভাতা", "salary", "allowance"],
        "۶.۶ বেতন-ভাতা নীতিমালা দেখান",
    ),
    topic(
        "service_rules",
        "চাকরি বিধিমালা",
        "6.7",
        "ভ্রমণ / TA-DA",
        ["ভ্রমণ", "ta-da", "travel"],
        "۶.۷ ভ্রমণ বা TA-DA নিয়ম দেখান",
    ),
    topic(
        "service_rules",
        "চাকরি বিধিমালা",
        "6.8",
        "চিকিৎসা সুবিধা",
        ["চিকিৎসা", "medical benefit"],
        "۶.۸ চিকিৎসা সুবিধা দেখান",
    ),
    topic(
        "service_rules",
        "চাকরি বিধিমালা",
        "6.9",
        "অবসর",
        ["অবসর", "retirement"],
        "۶.۹ অবসর نীতিমালা দেখান",
    ),
    topic(
        "service_rules",
        "চাকরি বিধিমালা",
        "6.10",
        "অন্যান্য সুবিধা",
        ["অন্যান্য সুবিধা", "other benefits"],
        "۶.۱۰ অন্যান্য সুবিধা দেখান",
    ),
    topic(
        "service_rules",
        "চাকরি বিধিমালা",
        "6.11",
        "চাকরি সম্পর্কিত বিধান",
        ["চাকরি সম্পর্কিত বিধান", "service-related"],
        "۶.۱۱ চাকরি সম্পর্কিত বিধান দেখান",
    ),
    topic(
        "service_rules",
        "চাকরি বিধিমালা",
        "6.17",
        "বিশেষ বিধান",
        ["বিশেষ বিধান", "special provisions"],
        "۶.۱۷ বিশেষ বিধান দেখان",
    ),

    topic(
        "project_policy",
        "প্রকল্প নীতিমালা",
        "7.1",
        "প্রকল্পভিত্তিক কার্যক্রম",
        ["প্রকল্পভিত্তিক কার্যক্রম", "project-based activities"],
        "۷.۱ প্রকল্পভিত্তিক কার্যক্রম দেখান",
    ),
    topic(
        "project_policy",
        "প্রকল্প নীতিমালা",
        "7.2",
        "প্রকল্প কর্মী",
        ["প্রকল্প কর্মী", "project employee"],
        "۷.۲ প্রকল্প কর্মী নীতিমালা দেখান",
    ),
    topic(
        "project_policy",
        "প্রকল্প নীতিমালা",
        "7.3",
        "প্রকল্প সুবিধা",
        ["প্রকল্প সুবিধা", "project benefits"],
        "۷.۳ প্রকল্প সুবিধা দেখান",
    ),
    topic(
        "project_policy",
        "প্রকল্প নীতিমালা",
        "7.4",
        "প্রকল্প সমাপ্তি",
        ["প্রকল্প সমাপ্তি", "project closure"],
        "۷.۴ প্রকল্প সমাপ্তি নীতিমালা দেখান",
    ),

    topic(
        "contract_policy",
        "চুক্তিভিত্তিক কর্মী নীতিমালা",
        "8.1",
        "চুক্তিভিত্তিক নিয়োগ",
        ["চুক্তিভিত্তিক নিয়োগ", "contractual appointment"],
        "۸.۱ চুক্তিভিত্তিক নিয়োগ দেখান",
    ),
    topic(
        "contract_policy",
        "চুক্তিভিত্তিক কর্মী নীতিমালা",
        "8.2",
        "চুক্তির মেয়াদ",
        ["চুক্তির মেয়াদ", "contract duration"],
        "۸.۲ চুক্তির মেয়াদ দেখান",
    ),
    topic(
        "contract_policy",
        "চুক্তিভিত্তিক কর্মী নীতিমালা",
        "8.3",
        "দায়িত্ব",
        ["দায়িত্ব", "responsibilities"],
        "۸.۳ চুক্তিভিত্তিক কর্মীর দায়িত্ব দেখান",
    ),
    topic(
        "contract_policy",
        "চুক্তিভিত্তিক কর্মী নীতিমালা",
        "8.4",
        "সুবিধা",
        ["সুবিধা", "benefits"],
        "۸.۴ চুক্তিভিত্তিক কর্মীর সুবিধা দেখান",
    ),
    topic(
        "contract_policy",
        "চুক্তিভিত্তিক কর্মী নীতিমালা",
        "8.5",
        "চুক্তি সমাপ্তি",
        ["চুক্তি সমাপ্তি", "contract termination"],
        "۸.۵ চুক্তি সমাপ্তি নীতিমালা দেখان",
    ),
]


# ============================================================
# Exact-topic detection
# ============================================================

def detect_exact_topic_request(question: str) -> Dict[str, Any]:
    """
    Deterministic topic resolver.

    Important: one-word/broad topic requests such as:
      - বদলি
      - ছুটি
      - নিয়োগ
      - প্রবেশন
      - উপস্থিতি
    must be handled by the exact topic catalog before semantic/vector search.
    Otherwise Qdrant can return a related but wrong topic such as
    বদলি/স্থানান্তর ভাতা instead of ۱.۸ বদলি.
    """
    q_raw = clean(question)
    q = normalize_clause_key(q_raw).lower()
    q_compact = re.sub(r"\s+", " ", q).strip(" .?!।")
    clause = extract_ascii_clause_from_text(q)

    if not q_compact:
        return {}

    for item in EXACT_POLICY_TOPICS:
        item_clause = clean(item.get("clause"))
        title_bn = normalize_clause_key(item.get("title_bn")).lower().strip()
        aliases = [
            normalize_clause_key(a).lower().strip()
            for a in item.get("aliases", [])
            if clean(a)
        ]

        # Exact clause request, e.g. 1.8 / ۱.۸ / ১.৮
        if clause == item_clause:
            return item

        # Broken clause labels such as .۸ বদলি
        if clause.startswith("."):
            last_part = clause.replace(".", "", 1)
            if item_clause.endswith("." + last_part) and any(alias in q_compact for alias in aliases):
                return item

        # Exact one-word/title/alias request must win over vector search.
        # Example: "বদলি" should map to ۱.۸ বদলি, not ۶.۹ বদলি/স্থানান্তর ভাতা.
        if q_compact == title_bn or q_compact in aliases:
            return item

        # Clause + title/alias request.
        if item_clause and q_compact.startswith(item_clause) and (title_bn in q_compact or any(alias in q_compact for alias in aliases)):
            return item

        # Slash/composite requests, e.g. grade/topic written with separators.
        if "/" in q_compact and any(alias in q_compact for alias in aliases):
            return item

        # Natural topic request with an action marker or short English/Bangla phrase.
        # Examples that must resolve deterministically before vector search:
        #   tell about transfer
        #   about transfer
        #   transfer details
        #   ell about transfer   (common typo/missed first character)
        #   বদলি সম্পর্কে বলুন
        #   বদলি কী
        alias_hit = any(alias and alias in q_compact for alias in aliases)
        if alias_hit:
            marker_hit = any(
                marker in q_compact
                for marker in [
                    "দেখান", "কী", "কি", "ব্যাখ্যা", "বিস্তারিত",
                    "সম্পূর্ণ", "পূর্ণ", "ধারা", "নীতিমালা", "সম্পর্কে",
                    "বলুন", "বলেন", "জানতে", "দেখাও", "দাও",
                    "policy", "show", "explain", "details", "detail",
                    "full", "complete", "about", "tell", "ell about",
                    "what is", "describe", "definition", "summary",
                ]
            )
            short_topic_phrase = len(q_compact.split()) <= 5
            if marker_hit or short_topic_phrase:
                return item

    return {}


def exact_topic_not_found_answer(item: Dict[str, Any]) -> str:
    clause = display_clause_bn(item.get("clause", ""))
    title = clean(item.get("title_bn"))

    return (
        "Decision: নির্দিষ্ট টপিক পাওয়া যায়নি\n\n"
        "Answer:\n"
        f"আপনি `{clause} {title}` টপিকটি দেখতে চেয়েছেন, কিন্তু এই টপিকের সুনির্দিষ্ট ধারা/টেক্সট "
        "নীতিমালার ডেটা থেকে স্পষ্টভাবে পাওয়া যায়নি। তাই ভুল বা সম্পর্কহীন ধারা দেখানো হলো না।\n\n"
        "Policy reference:\n"
        f"{clause} {title} - Not found\n\n"
        "Follow-up questions:\n"
        f"- {title} বিষয়ে কাছাকাছি নীতিমালা খুঁজুন\n"
        "- টপিক তালিকা দেখান\n"
        "- সংশ্লিষ্ট ধারা নম্বর দিয়ে আবার চেষ্টা করুন"
    )


# ============================================================
# Common topic follow-up context
# ============================================================

GENERIC_FOLLOWUP_PHRASES = [
    "এই বিষয়ে বিস্তারিত",
    "এই বিষয়ে বিস্তারিত",
    "এই নীতির মূল শর্ত",
    "এই নীতির শর্ত",
    "নীতির মূল শর্ত",
    "মূল শর্তগুলো",
    "মূল শর্তগুলি",
    "এখানে কোনো ব্যতিক্রম",
    "এখানে কোন ব্যতিক্রম",
    "ব্যতিক্রম আছে",
    "সহজ করে ব্যাখ্যা",
    "۳টি পয়েন্টে",
    "৩টি পয়েন্টে",
    "তিনটি পয়েন্টে",
    "সংশ্লিষ্ট ধারা",
    "সম্পূর্ণ ধারা",
    "আরও বিস্তারিত",
    "আরো বিস্তারিত",
    "more details",
    "explain more",
    "main conditions",
    "key conditions",
    "exceptions",
    "summarize",
    "summary",
]


def is_generic_followup_question(question: str) -> bool:
    q = clean(question).lower()
    return bool(q) and any(phrase in q for phrase in GENERIC_FOLLOWUP_PHRASES)


def get_active_topic_title(active_topic: Dict[str, Any]) -> str:
    if not isinstance(active_topic, dict):
        return ""

    return clean(
        active_topic.get("topic_bn")
        or active_topic.get("topic_en")
        or active_topic.get("title_bn")
        or active_topic.get("title")
        or ""
    )


def get_active_topic_clause(active_topic: Dict[str, Any]) -> str:
    if not isinstance(active_topic, dict):
        return ""

    clause = clean(
        active_topic.get("clause_range")
        or active_topic.get("clause")
        or active_topic.get("policy_reference")
        or ""
    )

    if not clause:
        return ""

    parts = [clean(x) for x in clause.split(",") if clean(x)]

    return parts[0] if parts else clause


def build_active_topic_prefix(active_topic: Dict[str, Any]) -> str:
    clause = get_active_topic_clause(active_topic)
    title = get_active_topic_title(active_topic)

    if clause and title:
        return f"{clause} {title}"

    if title:
        return title

    if clause:
        return clause

    return ""


def resolve_contextual_followup_question(
    question: str,
    active_topic_from_request: Dict[str, Any] | None = None,
) -> str:
    question_clean = clean(question)

    if not question_clean or not active_topic_from_request:
        return question_clean

    if not is_generic_followup_question(question_clean):
        return question_clean

    prefix = build_active_topic_prefix(active_topic_from_request)

    if not prefix:
        return question_clean

    return f"{prefix} - {question_clean}"


def build_contextual_quick_actions(active_topic: Dict[str, Any]) -> List[str]:
    prefix = build_active_topic_prefix(active_topic)

    if not prefix:
        return [
            "আরও বিস্তারিত দেখান",
            "সংশ্লিষ্ট ধারা দেখান",
            "সহজ করে ব্যাখ্যা করুন",
            "۳টি পয়েন্টে বলুন",
        ]

    return [
        f"{prefix} নীতির মূল শর্তগুলো কী?",
        f"{prefix} বিস্তারিত দেখান",
        f"{prefix} সহজ করে ব্যাখ্যা করুন",
        f"{prefix} নীতিতে কোনো ব্যতিক্রম আছে কি?",
        f"{prefix} ۳টি পয়েন্টে বলুন",
    ]


def build_missing_context_payload(question: str, collection_name: str) -> Dict[str, Any]:
    quick_actions = [
        "টপিক তালিকা দেখান",
        "۱.۸ বদলি নীতিমালা দেখান",
        "۶.۳ প্রভিডেন্ট ফান্ড বিস্তারিত দেখান",
    ]

    answer = (
        "Decision: পূর্বের টপিক পাওয়া যায়নি\n\n"
        "Answer:\n"
        "আপনি একটি follow-up প্রশ্ন করেছেন, কিন্তু কোন নীতিমালার উপর follow-up করছেন তা পাওয়া যায়নি। "
        "অনুগ্রহ করে প্রথমে একটি নির্দিষ্ট টপিক নির্বাচন করুন, যেমন `۱.۸ বদলি`, তারপর follow-up প্রশ্ন করুন।\n\n"
        "Policy reference:\n"
        "Not found\n\n"
        "Follow-up questions:\n"
        "- টপিক তালিকা দেখান\n"
        "- ۱.۸ বদলি নীতিমালা দেখান\n"
        "- ۶.۳ প্রভিডেন্ট ফান্ড বিস্তারিত দেখান"
    )

    route = {
        "category": "FOLLOWUP",
        "subtopic": "CONTEXT_MISSING",
        "employee_type": POLICY_DEFAULT_EMPLOYEE_TYPE,
        "expanded_query": question,
    }

    payload = {
        "ok": True,
        "question": question,
        "question_for_route": question,
        "context_question": question,
        "answer": answer,
        "raw_context": "",
        "route": route,
        "answer_mode": "context_missing",
        "validation": {
            "valid": False,
            "reason": "generic_followup_without_active_topic",
        },
        "detected_topic": {},
        "active_topic": {},
        "citations": [],
        "suggestions": quick_actions,
        "related_questions": quick_actions,
        "must_followup_questions": [],
        "clarification_questions": [],
        "quick_actions": quick_actions,
        "topic_buttons": [],
        "needs_clarification": True,
        "interactive": {
            "interactive_enabled": True,
            "active_topic": {},
            "quick_actions": quick_actions,
            "topic_buttons": [],
            "needs_clarification": True,
            "default_employee_type": POLICY_DEFAULT_EMPLOYEE_TYPE,
            "route": route,
            "answer_mode": "context_missing",
        },
        "topic_groups": [],
        "topic_list_mode": False,
        "topics_count": 0,
    }

    if POLICY_STREAM_DEBUG:
        payload["debug"] = {
            "collection": collection_name,
            "code_version": POLICY_CODE_VERSION,
        }

    return payload


# ============================================================
# Topic list response
# ============================================================

def is_policy_topic_list_request(question: str) -> bool:
    q = normalize_clause_key(question).lower()
    q = re.sub(r"\s+", " ", q).strip(" .?!।")

    if not q:
        return False

    exact_requests = {
        "show topics list",
        "show topic list",
        "show all topics",
        "show topics",
        "topics list",
        "topic list",
        "all topics",
        "policy topics",
        "policy topic list",
        "policy topics list",
        "available topics",
        "available policy topics",
        "show menu",
        "policy menu",
        "menu",
        "টপিক",
        "টপিক তালিকা",
        "সব টপিক",
        "সব টপিক দেখান",
        "সকল টপিক",
        "নীতিমালার টপিক",
        "নীতিমালার টপিক তালিকা",
        "বিষয় তালিকা",
        "বিষয় তালিকা",
        "সব বিষয়",
        "সব বিষয়",
        "নীতিমালার বিষয়",
        "নীতিমালার বিষয়",
        "মেনু",
        "নীতিমালার মেনু",
    }

    if q in exact_requests:
        return True

    phrase_requests = [
        "show topics list",
        "show topic list",
        "show all topics",
        "show policy topics",
        "list policy topics",
        "list all topics",
        "available policy topics",
        "policy topics list",
        "নীতিমালার টপিক",
        "টপিক তালিকা",
        "সব টপিক",
        "সকল টপিক",
        "বিষয় তালিকা",
        "বিষয় তালিকা",
        "নীতিমালার মেনু",
    ]

    return any(term in q for term in phrase_requests)


def grouped_policy_topics() -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}

    for item in EXACT_POLICY_TOPICS:
        group_id = clean(item.get("group_id")) or "policy"
        group_bn = clean(item.get("group_bn")) or "নীতিমালা"

        if group_id not in seen:
            seen[group_id] = {
                "group_id": group_id,
                "title_bn": group_bn,
                "topics": [],
            }
            groups.append(seen[group_id])

        seen[group_id]["topics"].append(item)

    return groups


def get_policy_topics_count() -> int:
    return len(EXACT_POLICY_TOPICS)


def get_topic_sample_questions(limit: int = 5) -> List[str]:
    questions: List[str] = []

    for item in EXACT_POLICY_TOPICS:
        sample = clean(item.get("question"))

        if sample:
            questions.append(sample)

        if len(questions) >= limit:
            return questions

    return questions


def build_policy_topic_buttons() -> List[Dict[str, str]]:
    buttons: List[Dict[str, str]] = []

    for item in EXACT_POLICY_TOPICS:
        clause = display_clause_bn(item.get("clause"))
        title_bn = clean(item.get("title_bn"))
        group_bn = clean(item.get("group_bn"))
        sample_question = clean(item.get("question")) or f"{clause} {title_bn} দেখান"
        label = f"{clause} {title_bn}"

        buttons.append(
            {
                "label": label,
                "title": label,
                "tooltip": group_bn,
                "text": sample_question,
                "question": sample_question,
                "value": sample_question,
                "type": "policy_topic",
                "group": group_bn,
                "clause": clause,
                "topic_bn": title_bn,
            }
        )

    return buttons


def build_policy_topics_answer() -> str:
    lines: List[str] = [
        "Decision: নীতিমালার টপিক তালিকা",
        "",
        "Answer:",
        "নিচে বর্তমানে কনফিগার করা নীতিমালার টপিক তালিকা দেওয়া হলো:",
        "",
    ]

    for group_no, group in enumerate(grouped_policy_topics(), start=1):
        lines.append(f"{group_no}. {clean(group.get('title_bn'))}")

        for item in group.get("topics") or []:
            lines.append(
                f"   - {display_clause_bn(item.get('clause'))} {clean(item.get('title_bn'))}"
            )

        lines.append("")

    lines.extend(
        [
            "Policy reference:",
            "Topic catalog",
            "",
            "Follow-up questions:",
        ]
    )

    for question in get_topic_sample_questions(limit=3):
        lines.append(f"- {question}")

    return "\n".join(lines).strip()


def build_policy_topics_payload(
    question: str,
    question_norm: str,
    collection_name: str,
) -> Dict[str, Any]:
    route = {
        "category": "TOPIC_CATALOG",
        "subtopic": "POLICY_TOPICS_LIST",
        "employee_type": POLICY_DEFAULT_EMPLOYEE_TYPE,
        "expanded_query": question_norm,
    }

    quick_actions = [
        "۶.۳ প্রভিডেন্ট ফান্ড বিস্তারিত দেখান",
        "۱.۱۰ অফিস সময় দেখান",
        "۲.۴ দক্ষতা মূল্যায়ন পদ্ধতি ব্যাখ্যা করুন",
        "۳.۲ অসদাচরণ কী",
        "ছুটির নিয়মাবলী দেখান",
    ]

    topic_buttons = build_policy_topic_buttons()

    payload = {
        "ok": True,
        "question": question,
        "question_for_route": question_norm,
        "context_question": question_norm,
        "answer": build_policy_topics_answer(),
        "raw_context": "",
        "route": route,
        "answer_mode": "topic_list",
        "validation": {
            "valid": True,
            "reason": "deterministic_policy_topic_catalog",
        },
        "detected_topic": {},
        "active_topic": {},
        "citations": [],
        "suggestions": quick_actions,
        "related_questions": quick_actions,
        "must_followup_questions": [],
        "clarification_questions": [],
        "quick_actions": quick_actions,
        "topic_buttons": topic_buttons,
        "needs_clarification": False,
        "interactive": {
            "interactive_enabled": True,
            "active_topic": {},
            "must_followup_questions": [],
            "clarification_questions": [],
            "quick_actions": quick_actions,
            "topic_buttons": topic_buttons,
            "needs_clarification": False,
            "default_employee_type": POLICY_DEFAULT_EMPLOYEE_TYPE,
            "route": route,
            "answer_mode": "topic_list",
        },
        "topic_groups": [],
        "topic_list_mode": True,
        "topics_count": get_policy_topics_count(),
    }

    if POLICY_STREAM_DEBUG:
        payload["debug"] = {
            "collection": collection_name,
            "qdrant_url": get_qdrant_url(check_health=False),
            "ollama_url": get_ollama_url(),
            "embed_model": get_embed_model(),
            "answer_model": get_policy_answer_model(),
            "vector_name": POLICY_VECTOR_NAME,
            "route": route,
            "citations_count": 0,
            "context_chars": 0,
            "deterministic_handler": "policy_topic_list",
            "code_version": POLICY_CODE_VERSION,
        }

    return payload


# ============================================================
# Normalization and route detection
# ============================================================

def normalize_policy_question(question: str) -> str:
    q = clean(question)

    if not q:
        return q

    exact_topic = detect_exact_topic_request(q)

    if exact_topic and not is_generic_followup_question(q):
        return clean(exact_topic.get("question") or q)

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


def is_recruitment_question(question: str) -> bool:
    q = clean(question).lower()

    return any(
        term in q
        for term in ["নিয়োগ", "নিয়োগ", "recruitment", "appointment", "hiring"]
    )


def detect_policy_route(question: str) -> Dict[str, str]:
    q = clean(question).lower()
    employee_type = _detect_requested_employee_type(question)

    route = {
        "category": "",
        "subtopic": "",
        "employee_type": employee_type,
        "expanded_query": question,
    }

    salary_terms = [
        "salary", "salary policy", "salary structure", "pay", "pay scale",
        "payment", "wage", "wages", "remuneration", "compensation",
        "basic salary", "gross salary", "net salary", "বেতন",
        "বেতন কাঠামো", "বেতন স্কেল", "মূল বেতন", "গ্রস বেতন",
        "নেট বেতন", "পারিশ্রমিক", "মজুরি",
    ]

    separation_terms = [
        "কর্ম বিচ্ছেদ", "চাকুরী বিচ্ছেদ", "চাকরি বিচ্ছেদ",
        "অব্যাহতি", "পদত্যাগ", "বরখাস্ত", "termination",
        "separation", "employment separation", "resignation", "dismissal",
    ]

    leave_types_terms = [
        "types of leave", "leave types", "type of leave", "kinds of leave",
        "list of leave", "leave list", "ছুটির প্রকারভেদ",
        "ছুটির ধরন", "ছুটির ধরণ", "ছুটির প্রকার",
        "কত ধরনের ছুটি", "কি কি ছুটি", "কী কী ছুটি",
    ]

    sick_terms = [
        "sick leave", "medical leave", "illness leave", "অসুস্থতার ছুটি",
        "অসুস্থতাজনিত ছুটি", "অসুস্থতা জনিত ছুটি", "অসুস্থ",
    ]

    annual_terms = [
        "annual leave", "earned leave", "yearly leave",
        "বার্ষিক ছুটি", "বাৎসরিক ছুটি",
    ]

    casual_terms = ["casual leave", "নৈমিত্তিক ছুটি"]

    maternity_terms = [
        "maternity leave", "মাতৃত্বকালীন ছুটি", "মাতৃত্বজনিত ছুটি",
    ]

    paternity_terms = [
        "paternity leave", "পিতৃত্বকালীন ছুটি", "পিতৃত্বজনিত ছুটি",
    ]

    unpaid_terms = [
        "leave without pay", "without pay", "unpaid leave",
        "বিনা বেতনে ছুটি", "বিনা বেতনে",
    ]

    general_leave_terms = [
        "leave policy", "hr leave policy", "leave rules", "leave rule",
        "ছুটির নিয়ম", "ছুটির নিয়মাবলী", "ছুটির নীতিমালা", "ছুটি নীতিমালা",
    ]

    overtime_terms = [
        "অধিকাল ভাতা", "overtime", "overtime allowance", "ছুটির দিনে কাজ",
    ]

    if is_recruitment_question(q):
        route.update(
            {
                "category": "RECRUITMENT",
                "subtopic": "GENERAL_RECRUITMENT",
                "expanded_query": (
                    "۱.۵ নিয়োগ নিয়মিত কর্মী নিয়োগ নীতিমালা recruitment "
                    "regular employee recruitment appointment hiring মানবসম্পদ নিয়োগ চাকরির নিয়োগ"
                ),
            }
        )
        return route

    if any(t in q for t in salary_terms):
        route.update(
            {
                "category": "COMPENSATION",
                "subtopic": "SALARY",
                "expanded_query": (
                    "বেতন বেতন কাঠামো বেতন স্কেল মূল বেতন গ্রস বেতন নেট বেতন "
                    "salary salary structure pay scale basic salary gross salary net salary "
                    "remuneration compensation wage wages payment"
                ),
            }
        )
        return route

    if any(t in q for t in separation_terms):
        route.update(
            {
                "category": "EMPLOYMENT_END",
                "subtopic": "WORK_SEPARATION",
                "expanded_query": (
                    "কর্ম বিচ্ছেদ চাকুরী বিচ্ছেদ চাকরি বিচ্ছেদ অব্যাহতি পদত্যাগ "
                    "termination employment separation resignation dismissal ۴.۱ 4.1"
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
                    "۵.۲ 5.2 নৈমিত্তিক ছুটি বার্ষিক ছুটি অসুস্থতাজনিত ছুটি "
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

    if any(t in q for t in general_leave_terms) or "leave" in q or "ছুটি" in q or "ছুটির" in q:
        route.update(
            {
                "category": "LEAVE",
                "subtopic": "GENERAL_LEAVE_RULES",
                "expanded_query": (
                    "HR Policy ছুটির নিয়মাবলী leave policy leave rules general leave rules "
                    "leave application leave approval unauthorized absence leave without pay "
                    "ছুটির প্রকারভেদ নৈমিত্তিক ছুটি বার্ষিক ছুটি অসুস্থতাজনিত ছুটি"
                ),
            }
        )
        return route

    return route


def is_full_policy_request(question: str) -> bool:
    q = clean(question).lower()

    terms = [
        "full policy", "complete policy", "whole policy", "full clause",
        "exact clause", "complete clause", "show full", "show complete",
        "entire policy", "পুরো নীতিমালা", "সম্পূর্ণ নীতিমালা",
        "পুরো ধারা", "সম্পূর্ণ ধারা", "পুরো নিয়ম", "সম্পূর্ণ নিয়ম",
    ]

    return any(t in q for t in terms)


def is_list_or_reason_question(question: str) -> bool:
    q = clean(question).lower()

    terms = [
        "কারণ", "কারণগুলো", "কি কি", "কী কী", "তালিকা",
        "প্রকার", "ধরন", "ধরণ", "list", "types", "kinds",
        "causes", "reasons", "what are",
    ]

    return any(t in q for t in terms)


def extract_list_items_from_context(context: str, max_items: int = 15) -> List[str]:
    lines = [clean(x) for x in (context or "").splitlines()]
    items: List[str] = []

    for line in lines:
        if not line or line.startswith("[Source"):
            continue

        cleaned = re.sub(
            r"^(?:[-•*]+|ক্স|[0-9]+[.)]|[۰-۹]+[.)]|[۰-۹]+[.)]|\([^)]+\)|[কখগঘঙচছজঝঞটঠডঢণতথদধনপফবভমযরলশষসহ]\))\s*",
            "",
            line,
        ).strip()

        if cleaned == line:
            continue

        cleaned = clean(cleaned)

        if not cleaned or len(cleaned) > 140:
            continue

        if cleaned not in items:
            items.append(cleaned)

        if len(items) >= max_items:
            break

    return items


# ============================================================
# Relevance guard
# ============================================================

def is_context_relevant_for_route(route: Dict[str, str], context: str) -> bool:
    subtopic = clean(route.get("subtopic")).upper()
    category = clean(route.get("category")).upper()
    text = clean(context).lower()

    if not subtopic and not category:
        return True

    salary_terms = [
        "salary", "pay scale", "basic salary", "gross salary", "net salary",
        "remuneration", "compensation", "wage", "wages", "payment",
        "বেতন", "বেতন কাঠামো", "বেতন স্কেল", "মূল বেতন", "গ্রস বেতন",
        "নেট বেতন", "পারিশ্রমিক", "মজুরি",
    ]

    leave_terms = [
        "leave", "ছুটি", "ছুটির", "নৈমিত্তিক", "বার্ষিক", "বাৎসরিক",
        "অসুস্থতাজনিত", "অসুস্থতার", "মাতৃত্ব", "পিতৃত্ব", "বিনা বেতনে",
    ]

    separation_terms = [
        "কর্ম বিচ্ছেদ", "পদত্যাগ", "অবসর", "অব্যাহতি", "termination",
        "separation", "resignation", "dismissal", "retirement",
    ]

    allowance_terms = ["allowance", "ভাতা", "অধিকাল ভাতা", "overtime"]

    recruitment_terms = ["নিয়োগ", "নিয়োগ", "recruitment", "appointment", "hiring"]

    if subtopic == "GENERAL_RECRUITMENT" or category == "RECRUITMENT":
        return any(t in text for t in recruitment_terms)

    if subtopic == "SALARY" or category == "COMPENSATION":
        return any(t in text for t in salary_terms)

    if category == "LEAVE":
        return any(t in text for t in leave_terms)

    if category == "EMPLOYMENT_END" or subtopic == "WORK_SEPARATION":
        return any(t in text for t in separation_terms)

    if category == "ALLOWANCE":
        return any(t in text for t in allowance_terms)

    return True


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
        must.append(FieldCondition(key="employee_type", match=MatchValue(value=employee_type)))

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


def _payload_search_blob(payload: Dict[str, Any]) -> str:
    parts = []

    for key in [
        "topic_id", "topic_bn", "topic_en", "clause_range", "source_file",
        "category", "subtopic", "employee_type", "text", "policy_text",
        "content", "body", "rules", "clause_text", "chunk_text",
        "page_content", "embed_text",
    ]:
        value = clean(payload.get(key))

        if value:
            parts.append(value)

    return normalize_clause_key(" ".join(parts)).lower()


def _payload_clause_matches(payload: Dict[str, Any], clause_key: str) -> bool:
    wanted = normalize_clause_key(clause_key)

    if not wanted:
        return False

    for key in ["clause_range", "topic_id"]:
        value = normalize_clause_key(payload.get(key))

        if not value:
            continue

        if value.startswith(wanted):
            return True

        pattern = rf"(^|[^0-9.]){re.escape(wanted)}($|[^0-9])"

        if re.search(pattern, value):
            return True

    text = normalize_clause_key(_payload_text(payload))
    first_500 = text[:500]

    if first_500.startswith(wanted):
        return True

    pattern = rf"(^|[^0-9.]){re.escape(wanted)}($|[^0-9])"

    return re.search(pattern, first_500) is not None


def scroll_payload_by_clause_or_topic(
    qdrant: QdrantClient,
    collection: str,
    clause_prefix: str = "",
    topic_terms: List[str] | None = None,
    exclude_terms: List[str] | None = None,
    max_points: int = 3000,
) -> Dict[str, Any]:
    topic_terms_norm = [
        normalize_clause_key(x).lower()
        for x in (topic_terms or [])
        if clean(x)
    ]

    exclude_terms_norm = [
        normalize_clause_key(x).lower()
        for x in (exclude_terms or [])
        if clean(x)
    ]

    clause_key = normalize_clause_key(clause_prefix)
    next_offset = None
    scanned = 0

    while scanned < max_points:
        try:
            points, next_offset = qdrant.scroll(
                collection_name=collection,
                offset=next_offset,
                limit=100,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            logger.warning("Qdrant deterministic topic scroll failed: %s", e)
            return {}

        if not points:
            break

        for point in points:
            scanned += 1
            payload = _point_payload(point)

            if not payload or not _payload_text(payload):
                continue

            blob = _payload_search_blob(payload)

            if exclude_terms_norm and any(term in blob for term in exclude_terms_norm):
                continue

            if clause_key and _payload_clause_matches(payload, clause_key):
                return payload

            if topic_terms_norm and any(term in blob for term in topic_terms_norm):
                return payload

        if next_offset is None:
            break

    return {}


def find_regular_leave_payload(
    qdrant: QdrantClient,
    collection: str,
    max_points: int = 10000,
) -> Dict[str, Any]:
    """
    Dedicated resolver for regular leave policy.

    UI may say ۶.۵ ছুটি, but real policy text may be stored as ۵.۱ / ۵.۲
    or only as text like ছুটির প্রকারভেদ / নৈমিত্তিক ছুটি.
    """
    positive_terms = [
        "ছুটির নিয়ম",
        "ছুটির নিয়মাবলী",
        "ছুটির নীতিমালা",
        "ছুটির প্রকারভেদ",
        "নৈমিত্তিক ছুটি",
        "বার্ষিক ছুটি",
        "বাৎসরিক ছুটি",
        "অসুস্থতাজনিত ছুটি",
        "মাতৃত্বজনিত ছুটি",
        "পিতৃত্বজনিত ছুটি",
        "বিনা বেতনে ছুটি",
        "leave policy",
        "leave rules",
        "leave types",
        "casual leave",
        "annual leave",
        "sick leave",
        "maternity leave",
        "paternity leave",
    ]

    strong_clause_prefixes = [
        "5.1",
        "5.2",
        "5.2.1",
        "5.2.2",
        "5.2.3",
        "5.2.4",
        "5.2.5",
        "5.2.6",
        "5.2.7",
        "6.5",
    ]

    reject_terms = [
        "প্রকল্প কর্মীদের ছুটি",
        "প্রকল্প কর্মী",
        "প্রকল্প কর্মীদের",
        "project employee",
        "project staff",
        "۷.۴",
        "7.4",
    ]

    next_offset = None
    scanned = 0
    best_payload: Dict[str, Any] = {}
    best_score = -1

    while scanned < max_points:
        try:
            points, next_offset = qdrant.scroll(
                collection_name=collection,
                offset=next_offset,
                limit=100,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            logger.warning("Forced leave resolver scroll failed: %s", e)
            return {}

        if not points:
            break

        for point in points:
            scanned += 1
            payload = _point_payload(point)

            if not payload:
                continue

            text = _payload_text(payload)

            if not text:
                continue

            blob_raw = " ".join(
                clean(payload.get(k))
                for k in [
                    "topic_id",
                    "topic_bn",
                    "topic_en",
                    "clause_range",
                    "source_file",
                    "category",
                    "subtopic",
                    "text",
                    "policy_text",
                    "content",
                    "body",
                    "rules",
                    "clause_text",
                    "chunk_text",
                    "page_content",
                    "embed_text",
                ]
                if payload.get(k)
            )

            blob = normalize_clause_key(blob_raw).lower()

            if any(normalize_clause_key(term).lower() in blob for term in reject_terms):
                continue

            score = 0

            for clause in strong_clause_prefixes:
                clause_norm = normalize_clause_key(clause)
                if clause_norm in blob:
                    score += 10

            for term in positive_terms:
                term_norm = normalize_clause_key(term).lower()
                if term_norm in blob:
                    score += 5

            if "ছুটি" in blob:
                score += 2

            if "leave" in blob:
                score += 2

            if score > best_score:
                best_score = score
                best_payload = payload

        if next_offset is None:
            break

    if best_score >= 5:
        return best_payload

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

    if subtopic == "GENERAL_RECRUITMENT":
        payload = scroll_payload_by_clause_or_topic(
            qdrant,
            collection,
            clause_prefix="1.5",
            topic_terms=["নিয়োগ", "recruitment"],
            exclude_terms=["প্রকল্প কর্মী", "project employee", "প্রকল্প কর্মীদের"],
        )

        if payload:
            return payload

        filters.extend(
            [
                _build_qdrant_filter(topic_bn="নিয়োগ"),
                _build_qdrant_filter(topic_bn="নিয়োগ"),
                _build_qdrant_filter(topic_en="Recruitment"),
                _build_qdrant_filter(topic_en="Appointment"),
            ]
        )

    if subtopic == "GENERAL_LEAVE_RULES":
        payload = find_regular_leave_payload(qdrant, collection)

        if payload:
            return payload

        payload = scroll_payload_by_clause_or_topic(
            qdrant,
            collection,
            topic_terms=["ছুটি", "ছুটির", "leave"],
            exclude_terms=["প্রকল্প কর্মী", "project employee", "প্রকল্প কর্মীদের"],
        )

        if payload:
            return payload

        filters.extend(
            [
                _build_qdrant_filter(topic_bn="ছুটি"),
                _build_qdrant_filter(topic_bn="ছুটির নিয়মাবলী"),
                _build_qdrant_filter(topic_bn="ছুটির প্রকারভেদ"),
                _build_qdrant_filter(topic_en="Leave"),
                _build_qdrant_filter(topic_en="Leave Rules"),
                _build_qdrant_filter(topic_en="Leave Policy"),
            ]
        )

    if subtopic == "SALARY":
        filters.extend(
            [
                _build_qdrant_filter(topic_id="policy_salary"),
                _build_qdrant_filter(topic_id="policy_salary_structure"),
                _build_qdrant_filter(topic_id="policy_pay_scale"),
                _build_qdrant_filter(topic_bn="বেতন"),
                _build_qdrant_filter(topic_bn="বেতন কাঠামো"),
                _build_qdrant_filter(topic_bn="বেতন স্কেল"),
                _build_qdrant_filter(topic_en="Salary"),
                _build_qdrant_filter(topic_en="Salary Structure"),
                _build_qdrant_filter(topic_en="Pay Scale"),
                _build_qdrant_filter(topic_en="Compensation"),
            ]
        )

    if subtopic == "LEAVE_TYPES":
        payload = find_regular_leave_payload(qdrant, collection)

        if payload:
            return payload

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

    response = requests.post(
        f"{ollama_url}/api/embeddings",
        json={
            "model": get_embed_model(),
            "prompt": text,
        },
        timeout=(10, 180),
    )
    response.raise_for_status()

    data = response.json()
    vector = data.get("embedding") or []

    return vector if isinstance(vector, list) and vector else []


def qdrant_vector_search(
    qdrant: QdrantClient,
    collection: str,
    vector: List[float],
    top_k: int = POLICY_TOP_K,
) -> List[Any]:
    errors = []

    if POLICY_VECTOR_NAME:
        try:
            return qdrant.search(
                collection_name=collection,
                query_vector=(POLICY_VECTOR_NAME, vector),
                limit=top_k,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            errors.append(f"{POLICY_VECTOR_NAME}: {str(e)}")

    try:
        return qdrant.search(
            collection_name=collection,
            query_vector=("default", vector),
            limit=top_k,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        errors.append(f"default: {str(e)}")

    try:
        return qdrant.search(
            collection_name=collection,
            query_vector=vector,
            limit=top_k,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        errors.append(f"unnamed: {str(e)}")

    logger.error("Qdrant vector search failed: %s", " | ".join(errors))

    return []


def build_citation(
    payload: Dict[str, Any],
    idx: int,
    score: Any = None,
) -> Dict[str, Any]:
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
    refs = [
        clean(c.get("clause_range"))
        for c in citations or []
        if clean(c.get("clause_range"))
    ]

    refs = _unique_clean(refs, limit=3)

    if refs:
        return ", ".join(refs)

    normalized_text = normalize_clause_key(text or "")
    found = re.findall(r"\d+\.\d+(?:\.\d+)?", normalized_text)

    if found:
        return ", ".join(_unique_clean([display_clause_bn(x) for x in found], limit=5))

    return "Not found"


def retrieve_policy_context(
    question: str,
    collection: str,
) -> Tuple[str, List[Dict[str, Any]], Dict[str, str], Dict[str, Any]]:
    qdrant = get_qdrant_client()
    exact_topic = detect_exact_topic_request(question)
    question_norm = normalize_policy_question(question)
    route = detect_policy_route(question_norm)
    payloads: List[Tuple[Dict[str, Any], Any]] = []

    if exact_topic:
        clause_key = clean(exact_topic.get("clause"))
        aliases = exact_topic.get("aliases", [])
        exclude_terms = exact_topic.get("exclude", [])
        allow_topic_fallback = bool(exact_topic.get("allow_topic_fallback", False))
        alternate_clauses = exact_topic.get("alternate_clauses", []) or []
        is_leave_topic = clean(exact_topic.get("title_bn")) == "ছুটি"

        exact_payload = {}

        # 1. Try exact UI/catalog clause first.
        if clause_key:
            exact_payload = scroll_payload_by_clause_or_topic(
                qdrant,
                collection,
                clause_prefix=clause_key,
                topic_terms=[],
                exclude_terms=exclude_terms,
            )

        if exact_payload:
            payloads.append((exact_payload, None))

        # 2. Try alternate real document clauses.
        # Example: UI topic ۶.۵ ছুটি, but real leave clauses may be ۵.۱ / ۵.۲.
        if not payloads and alternate_clauses:
            for alt_clause in alternate_clauses:
                exact_payload = scroll_payload_by_clause_or_topic(
                    qdrant,
                    collection,
                    clause_prefix=alt_clause,
                    topic_terms=[],
                    exclude_terms=exclude_terms,
                )

                if exact_payload:
                    payloads.append((exact_payload, None))
                    break

        # 2.5. Dedicated regular leave resolver.
        if not payloads and is_leave_topic:
            leave_payload = find_regular_leave_payload(
                qdrant=qdrant,
                collection=collection,
            )

            if leave_payload:
                payloads.append((leave_payload, None))

        # 3. Try topic text fallback.
        if not payloads and (allow_topic_fallback or aliases):
            exact_payload = scroll_payload_by_clause_or_topic(
                qdrant,
                collection,
                clause_prefix="",
                topic_terms=aliases,
                exclude_terms=exclude_terms,
            )

            if exact_payload:
                payloads.append((exact_payload, None))

        # 4. Exact clicked topic must never show unrelated semantic result.
        if not payloads:
            route.update(
                {
                    "category": "EXACT_TOPIC",
                    "subtopic": "NOT_FOUND",
                    "employee_type": POLICY_DEFAULT_EMPLOYEE_TYPE,
                    "expanded_query": question_norm,
                }
            )
            return exact_topic_not_found_answer(exact_topic), [], route, {}

    if not payloads:
        direct_payload = get_direct_payload_for_route(qdrant, collection, route)

        if direct_payload:
            payloads.append((direct_payload, None))

    if not payloads:
        vector = ollama_embed(route.get("expanded_query") or question_norm)

        if vector:
            for point in qdrant_vector_search(qdrant, collection, vector, top_k=POLICY_TOP_K):
                payload = _point_payload(point)

                if _payload_text(payload):
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

    context = "\n\n---\n\n".join(context_parts)[:POLICY_CONTEXT_MAX_CHARS]

    if context and not is_context_relevant_for_route(route, context):
        context = ""
        citations = []

    active_topic = citations[0] if citations else {}

    return context, citations, route, active_topic


# ============================================================
# Ollama answer generation
# ============================================================
def build_policy_prompt(
    question: str,
    context: str,
    citations: List[Dict[str, Any]],
    route: Dict[str, str],
    memory_context: str = "",
) -> str:
    clause_ref = get_clause_reference(citations, context)
    output_lang = "Bangla" if is_bn(question) or is_bn(context) else "English"

    return f"""
You are an HR policy assistant for an enterprise RAG system.

Use only the policy context below. Do not use outside knowledge. Do not invent any policy rule, number, amount, grade, day count, approval authority, benefit, eligibility, or exception.

Language: {output_lang}

Mandatory behavior:
- If the user asks a broad topic, give the relevant policy section.
- If the user asks a specific factual question, answer only the specific fact.
- If the user asks who approves/grants, answer only the approving/granting authority.
- If the user asks about qualification, eligibility, days, duration, amount, condition, process, exception, responsibility, benefit, penalty, document, definition, entitlement, or calculation, answer only that requested detail.
- If the user asks for list/types/categories, provide only the relevant list.
- If the user asks for summary or explanation, summarize or explain only the relevant part.
- If the user asks a natural topic request such as "tell about transfer", "about transfer", "describe transfer", or "বদলি সম্পর্কে বলুন", do not return only the heading/title. Give a short explanation with the main rules/conditions from the provided policy context.
- If the requested detail is not present in the policy context, say that it was not found in the provided policy text.
- Do not print source markers like [Source 1] or [Previous RAG Memory].
- Do not print the full retrieved context unless the user asks for full policy/complete clause or asks only a broad topic.
- Never output placeholder text. Never output angle brackets.

Previous RAG memory:
{memory_context if memory_context else "No previous RAG memory supplied."}

User question:
{question}

Detected retrieval route:
{json.dumps(route, ensure_ascii=False)}

Policy reference:
{clause_ref}

Policy context:
{context}

Return exactly this plain format with real content only:
Decision:
Short decision title.

Answer:
Direct answer.

Policy reference:
{clause_ref}

Follow-up questions:
- One relevant follow-up question.
- One relevant follow-up question.
- One relevant follow-up question.
""".strip()



def call_ollama_generate(prompt: str) -> str:
    ollama_url = get_ollama_url().rstrip("/")

    response = requests.post(
        f"{ollama_url}/api/generate",
        json={
            "model": get_policy_answer_model(),
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

    return clean_model_answer(response.json().get("response"))




# ============================================================
# Generic intent-aware answer extraction
# ============================================================

BN_EN_STOPWORDS = {
    "জন্য", "হবে", "করা", "করবে", "করবেন", "আছে", "নাকি", "হয়", "হয়",
    "যাবে", "দিতে", "পাবে", "পাওয়া", "পাওয়া", "সম্পর্কে", "বিষয়ে", "বিষয়ে",
    "minimum", "required", "tell", "show", "give", "please", "need", "for", "about",
}

INTENT_TERMS: Dict[str, List[str]] = {
    "approval_authority": [
        "অনুমোদন", "অনুমতি", "মঞ্জুর", "মঞ্জুরী", "মঞ্জুরীকৃত", "অনুমোদনকারী",
        "কে", "কারা", "authority", "approve", "approves", "approved", "approver", "grant", "grants",
    ],
    "qualification": [
        "শিক্ষাগত", "যোগ্যতা", "ন্যূনতম", "পাশ", "ডিগ্রী", "ডিগ্রি", "স্নাতক", "স্নাতকোত্তর",
        "মাধ্যমিক", "উচ্চ", "শিথিল", "গাড়ী", "গাড়ী", "চালক", "qualification", "education", "minimum",
    ],
    "duration": [
        "কতদিন", "কত দিন", "দিন", "মাস", "বছর", "সময়", "সময়", "মেয়াদ", "মেয়াদ",
        "duration", "period", "days", "months", "years", "time",
    ],
    "amount": [
        "টাকা", "পরিমাণ", "বেতন", "ভাতা", "স্কেল", "মূল", "গ্রস", "নেট", "amount", "salary", "allowance", "pay", "scale",
    ],
    "eligibility": [
        "যোগ্য", "যোগ্যতা", "শর্ত", "পাবে", "পাওয়ার", "পাওয়ার", "প্রাপ্য", "eligibility", "eligible", "condition",
    ],
    "exception": [
        "ব্যতিক্রম", "তবে", "শর্ত থাকে", "শিথিল", "exception", "except", "relax", "relaxed",
    ],
    "process": [
        "প্রক্রিয়া", "প্রক্রিয়া", "ধাপ", "কিভাবে", "কীভাবে", "আবেদন", "জমা", "process", "step", "apply", "application",
    ],
    "responsibility": [
        "দায়িত্ব", "দায়িত্ব", "কর্তব্য", "responsibility", "duty", "duties",
    ],
    "benefit": [
        "সুবিধা", "গ্র্যাচুইটি", "প্রভিডেন্ট", "ফান্ড", "benefit", "gratuity", "provident", "fund",
    ],
    "penalty": [
        "শাস্তি", "শাস্তিমূলক", "অসদাচরণ", "বরখাস্ত", "penalty", "punishment", "disciplinary", "misconduct",
    ],
    "definition": [
        "কী", "কি", "মানে", "সংজ্ঞা", "what is", "meaning", "definition",
    ],
    "list": [
        "কি কি", "কী কী", "প্রকার", "প্রকারভেদ", "ধরন", "ধরণ", "তালিকা", "list", "types", "kinds", "categories",
    ],
    "summary": ["সারাংশ", "সংক্ষেপ", "summary", "summarize", "brief"],
    "full_topic": [
        "full policy", "complete policy", "full clause", "complete clause", "পুরো", "সম্পূর্ণ", "সম্পূর্ণ ধারা", "পুরো ধারা",
    ],
}

BROAD_TOPIC_WORDS = {
    "ছুটি", "ছুটির", "শিক্ষাগত", "যোগ্যতা", "নিয়োগ", "নিয়োগ", "বদলি", "প্রবেশন",
    "উপস্থিতি", "অফিস", "সময়", "সময়", "বেতন", "ভাতা", "প্রভিডেন্ট", "গ্র্যাচুইটি",
    "অবসর", "আপিল", "তদন্ত", "অসদাচরণ", "শাস্তিমূলক", "দায়িত্ব", "কর্তব্য",
    "leave", "qualification", "education", "recruitment", "transfer", "probation", "salary",
    "allowance", "provident", "gratuity", "appeal", "investigation", "misconduct",
}


def _norm_text_for_match(value: Any) -> str:
    return normalize_clause_key(value).lower()


def _question_tokens(question: str, limit: int = 60) -> List[str]:
    normalized = _norm_text_for_match(question)
    tokens = re.findall(r"[a-zA-Z0-9_]+|[\u0980-\u09FF]+", normalized)
    out: List[str] = []

    for token in tokens:
        token = token.strip().lower()

        if len(token) < 2:
            continue

        if token in BN_EN_STOPWORDS:
            continue

        if token not in out:
            out.append(token)

        if len(out) >= limit:
            break

    return out


def _bn_token_variants(token: str) -> List[str]:
    token = clean(token).lower()
    variants = [token] if token else []

    suffixes = ["য়ের", "য়েরা", "য়ে", "য়", "ের", "তে", "কে", "টি", "টা", "র"]
    for suffix in suffixes:
        if token.endswith(suffix) and len(token) > len(suffix) + 2:
            base = token[: -len(suffix)]
            if base and base not in variants:
                variants.append(base)

    # Common orthographic equivalents.
    more = []
    for v in variants:
        more.append(v.replace("গাড়ী", "গাড়ী"))
        more.append(v.replace("গাড়ী", "গাড়ী"))
    for v in more:
        if v and v not in variants:
            variants.append(v)

    return variants


def _extract_grade_targets(question: str) -> List[str]:
    q = _norm_text_for_match(question)
    targets: List[str] = []

    for m in re.finditer(r"(?:গ্রেড|grade)\s*[-–]?\s*(\d+)", q):
        grade_no = m.group(1)
        for form in [f"গ্রেড-{grade_no}", f"গ্রেড {grade_no}", f"grade-{grade_no}", f"grade {grade_no}"]:
            if form not in targets:
                targets.append(form)

    return targets


def _extract_clause_targets(question: str) -> List[str]:
    q = normalize_clause_key(question)
    return _unique_clean(re.findall(r"\d+(?:\.\d+){1,3}", q), limit=10)


def classify_policy_question_type(question: str) -> str:
    q = _norm_text_for_match(question)
    tokens = _question_tokens(question)

    if is_full_policy_request(question):
        return "full_topic"

    if is_policy_topic_list_request(question):
        return "topic_list"

    exact_markers = [
        "?", "؟", "？", "কে", "কারা", "কত", "কখন", "কিভাবে", "কীভাবে",
        "কি", "কী", "অনুমোদন", "মঞ্জুর", "শিথিল", "পাবে", "হবে কি",
        "who", "how", "when", "what", "approve", "approval",
    ]

    has_exact_marker = (
        bool(_extract_grade_targets(question))
        or bool(_extract_clause_targets(question))
        or any(marker in q for marker in exact_markers)
    )

    # Natural explanation/topic-intro requests should be answered like ChatGPT:
    # "Tell about transfer" / "বদলি সম্পর্কে বলুন" means explain the topic,
    # not just return the heading or clause number.
    explanation_markers = [
        "ell about", "tell about", "about", "explain", "describe", "details", "detail",
        "summary", "summarize", "brief",
        "সম্পর্কে", "বলুন", "বলেন", "ব্যাখ্যা", "বিস্তারিত", "সারাংশ", "সংক্ষেপ",
    ]

    if any(_norm_text_for_match(term) in q for term in explanation_markers):
        return "summary"

    # Broad topic-like query must stay broad, for example: ছুটি, শিক্ষাগত যোগ্যতা, প্রভিডেন্ট ফান্ড.
    # Do this BEFORE intent detection, otherwise "শিক্ষাগত যোগ্যতা" becomes a specific qualification question.
    if 0 < len(tokens) <= 4 and not has_exact_marker:
        if any(t in BROAD_TOPIC_WORDS for t in tokens) or len(tokens) <= 3:
            return "broad_topic"

    # Exception/relaxation should override normal qualification intent.
    if any(_norm_text_for_match(term) in q for term in INTENT_TERMS.get("exception", [])):
        return "exception"

    for intent, terms in INTENT_TERMS.items():
        if intent == "full_topic":
            continue
        if any(_norm_text_for_match(term) in q for term in terms):
            return intent

    if _extract_grade_targets(question) or _extract_clause_targets(question):
        return "exact_fact"

    if has_exact_marker:
        return "exact_fact"

    return "broad_topic" if len(tokens) <= 4 else "exact_fact"

def is_broad_policy_topic_question(question: str) -> bool:
    return classify_policy_question_type(question) in {"broad_topic", "full_topic"}


def _clean_answer_context(context: str) -> str:
    lines = []
    for raw in (context or "").splitlines():
        line = clean(raw)
        if not line:
            continue
        if line == "---":
            continue
        if line.startswith("[Previous RAG Memory"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def split_policy_context_units(context: str) -> List[str]:
    units: List[str] = []

    for raw_line in (context or "").splitlines():
        line = clean(raw_line)

        if not line or line == "---":
            continue

        if line.startswith("[Source") or line.startswith("[Previous RAG Memory"):
            continue

        # Split Bangla sentences by danda but preserve the punctuation.
        parts = re.split(r"(?<=।)\s+", line)
        for part in parts:
            part = clean(part)
            if not part:
                continue
            units.append(part)

    # If a paragraph is very long and contains many clause numbers, split before later clause numbers.
    expanded: List[str] = []
    clause_pat = r"(?=\s*[০-৯0-9]+\.[۰-۹0-9]+(?:\.[۰-۹0-9]+)?\s+)"
    for unit in units:
        if len(unit) > 900:
            chunks = [clean(x) for x in re.split(clause_pat, unit) if clean(x)]
            expanded.extend(chunks if chunks else [unit])
        else:
            expanded.append(unit)

    # Deduplicate while preserving order.
    return _unique_clean(expanded, limit=200)


def _unit_has_any(unit_norm: str, terms: List[str]) -> bool:
    return any(_norm_text_for_match(term) in unit_norm for term in terms if clean(term))


def _intent_bonus_terms(answer_type: str) -> List[str]:
    terms = list(INTENT_TERMS.get(answer_type, []))

    # Some intents share important words.
    if answer_type in {"eligibility", "qualification", "exception"}:
        terms += INTENT_TERMS.get("qualification", []) + INTENT_TERMS.get("exception", [])
    if answer_type == "approval_authority":
        terms += ["ব্যবস্থাপক", "পরিচালক", "চেয়ারপার্সন", "চেয়ারপার্সন", "তত্ত্বাবধায়ক", "প্রধান"]

    return _unique_clean(terms, limit=80)


def _trim_unit_to_target(unit: str, question: str) -> str:
    grade_targets = _extract_grade_targets(question)
    if not grade_targets:
        return unit

    unit_norm = _norm_text_for_match(unit)
    target_present = any(target in unit_norm for target in grade_targets)

    if not target_present:
        return unit

    # Split common compound sentences where multiple grades are joined with 'এবং'.
    pieces = re.split(r"\s+এবং\s+|\s+and\s+", unit)
    chosen = []

    for piece in pieces:
        piece_norm = _norm_text_for_match(piece)
        if any(target in piece_norm for target in grade_targets):
            chosen.append(clean(piece))

    if chosen:
        joined = " এবং ".join(chosen)
        if unit.endswith("।") and not joined.endswith("।"):
            joined += "।"
        return joined

    return unit


def rank_policy_units(question: str, context: str, answer_type: str) -> List[Tuple[int, str]]:
    units = split_policy_context_units(context)
    q_norm = _norm_text_for_match(question)
    q_tokens = _question_tokens(question)
    grade_targets = _extract_grade_targets(question)
    clause_targets = _extract_clause_targets(question)
    intent_terms = _intent_bonus_terms(answer_type)

    ranked: List[Tuple[int, str]] = []

    for unit in units:
        unit_norm = _norm_text_for_match(unit)
        score = 0

        if len(unit) > 1600:
            score -= 20

        if unit.startswith("[Source"):
            score -= 100

        for target in grade_targets:
            if target in unit_norm:
                score += 45

        for clause in clause_targets:
            if clause and clause in normalize_clause_key(unit):
                score += 35

        for token in q_tokens:
            matched_token = False
            for variant in _bn_token_variants(token):
                if variant and variant in unit_norm:
                    matched_token = True
                    score += 8 if variant == token else 6
                    break

            # Location words are important for approval questions, e.g. শাখা কার্যালয়ের.
            if answer_type == "approval_authority" and token in {"কার্যালয়", "কার্যালয়ের", "কার্যালয়ে", "অফিস"}:
                if "কার্যাল" in unit_norm or "office" in unit_norm:
                    score += 25

        for term in intent_terms:
            term_norm = _norm_text_for_match(term)
            if term_norm and term_norm in unit_norm:
                score += 10

        q_has_exception = any(_norm_text_for_match(term) in q_norm for term in INTENT_TERMS.get("exception", []))

        # Strong negative examples: designation-only lines are bad for qualification questions.
        if answer_type in {"qualification", "eligibility", "exception"}:
            if ("যোগ্যতা" not in unit_norm and "শিক্ষাগত" not in unit_norm and "শিথিল" not in unit_norm and "qualification" not in unit_norm):
                if re.search(r"গ্রেড\s*[- ]?\s*\d+\s*[:：]", unit_norm):
                    score -= 80

            # If user did not ask about relaxation/exception, do not prefer exception lines.
            if not q_has_exception and ("শিথিল" in unit_norm or "তবে" in unit_norm or "exception" in unit_norm):
                score -= 45

            # If user asked about relaxation/exception, strongly prefer such lines.
            if q_has_exception and ("শিথিল" in unit_norm or "তবে" in unit_norm or "exception" in unit_norm):
                score += 45

        # For approval questions, prefer operational authority lines, not section headings.
        if answer_type == "approval_authority":
            if not _unit_has_any(unit_norm, ["মঞ্জুর করবেন", "মঞ্জুর", "approve", "grant"]):
                score -= 70
            if "অনুমোদনকারী" in unit_norm and "মঞ্জুর" not in unit_norm:
                score -= 50

        if score > 0:
            ranked.append((score, _trim_unit_to_target(unit, question)))

    # Deduplicate after trimming.
    ranked.sort(key=lambda x: x[0], reverse=True)
    deduped: List[Tuple[int, str]] = []
    seen = set()
    for score, unit in ranked:
        key = _norm_text_for_match(unit)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((score, unit))

    return deduped


def is_bad_model_answer(answer: str) -> bool:
    text = clean(answer).lower()

    if not text:
        return True

    bad_phrases = [
        "<short decision", "<direct answer", "<question 1", "<question 2", "<question 3",
        "<list related topics", "<any additional notes", "write a short title here",
        "write the direct answer here", "one relevant follow-up question", "direct answer.",
        "short decision title", "list related topics from the policy text",
        "any additional notes about the policy", "only the exact requested information",
    ]

    if any(x in text for x in bad_phrases):
        return True

    if "<" in answer and ">" in answer:
        return True

    return False


def is_model_answer_too_broad(question: str, answer: str, context: str) -> bool:
    if is_broad_policy_topic_question(question):
        return False

    answer_clean = clean(answer)
    context_clean = clean(context)

    if not answer_clean:
        return True

    # If exact answer repeats a large part of context, it is too broad.
    if len(answer_clean) > 1800:
        return True

    if context_clean and len(answer_clean) > 800:
        sample = answer_clean[:500]
        if sample in context_clean:
            return True

    if "[Source" in answer_clean or "[Previous RAG Memory" in answer_clean:
        return True

    return False


def _related_followups_for_type(answer_type: str, question: str) -> List[str]:
    if is_bn(question):
        if answer_type == "approval_authority":
            return ["এই অনুমোদন প্রক্রিয়ার সম্পূর্ণ ধারা দেখান।", "এখানে কোনো ব্যতিক্রম আছে কি?", "এই বিষয়ে দায়িত্ব কার?" ]
        if answer_type in {"qualification", "eligibility"}:
            return ["এই যোগ্যতার সম্পূর্ণ ধারা দেখান।", "আর কোন গ্রেডের যোগ্যতা আছে?", "এখানে কোনো শিথিলতার নিয়ম আছে কি?"]
        if answer_type == "duration":
            return ["এই সময়সীমার শর্ত কী?", "সম্পূর্ণ ধারা দেখান।", "এখানে কোনো ব্যতিক্রম আছে কি?"]
        if answer_type == "amount":
            return ["এই সুবিধার শর্ত কী?", "কারা এই সুবিধা পাবে?", "সম্পূর্ণ ধারা দেখান।"]
        return ["এই বিষয়ে বিস্তারিত কী?", "সংশ্লিষ্ট সম্পূর্ণ ধারা দেখান।", "এখানে কোনো ব্যতিক্রম আছে কি?"]

    return ["What are the details?", "Show the full related clause.", "Are there any exceptions?"]


def build_policy_answer_from_context(
    question: str,
    context: str,
    citations: List[Dict[str, Any]],
    route: Dict[str, str] | None = None,
    force_full: bool = False,
) -> str:
    clause_ref = get_clause_reference(citations, context)
    answer_type = classify_policy_question_type(question)
    bn = is_bn(question) or is_bn(context)

    # Broad topic or full policy request: return the relevant policy section.
    if force_full or answer_type in {"broad_topic", "full_topic"}:
        full_context = _clean_answer_context(context)
        if not full_context:
            return fallback_answer(question, context, citations)

        if bn:
            return (
                "Decision: নীতিমালার প্রাসঙ্গিক অংশ\n\n"
                f"Answer:\n{full_context}\n\n"
                f"Policy reference:\n{clause_ref}\n\n"
                "Follow-up questions:\n"
                "- এই বিষয়ে নির্দিষ্ট কোন তথ্য জানতে চান?\n"
                "- এই নীতির মূল শর্তগুলো কী?\n"
                "- সংশ্লিষ্ট ধারা থেকে সংক্ষিপ্ত সারাংশ দিন।"
            )

        return (
            "Decision: Relevant policy section\n\n"
            f"Answer:\n{full_context}\n\n"
            f"Policy reference:\n{clause_ref}\n\n"
            "Follow-up questions:\n"
            "- Which specific detail do you want?\n"
            "- What are the main conditions?\n"
            "- Summarize this clause."
        )

    ranked = rank_policy_units(question, context, answer_type)

    if ranked:
        best_score = ranked[0][0]
        selected: List[str] = []

        for score, unit in ranked:
            if len(selected) >= 5:
                break
            if score >= max(12, int(best_score * 0.70)):
                selected.append(unit)

        if not selected:
            selected = [ranked[0][1]]

        # For exact fact questions, keep the answer as focused as possible.
        if answer_type in {"qualification", "duration", "amount", "exception", "eligibility", "exact_fact"}:
            selected = selected[:1]
        elif answer_type == "approval_authority":
            # Specific office/grade authority questions may need one or two lines only.
            selected = selected[:2]

        answer_lines = "\n".join(f"- {line}" for line in selected)
        followups = _related_followups_for_type(answer_type, question)

        if bn:
            return (
                "Decision: নীতিমালার নির্দিষ্ট উত্তর\n\n"
                f"Answer:\n{answer_lines}\n\n"
                f"Policy reference:\n{clause_ref}\n\n"
                "Follow-up questions:\n"
                f"- {followups[0]}\n"
                f"- {followups[1]}\n"
                f"- {followups[2]}"
            )

        return (
            "Decision: Specific policy answer\n\n"
            f"Answer:\n{answer_lines}\n\n"
            f"Policy reference:\n{clause_ref}\n\n"
            "Follow-up questions:\n"
            f"- {followups[0]}\n"
            f"- {followups[1]}\n"
            f"- {followups[2]}"
        )

    # No matching sentence found.
    if bn:
        return (
            "Decision: স্পষ্টভাবে পাওয়া যায়নি\n\n"
            "Answer:\n"
            "প্রাপ্ত নীতিমালার কনটেক্সটে এই নির্দিষ্ট তথ্যটি স্পষ্টভাবে পাওয়া যায়নি।\n\n"
            f"Policy reference:\n{clause_ref}\n\n"
            "Follow-up questions:\n"
            "- সংশ্লিষ্ট ধারা নম্বর দিয়ে আবার চেষ্টা করুন।\n"
            "- সম্পূর্ণ ধারা দেখতে চান?\n"
            "- একই বিষয়ে অন্য প্রশ্ন করতে চান?"
        )

    return (
        "Decision: Not clearly found\n\n"
        "Answer:\n"
        "The requested detail was not clearly found in the provided policy context.\n\n"
        f"Policy reference:\n{clause_ref}\n\n"
        "Follow-up questions:\n"
        "- Try with the specific clause number.\n"
        "- Do you want the full clause?\n"
        "- Do you want to ask another question on the same topic?"
    )


def build_policy_overview_from_context(
    question: str,
    context: str,
    citations: List[Dict[str, Any]],
    max_units: int = 6,
) -> str:
    """
    Safe fallback for natural topic-intro questions such as:
      - tell about transfer
      - ell about transfer
      - বদলি সম্পর্কে বলুন

    This is used when the LLM answer is empty/bad. It must not run the
    exact-fact extractor, because exact-fact extraction can fail for broad
    explanation requests and return "স্পষ্টভাবে পাওয়া যায়নি" even though
    the correct topic context was retrieved.
    """
    clause_ref = get_clause_reference(citations, context)
    bn = is_bn(question) or is_bn(context)

    units = []
    for unit in split_policy_context_units(context):
        line = clean(unit)
        if not line:
            continue
        # Remove pure heading-only lines like "১.৮ বদলী" when more content exists.
        line_norm = normalize_clause_key(line)
        if re.fullmatch(r"\d+(?:\.\d+){0,3}\s+.{1,40}", line_norm) and len(line) < 80:
            continue
        if len(line) > 1200:
            # Keep this readable if one clause is very long.
            line = line[:1200].rstrip() + "..."
        units.append(line)
        if len(units) >= max_units:
            break

    # If all lines looked like headings, fall back to cleaned context instead of "not found".
    if not units:
        cleaned = _clean_answer_context(context)
        if cleaned:
            units = [cleaned[:1600].rstrip() + ("..." if len(cleaned) > 1600 else "")]

    if not units:
        return fallback_answer(question, context, citations)

    answer_lines = "\n".join(f"- {u}" for u in units)

    if bn:
        return (
            "Decision: নীতিমালার সংক্ষিপ্ত ব্যাখ্যা\n\n"
            f"Answer:\n{answer_lines}\n\n"
            f"Policy reference:\n{clause_ref}\n\n"
            "Follow-up questions:\n"
            "- এই নীতির মূল শর্তগুলো কী?\n"
            "- সংশ্লিষ্ট সম্পূর্ণ ধারা দেখান।\n"
            "- এখানে কোনো ব্যতিক্রম আছে কি?"
        )

    return (
        "Decision: Policy overview\n\n"
        f"Answer:\n{answer_lines}\n\n"
        f"Policy reference:\n{clause_ref}\n\n"
        "Follow-up questions:\n"
        "- What are the main conditions?\n"
        "- Show the full related clause.\n"
        "- Are there any exceptions?"
    )


def should_use_deterministic_answer(question: str) -> bool:
    answer_type = classify_policy_question_type(question)

    # Use deterministic answer for broad sections and precise fact extraction.
    # LLM is only optional for summary/explanation/comparison where language generation helps.
    return answer_type not in {"summary"}


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
                    f"Answer:\n{items_text}\n\n"
                    f"Policy reference:\n{clause_ref}\n\n"
                    "Follow-up questions:\n"
                    "- এই বিষয়ে বিস্তারিত কী?\n"
                    "- প্রতিটি পয়েন্টের ব্যাখ্যা কী?\n"
                    "- সংশ্লিষ্ট সম্পূর্ণ ধারা দেখান।"
                )

            return (
                "Decision: Policy list\n\n"
                f"Answer:\n{items_text}\n\n"
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
                f"Policy details:\n{context}\n\n"
                f"Policy reference:\n{clause_ref}\n\n"
                "Follow-up questions:\n"
                "- এই নীতির মূল শর্তগুলো কী?\n"
                "- সহজ করে ব্যাখ্যা করুন।\n"
                "- ۳টি পয়েন্টে বলুন।"
            )

        return (
            "Decision: Full policy\n\n"
            f"Policy details:\n{context}\n\n"
            f"Policy reference:\n{clause_ref}\n\n"
            "Follow-up questions:\n"
            "- What are the main conditions?\n"
            "- Explain simply.\n"
            "- Answer in 3 points."
        )

    if bn:
        return (
            "Decision: নীতিমালার প্রাসঙ্গিক অংশ\n\n"
            f"Answer:\n{context[:2500]}\n\n"
            f"Policy reference:\n{clause_ref}\n\n"
            "Follow-up questions:\n"
            "- এই বিষয়ে বিস্তারিত কী?\n"
            "- এই নীতির মূল শর্তগুলো কী?\n"
            "- এখানে কোনো ব্যতিক্রম আছে কি?"
        )

    return (
        "Decision: Relevant policy section\n\n"
        f"Answer:\n{context[:2500]}\n\n"
        f"Policy reference:\n{clause_ref}\n\n"
        "Follow-up questions:\n"
        "- What are the main conditions?\n"
        "- Are there any exceptions?\n"
        "- Do you want the full policy text?"
    )



# ============================================================
# Topic-aware RAG follow-up memory
# ============================================================

MEMORY_FOLLOWUP_PHRASES = GENERIC_FOLLOWUP_PHRASES + [
    # Generic follow-up words
    "আরও", "আরো", "বিস্তারিত", "ব্যাখ্যা", "সহজ করে", "আবার বলুন",
    "এইটা", "এটা", "সেটা", "ওটা", "উপরের", "আগের", "same", "previous", "above",
    "more", "details", "detail", "explain", "explain more", "what about",

    # Common question/intent words
    "কত দিন", "কতদিন", "কত", "কে", "কারা", "কখন", "কিভাবে", "কীভাবে",
    "কে অনুমোদন", "কার অনুমোদন", "অনুমোদন", "মঞ্জুর", "মঞ্জুর করবেন",
    "শর্ত", "মূল শর্ত", "ব্যতিক্রম", "প্রকারভেদ", "ধরন", "ধরণ",
    "কি কি", "কী কী", "সংশ্লিষ্ট", "যোগ্যতা", "শিক্ষাগত", "ন্যূনতম",
    "গ্রেড", "পদের", "পদ", "শিথিল", "গাড়ী", "গাড়ী", "চালক",
    "বেতন", "ভাতা", "পরিমাণ", "টাকা", "মেয়াদ", "মেয়াদ", "সময়", "সময়",
    "প্রক্রিয়া", "প্রক্রিয়া", "ধাপ", "দায়িত্ব", "কর্তব্য", "সুবিধা",
    "শাস্তি", "আপিল", "প্রভিডেন্ট", "গ্র্যাচুইটি",

    # English
    "how many", "how much", "who approve", "who approves", "approval", "authority",
    "condition", "conditions", "exception", "eligibility", "qualification", "minimum",
    "grade", "duration", "amount", "salary", "allowance", "process", "responsibility",
    "benefit", "penalty", "appeal", "document", "documents",
]

POLICY_MEMORY_STOPWORDS = {
    "the", "is", "are", "a", "an", "to", "of", "for", "in", "on", "and",
    "or", "with", "from", "by", "about", "policy", "rule", "rules", "show",
    "tell", "me", "please", "what", "how", "why", "when", "where", "who",
    "which", "this", "that", "it", "same", "previous", "above", "more",
    "details", "explain", "কি", "কী", "কেন", "কখন", "কোথায়", "কোথায়",
    "কিভাবে", "কীভাবে", "বলুন", "বলেন", "আমাকে", "এই", "ওই", "এটা",
    "সেটা", "ওটা", "আরও", "আরো", "বিস্তারিত", "নীতি", "নীতিমালা",
    "নিয়ম", "নিয়ম", "ধারা", "শর্ত", "গুলো", "গুলি",
}


def _truncate(value: Any, max_chars: int) -> str:
    text = clean(value)

    if max_chars and len(text) > max_chars:
        return text[:max_chars] + "..."

    return text


def _safe_list(value: Any, limit: int = 20) -> List[Any]:
    if not value:
        return []

    if isinstance(value, list):
        return value[:limit]

    return [value]


def _policy_memory_keywords(text: str, limit: int = 50) -> List[str]:
    normalized = normalize_clause_key(text).lower()
    tokens = re.findall(r"[a-zA-Z0-9_]+|[\u0980-\u09FF]+", normalized)
    out: List[str] = []

    for token in tokens:
        token = token.strip().lower()

        if len(token) < 2:
            continue

        if token in POLICY_MEMORY_STOPWORDS:
            continue

        if token not in out:
            out.append(token)

        if len(out) >= limit:
            break

    return out


def is_policy_memory_followup_question(question: str) -> bool:
    q = clean(question).lower()

    if not q:
        return False

    if is_generic_followup_question(q):
        return True

    word_count = len(q.split())

    if word_count <= 10:
        for phrase in MEMORY_FOLLOWUP_PHRASES:
            if clean(phrase).lower() in q:
                return True

    followup_patterns = [
        r"^how many␈", r"^how much␈", r"^who␈", r"^when␈",
        r"^what about␈", r"^explain␈", r"^details␈", r"^more␈",
        r"কত", r"কত দিন", r"কতদিন", r"কে", r"কারা", r"কখন",
        r"অনুমোদন", r"মঞ্জুর", r"বিস্তারিত", r"আরও", r"আরো", r"শর্ত",
        r"যোগ্যতা", r"শিক্ষাগত", r"ন্যূনতম", r"গ্রেড", r"শিথিল",
        r"বেতন", r"ভাতা", r"মেয়াদ", r"মেয়াদ", r"প্রক্রিয়া", r"প্রক্রিয়া",
    ]

    return any(re.search(pattern, q) for pattern in followup_patterns)


def get_policy_request_mode(data: Dict[str, Any]) -> str:
    """
    Read optional frontend/API mode.

    Supported:
      - primary/new/new_search/search/reset/fresh: ignore old memory and retrieve again
      - followup/follow_up/same_topic/memory/memory_only: use saved context only
      - auto/empty: safe automatic detection
    """
    raw = clean(
        data.get("mode")
        or data.get("chat_mode")
        or data.get("followup_mode")
        or data.get("source_mode")
        or ""
    ).lower()

    if raw in POLICY_MODE_PRIMARY_VALUES:
        return "primary"

    if raw in POLICY_MODE_FOLLOWUP_VALUES:
        return "followup"

    return "auto"


def is_probably_new_policy_topic_question(question: str, memory: Dict[str, Any]) -> bool:
    """
    Prevent a new short topic from being hijacked by old follow-up memory.

    Example fixed case:
      Previous memory = leave policy
      New user question = "salary policy" / "বেতন"

    Old behavior could treat salary as follow-up because salary is an intent word.
    This function says: if current route/topic clearly differs from saved route/topic,
    treat it as a fresh policy question.
    """
    if not memory:
        return False

    # Very generic follow-up phrases should remain follow-up, not new topic.
    if is_generic_followup_question(question):
        return False

    question_norm = normalize_policy_question(question)
    q_route = detect_policy_route(question_norm)
    m_route = memory.get("route") or {}
    if not isinstance(m_route, dict):
        m_route = {}

    q_cat = clean(q_route.get("category"))
    q_sub = clean(q_route.get("subtopic"))
    m_cat = clean(m_route.get("category"))
    m_sub = clean(m_route.get("subtopic"))

    exact_topic = detect_exact_topic_request(question)
    if exact_topic:
        active_topic = _memory_active_topic(memory) if "_memory_active_topic" in globals() else {}
        q_clause = normalize_clause_key(exact_topic.get("clause"))
        q_title = clean(exact_topic.get("title_bn"))
        m_clause = normalize_clause_key(
            active_topic.get("clause_range")
            or active_topic.get("clause")
            or active_topic.get("policy_reference")
            or ""
        )
        m_title = clean(active_topic.get("topic_bn") or active_topic.get("title_bn") or "")

        if q_clause and m_clause and (m_clause.startswith(q_clause) or q_clause.startswith(m_clause)):
            return False
        if q_title and m_title and q_title == m_title:
            return False

        return True

    # Route clearly changed: fresh question.
    if (q_cat or q_sub) and (m_cat or m_sub):
        if q_cat and m_cat and q_cat == m_cat:
            return False
        if q_sub and m_sub and q_sub == m_sub:
            return False
        return True

    # If the current question has a known route but memory does not, a short topic-like
    # query is safer as new retrieval than as memory-only answer.
    if (q_cat or q_sub) and not (m_cat or m_sub):
        token_count = len(_policy_memory_keywords(question, limit=20))
        if token_count <= 6:
            return True

    return False


def should_use_saved_policy_memory(
    question: str,
    memory: Dict[str, Any],
    force_followup: bool = False,
) -> bool:
    """
    Decide whether current question should use saved RAG context only.

    Design:
      - explicit followup button/mode => use memory if memory exists
      - auto mode => use memory only when question is related to current topic
      - clear new topic => do not use memory
    """
    if not memory:
        return False

    if force_followup:
        return True

    if is_probably_new_policy_topic_question(question, memory):
        return False

    return is_question_related_to_policy_memory(question, memory)


def should_block_unrelated_explicit_followup(question: str, memory: Dict[str, Any]) -> bool:
    """
    Only block when the frontend explicitly requests follow-up but the question
    appears to be a new/different policy topic.
    In auto mode, different topics should simply run fresh retrieval.
    """
    if not memory:
        return False

    return is_probably_new_policy_topic_question(question, memory)


def get_policy_rag_memory(request: HttpRequest | None) -> Dict[str, Any]:
    if request is None:
        return {}

    try:
        memory = request.session.get(POLICY_RAG_MEMORY_KEY, {})

        if isinstance(memory, dict):
            return memory

    except Exception as e:
        logger.warning("Failed to read policy RAG memory from session: %s", e)

    return {}


def clear_policy_rag_memory(request: HttpRequest | None) -> None:
    if request is None:
        return

    try:
        if POLICY_RAG_MEMORY_KEY in request.session:
            del request.session[POLICY_RAG_MEMORY_KEY]
            request.session.modified = True

    except Exception as e:
        logger.warning("Failed to clear policy RAG memory: %s", e)


def _memory_active_topic(memory: Dict[str, Any]) -> Dict[str, Any]:
    active_topic = memory.get("active_topic") or memory.get("detected_topic") or {}

    return active_topic if isinstance(active_topic, dict) else {}


def _memory_topic_prefix(memory: Dict[str, Any]) -> str:
    active_topic = _memory_active_topic(memory)
    prefix = build_active_topic_prefix(active_topic)

    if prefix:
        return prefix

    route = memory.get("route") or {}

    if isinstance(route, dict):
        category = clean(route.get("category"))
        subtopic = clean(route.get("subtopic"))

        if category and subtopic:
            return f"{category} {subtopic}"

        if category:
            return category

    return clean(memory.get("last_question") or memory.get("question_for_route"))


def is_question_related_to_policy_memory(question: str, memory: Dict[str, Any]) -> bool:
    if not memory:
        return False

    # Do not let a clear new topic be answered from old saved context.
    # Example: previous topic = leave, current question = salary policy.
    if is_probably_new_policy_topic_question(question, memory):
        return False

    # Generic follow-up phrases like "এই বিষয়ে বিস্তারিত" or "explain more"
    # are safe to resolve against current memory.
    if is_generic_followup_question(question):
        return True

    question_route = detect_policy_route(normalize_policy_question(question))
    memory_route = memory.get("route") or {}

    if isinstance(memory_route, dict):
        q_category = clean(question_route.get("category"))
        q_subtopic = clean(question_route.get("subtopic"))
        m_category = clean(memory_route.get("category"))
        m_subtopic = clean(memory_route.get("subtopic"))

        if q_category and m_category and q_category == m_category:
            return True

        if q_subtopic and m_subtopic and q_subtopic == m_subtopic:
            return True

    active_topic = _memory_active_topic(memory)
    active_blob = " ".join(
        clean(active_topic.get(key))
        for key in [
            "topic_id", "topic_bn", "topic_en", "clause_range", "category",
            "subtopic", "employee_type", "source_file",
        ]
        if active_topic.get(key)
    )

    memory_blob = " ".join(
        [
            active_blob,
            clean(memory.get("last_question")),
            clean(memory.get("question_for_route")),
            clean(memory.get("context_question")),
            clean(memory.get("answer")),
            clean(memory.get("raw_context"))[:3000],
        ]
    )

    q_words = set(_policy_memory_keywords(question, limit=30))
    m_words = set(_policy_memory_keywords(memory_blob, limit=120))
    overlap = q_words.intersection(m_words)

    # Strong enough for policy follow-up: two or more matched content words.
    # Also allow one distinctive long Bengali/English token, e.g. বিলম্বে, উপস্থিত, শাখা, গ্রেড-১৬.
    if len(overlap) >= 2:
        return True

    if len(overlap) == 1:
        only = next(iter(overlap))
        if len(only) >= 5 or re.search(r"\d", only):
            return True

    return False


def build_policy_memory_contextual_question(question: str, memory: Dict[str, Any]) -> str:
    topic_prefix = _memory_topic_prefix(memory)
    previous_question = clean(memory.get("last_question") or memory.get("question"))
    previous_context_question = clean(memory.get("context_question"))

    parts = []

    if topic_prefix:
        parts.append(f"Active previous policy topic: {topic_prefix}")

    if previous_context_question:
        parts.append(f"Previous context question: {previous_context_question}")
    elif previous_question:
        parts.append(f"Previous question: {previous_question}")

    parts.append(f"Current follow-up question: {question}")

    return "\n".join(parts)


def build_policy_memory_prompt(memory: Dict[str, Any], current_question: str) -> str:
    if not memory:
        return ""

    topic_prefix = _memory_topic_prefix(memory)
    previous_question = clean(memory.get("last_question") or memory.get("question"))
    previous_answer = _truncate(memory.get("answer"), POLICY_RAG_MEMORY_ANSWER_MAX_CHARS)
    previous_context = _truncate(memory.get("raw_context"), POLICY_RAG_MEMORY_CONTEXT_MAX_CHARS)
    previous_route = memory.get("route") or {}
    previous_ref = get_clause_reference(_safe_list(memory.get("citations")), previous_context)

    return f"""
Active previous topic:
{topic_prefix or "Not found"}

Previous question:
{previous_question or "Not found"}

Previous retrieval route:
{json.dumps(previous_route, ensure_ascii=False)}

Previous policy reference:
{previous_ref}

Previous retrieved policy text:
{previous_context or "Not found"}

Previous answer summary:
{previous_answer or "Not found"}

Current follow-up question:
{current_question}
""".strip()


def combine_current_and_memory_context(
    current_context: str,
    memory: Dict[str, Any],
    use_memory: bool,
) -> str:
    current_context = clean(current_context)

    if not use_memory or not memory:
        return current_context

    previous_context = clean(memory.get("raw_context"))

    if not previous_context:
        return current_context

    if current_context and previous_context[:500] in current_context:
        return current_context

    if current_context:
        combined = (
            f"{current_context}\n\n---\n\n"
            "[Previous RAG Memory - same active policy topic]\n"
            f"{previous_context}"
        )
    else:
        combined = (
            "[Previous RAG Memory - same active policy topic]\n"
            f"{previous_context}"
        )

    return combined[:POLICY_CONTEXT_MAX_CHARS]


def merge_current_and_memory_citations(
    citations: List[Dict[str, Any]],
    memory: Dict[str, Any],
    use_memory: bool,
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen = set()

    def add_items(items: Any) -> None:
        for item in _safe_list(items, limit=20):
            if not isinstance(item, dict):
                continue

            key = (
                clean(item.get("topic_id")),
                clean(item.get("topic_bn")),
                clean(item.get("topic_en")),
                clean(item.get("clause_range")),
                clean(item.get("source_file")),
            )

            if key in seen:
                continue

            seen.add(key)
            merged.append(item)

    add_items(citations)

    if use_memory and memory:
        add_items(memory.get("citations"))

    for idx, item in enumerate(merged, start=1):
        item["idx"] = idx

    return merged[:20]


def build_not_related_to_memory_payload(
    question: str,
    collection_name: str,
    memory: Dict[str, Any],
) -> Dict[str, Any]:
    active_topic = _memory_active_topic(memory)
    topic_label = _memory_topic_prefix(memory) or "current policy topic"

    if is_bn(question):
        answer = (
            "Decision: বর্তমান টপিকের সাথে সম্পর্কিত নয়\n\n"
            "Answer:\n"
            f"আপনার প্রশ্নটি `{topic_label}` আলোচনার সাথে সরাসরি সম্পর্কিত নয়। "
            "নতুন নীতিমালা বিষয়ে জানতে চাইলে পূর্ণ প্রশ্ন করুন, অথবা বর্তমান টপিক সম্পর্কে follow-up করুন।\n\n"
            "Policy reference:\n"
            "Not applicable\n\n"
            "Follow-up questions:\n"
            f"- {topic_label} বিস্তারিত দেখান\n"
            f"- {topic_label} নীতির মূল শর্ত কী?\n"
            "- টপিক তালিকা দেখান"
        )
    else:
        answer = (
            "Decision: Not related to current topic\n\n"
            "Answer:\n"
            f"Your question is not directly related to `{topic_label}`. "
            "Ask a new full policy question, or ask a follow-up about the current topic.\n\n"
            "Policy reference:\n"
            "Not applicable\n\n"
            "Follow-up questions:\n"
            f"- Show details of {topic_label}\n"
            f"- What are the main conditions of {topic_label}?\n"
            "- Show topics list"
        )

    route = {
        "category": "FOLLOWUP",
        "subtopic": "NOT_RELATED_TO_ACTIVE_TOPIC",
        "employee_type": POLICY_DEFAULT_EMPLOYEE_TYPE,
        "expanded_query": question,
    }

    quick_actions = build_contextual_quick_actions(active_topic)

    payload = {
        "ok": True,
        "question": question,
        "question_for_route": question,
        "context_question": question,
        "answer": answer,
        "raw_context": "",
        "route": route,
        "answer_mode": "not_related_to_memory",
        "validation": {"valid": False, "reason": "not_related_to_active_topic"},
        "detected_topic": active_topic,
        "active_topic": active_topic,
        "citations": [],
        "suggestions": quick_actions,
        "related_questions": quick_actions,
        "must_followup_questions": [],
        "clarification_questions": [],
        "quick_actions": quick_actions,
        "topic_buttons": [],
        "needs_clarification": False,
        "interactive": {
            "interactive_enabled": True,
            "active_topic": active_topic,
            "must_followup_questions": [],
            "clarification_questions": [],
            "quick_actions": quick_actions,
            "topic_buttons": [],
            "needs_clarification": False,
            "default_employee_type": POLICY_DEFAULT_EMPLOYEE_TYPE,
            "route": route,
            "answer_mode": "not_related_to_memory",
        },
        "topic_groups": [],
        "topic_list_mode": False,
        "topics_count": 0,
        "followup_used_memory": True,
        "memory_relation": "not_related",
    }

    if POLICY_STREAM_DEBUG:
        payload["debug"] = {
            "collection": collection_name,
            "code_version": POLICY_CODE_VERSION,
            "memory_topic": topic_label,
        }

    return payload


def save_response_policy_rag_memory(request: HttpRequest | None, payload: Dict[str, Any]) -> None:
    if request is None:
        return

    try:
        answer_mode = clean(payload.get("answer_mode"))

        if answer_mode in {"topic_list", "context_missing", "not_related_to_memory", "error"}:
            return

        raw_context = clean(payload.get("raw_context"))
        active_topic = payload.get("active_topic") or payload.get("detected_topic") or {}

        if not raw_context and not active_topic:
            return

        old_memory = get_policy_rag_memory(request)
        old_history = old_memory.get("history", [])

        if not isinstance(old_history, list):
            old_history = []

        history_item = {
            "question": clean(payload.get("question")),
            "answer": _truncate(payload.get("answer"), 1200),
            "active_topic": active_topic if isinstance(active_topic, dict) else {},
            "saved_at": int(__import__("time").time()),
        }

        history = (old_history + [history_item])[-POLICY_RAG_MEMORY_HISTORY_LIMIT:]

        memory = {
            "version": 1,
            "domain": "policy",
            "collection": clean((payload.get("debug") or {}).get("collection")),
            "last_question": clean(payload.get("question")),
            "question_for_route": clean(payload.get("question_for_route")),
            "context_question": clean(payload.get("context_question")),
            "answer": _truncate(payload.get("answer"), POLICY_RAG_MEMORY_ANSWER_MAX_CHARS),
            "raw_context": _truncate(raw_context, POLICY_RAG_MEMORY_CONTEXT_MAX_CHARS),
            "route": payload.get("route") if isinstance(payload.get("route"), dict) else {},
            "active_topic": active_topic if isinstance(active_topic, dict) else {},
            "detected_topic": payload.get("detected_topic") if isinstance(payload.get("detected_topic"), dict) else {},
            "citations": _safe_list(payload.get("citations"), limit=20),
            "quick_actions": _safe_list(payload.get("quick_actions"), limit=10),
            "history": history,
            "saved_at": int(__import__("time").time()),
        }

        request.session[POLICY_RAG_MEMORY_KEY] = memory
        request.session.modified = True

    except Exception as e:
        logger.warning("Failed to save policy RAG memory in session: %s", e)

# ============================================================
# Main payload builder
# ============================================================

def build_success_payload(
    question: str,
    collection: str | None = None,
    active_topic_from_request: Dict[str, Any] | None = None,
    request: HttpRequest | None = None,
    mode: str = "auto",
) -> Dict[str, Any]:
    collection_name = clean(collection) or get_policy_collection()
    active_topic_from_request = (
        active_topic_from_request if isinstance(active_topic_from_request, dict) else {}
    )

    mode = clean(mode).lower() or "auto"
    force_followup = mode == "followup"
    force_primary = mode == "primary"

    policy_memory = {} if force_primary else get_policy_rag_memory(request)
    memory_relation = "new_topic"
    followup_used_memory = False
    memory_prompt = ""
    used_memory_only_context = False
    vector_search_skipped_for_followup = False

    if policy_memory and not active_topic_from_request:
        memory_active_topic = _memory_active_topic(policy_memory)

        if memory_active_topic:
            active_topic_from_request = memory_active_topic

    # SAFE ROUTING RULE:
    # - Explicit mode=followup: answer from saved context only.
    # - Explicit mode=primary/new_search: ignore saved context and retrieve again.
    # - Auto mode: use saved memory only if the question is related to the active topic.
    #
    # This prevents wrong behavior like:
    #   Previous topic: leave policy
    #   Current question: salary policy
    # being answered from the old leave context.
    if policy_memory and force_followup and should_block_unrelated_explicit_followup(question, policy_memory):
        return build_not_related_to_memory_payload(
            question=question,
            collection_name=collection_name,
            memory=policy_memory,
        )

    if policy_memory and should_use_saved_policy_memory(
        question=question,
        memory=policy_memory,
        force_followup=force_followup,
    ):
        memory_relation = "explicit_followup_memory_only" if force_followup else "auto_followup_memory_only"
        followup_used_memory = True
        used_memory_only_context = True
        vector_search_skipped_for_followup = True
        question_for_context = build_policy_memory_contextual_question(
            question=question,
            memory=policy_memory,
        )
        memory_prompt = build_policy_memory_prompt(policy_memory, question)
    else:
        if is_generic_followup_question(question) and not active_topic_from_request and not policy_memory:
            return build_missing_context_payload(question, collection_name)

        question_for_context = resolve_contextual_followup_question(
            question=question,
            active_topic_from_request=active_topic_from_request,
        )

    question_norm = normalize_policy_question(question_for_context)

    # Use normalized/canonical question for routing/retrieval, but preserve the user's
    # original intent for answer generation. Otherwise "Tell about transfer" becomes
    # "۱.۸ বদলি নীতিমালা দেখান", then the deterministic extractor may return only
    # the heading instead of explaining the policy.
    if followup_used_memory:
        answer_question = question
    elif clean(question_for_context) != clean(question):
        answer_question = question_for_context
    else:
        answer_question = question

    if is_policy_topic_list_request(question_norm):
        return build_policy_topics_payload(
            question=question,
            question_norm=question_norm,
            collection_name=collection_name,
        )

    if followup_used_memory:
        # Critical rule: follow-up questions must use saved RAG context only.
        # No Qdrant/vector retrieval is performed here.
        context = clean(policy_memory.get("raw_context"))
        citations = _safe_list(policy_memory.get("citations"), limit=20)
        previous_route = policy_memory.get("route") or {}
        route = previous_route if isinstance(previous_route, dict) else {}
        active_topic = _memory_active_topic(policy_memory)

        if not context:
            context = clean(policy_memory.get("answer"))
    else:
        context, citations, route, active_topic = retrieve_policy_context(
            question=question_norm,
            collection=collection_name,
        )

    quick_actions = build_contextual_quick_actions(active_topic)

    if route.get("category") == "EXACT_TOPIC" and route.get("subtopic") == "NOT_FOUND":
        answer = context
    elif not context:
        answer = fallback_answer(answer_question, context, citations)
    else:
        if should_use_deterministic_answer(answer_question):
            answer = build_policy_answer_from_context(
                question=answer_question,
                context=context,
                citations=citations,
                route=route,
            )
        else:
            prompt = build_policy_prompt(
                question=answer_question,
                context=context,
                citations=citations,
                route=route,
                memory_context=memory_prompt,
            )

            try:
                answer = clean_model_answer(call_ollama_generate(prompt))

                if (
                    not answer
                    or is_bad_model_answer(answer)
                    or is_model_answer_too_broad(answer_question, answer, context)
                ):
                    if classify_policy_question_type(answer_question) == "summary":
                        answer = build_policy_overview_from_context(
                            question=answer_question,
                            context=context,
                            citations=citations,
                        )
                    else:
                        answer = build_policy_answer_from_context(
                            question=answer_question,
                            context=context,
                            citations=citations,
                            route=route,
                        )

            except Exception as e:
                logger.exception("Ollama policy answer generation failed: %s", e)
                if classify_policy_question_type(answer_question) == "summary":
                    answer = build_policy_overview_from_context(
                        question=answer_question,
                        context=context,
                        citations=citations,
                    )
                else:
                    answer = build_policy_answer_from_context(
                        question=answer_question,
                        context=context,
                        citations=citations,
                        route=route,
                    )

    payload = {
        "ok": True,
        "question": question,
        "question_for_route": question_norm,
        "context_question": question_for_context,
        "answer": answer,
        "raw_context": context,
        "route": route,
        "answer_mode": "memory_only_deterministic" if followup_used_memory else "deterministic_grounded",
        "validation": {
            "valid": bool(context),
            "reason": "answer_from_saved_memory_only"
            if followup_used_memory
            else "answer_from_retrieved_context",
        },
        "detected_topic": active_topic,
        "active_topic": active_topic,
        "citations": citations,
        "suggestions": quick_actions,
        "related_questions": quick_actions,
        "must_followup_questions": [],
        "clarification_questions": [],
        "quick_actions": quick_actions,
        "topic_buttons": [],
        "needs_clarification": False,
        "interactive": {
            "interactive_enabled": True,
            "active_topic": active_topic,
            "must_followup_questions": [],
            "clarification_questions": [],
            "quick_actions": quick_actions,
            "topic_buttons": [],
            "needs_clarification": False,
            "default_employee_type": POLICY_DEFAULT_EMPLOYEE_TYPE,
            "route": route,
            "answer_mode": "memory_only_deterministic" if followup_used_memory else "deterministic_grounded",
        },
        "topic_groups": [],
        "topic_list_mode": False,
        "topics_count": 0,
        "followup_used_memory": followup_used_memory,
        "memory_relation": memory_relation,
        "memory_active_topic": _memory_active_topic(policy_memory) if policy_memory else {},
    }

    if POLICY_STREAM_DEBUG:
        payload["debug"] = {
            "collection": collection_name,
            "qdrant_url": get_qdrant_url(check_health=False),
            "ollama_url": get_ollama_url(),
            "embed_model": get_embed_model(),
            "answer_model": get_policy_answer_model(),
            "vector_name": POLICY_VECTOR_NAME,
            "route": route,
            "citations_count": len(citations),
            "context_chars": len(context or ""),
            "context_question": question_for_context,
            "answer_question": answer_question,
            "answer_type": classify_policy_question_type(answer_question),
            "request_mode": mode,
            "force_followup": force_followup,
            "force_primary": force_primary,
            "followup_used_memory": followup_used_memory,
            "memory_relation": memory_relation,
            "used_memory_only_context": used_memory_only_context,
            "vector_search_skipped_for_followup": vector_search_skipped_for_followup,
            "memory_key": POLICY_RAG_MEMORY_KEY,
            "code_version": POLICY_CODE_VERSION,
        }

    return payload



# ============================================================
# Session-based active topic memory
# ============================================================

def get_request_active_topic(request: HttpRequest, data: Dict[str, Any]) -> Dict[str, Any]:
    active_topic = data.get("active_topic") or data.get("activeTopic") or {}

    if isinstance(active_topic, dict) and active_topic:
        return active_topic

    try:
        session_topic = request.session.get(POLICY_SESSION_ACTIVE_TOPIC_KEY, {})

        if isinstance(session_topic, dict):
            return session_topic

    except Exception:
        pass

    return {}


def save_response_active_topic(request: HttpRequest, payload: Dict[str, Any]) -> None:
    try:
        active_topic = (
            payload.get("active_topic")
            or payload.get("detected_topic")
            or (payload.get("interactive") or {}).get("active_topic")
            or {}
        )

        if isinstance(active_topic, dict) and active_topic:
            request.session[POLICY_SESSION_ACTIVE_TOPIC_KEY] = active_topic
            request.session.modified = True

    except Exception as e:
        logger.warning("Failed to save policy active topic in session: %s", e)


# ============================================================
# HTTP endpoints
# ============================================================

@csrf_exempt
@require_POST
def policy_ask(request: HttpRequest):
    data = parse_json_body(request)
    question = extract_question(data)
    collection = clean(data.get("collection")) or get_policy_collection()
    request_mode = get_policy_request_mode(data)

    if request_mode == "primary":
        clear_policy_rag_memory(request)

    active_topic_from_request = get_request_active_topic(request, data)

    if not question:
        return HttpResponseBadRequest("Missing question")

    try:
        payload = build_success_payload(
            question,
            collection=collection,
            active_topic_from_request=active_topic_from_request,
            request=request,
            mode=request_mode,
        )
        save_response_active_topic(request, payload)
        save_response_policy_rag_memory(request, payload)

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
                "validation": {"valid": False, "reason": str(e)},
                "citations": [],
                "suggestions": [],
                "related_questions": [],
                "must_followup_questions": [],
                "clarification_questions": [],
                "quick_actions": [],
                "topic_buttons": [],
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
    request_mode = get_policy_request_mode(data)

    if request_mode == "primary":
        clear_policy_rag_memory(request)

    active_topic_from_request = get_request_active_topic(request, data)

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

            payload = build_success_payload(
                question,
                collection=collection,
                active_topic_from_request=active_topic_from_request,
                request=request,
            )
            save_response_active_topic(request, payload)
            save_response_policy_rag_memory(request, payload)

            answer_payload = {
                "ok": True,
                "event": "answer",
                "answer": payload.get("answer", ""),
                "raw_context": payload.get("raw_context", ""),
                "question_for_route": payload.get("question_for_route", ""),
                "context_question": payload.get("context_question", ""),
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
                "topic_buttons": payload.get("topic_buttons") or [],
                "needs_clarification": bool(payload.get("needs_clarification", False)),
                "interactive": payload.get("interactive") or {},
                "topic_groups": payload.get("topic_groups") or [],
                "topic_list_mode": bool(payload.get("topic_list_mode", False)),
                "topics_count": int(payload.get("topics_count") or 0),
                "followup_used_memory": bool(payload.get("followup_used_memory", False)),
                "memory_relation": payload.get("memory_relation") or "",
                "memory_active_topic": payload.get("memory_active_topic") or {},
            }

            if POLICY_STREAM_DEBUG:
                answer_payload["debug"] = payload.get("debug") or {}

            yield f"data: {json.dumps(answer_payload, ensure_ascii=False)}\n\n"

            end_payload = {"ok": True, "event": "end"}
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
                "validation": {"valid": False, "reason": str(e)},
                "citations": [],
                "suggestions": [],
                "related_questions": [],
                "must_followup_questions": [],
                "clarification_questions": [],
                "quick_actions": [],
                "topic_buttons": [],
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
