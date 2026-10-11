from __future__ import annotations

import os
import re
import logging
from typing import Any, Dict, List, Optional, Set

import requests
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


# ============================================================
# 1. Capability question detector
# ============================================================

CAPABILITY_KEYWORDS = [
    # English
    "what is your capability",
    "what are your capabilities",
    "what can you do",
    "what type problem",
    "what type of problem",
    "what problem can you solve",
    "which problem can you solve",
    "what kind of problem",
    "which kind of problem",
    "list your capability",
    "show your capability",
    "your capability",
    "your capabilities",
    "capability",
    "capabilities",
    "available topics",
    "show topics",
    "topic list",
    "what knowledge do you have",

    # Bangla
    "তুমি কি করতে পার",
    "আপনি কি করতে পারেন",
    "কি করতে পারো",
    "কি কি করতে পারো",
    "কি কি করতে পারেন",
    "কী করতে পার",
    "কী কী করতে পার",
    "সক্ষমতা",
    "তোমার সক্ষমতা",
    "আপনার সক্ষমতা",
    "কোন কোন সমস্যা",
    "কি ধরনের সমস্যা",
    "কী ধরনের সমস্যা",
    "ক্যাপাবিলিটি",
    "টপিক",
    "টপিক লিস্ট",
    "বিষয় তালিকা",
]


def normalize_text(text: str) -> str:
    text = text or ""
    text = str(text).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_key(text: str) -> str:
    text = normalize_text(text)
    text = text.replace("-", "_").replace(" ", "_")
    text = re.sub(r"[^a-z0-9_\u0980-\u09FF]+", "", text)
    return text


def is_dynamic_capability_question(text: str) -> bool:
    q = normalize_text(text)
    return any(k.lower() in q for k in CAPABILITY_KEYWORDS)


# ============================================================
# 2. Qdrant config
# ============================================================

def get_qdrant_url() -> str:
    return (
        getattr(settings, "QDRANT_URL", None)
        or getattr(settings, "HR_NOTICE_QDRANT_URL", None)
        or os.getenv("QDRANT_URL")
        or os.getenv("HR_NOTICE_QDRANT_URL")
        or "http://192.168.1.176:6333"
    ).rstrip("/")


def qdrant_get_collections() -> List[str]:
    url = get_qdrant_url()
    res = requests.get(f"{url}/collections", timeout=8)
    res.raise_for_status()

    data = res.json()
    collections = data.get("result", {}).get("collections", [])

    return sorted([c.get("name") for c in collections if c.get("name")])


def qdrant_scroll_sample(collection_name: str, limit: int = 40) -> List[Dict[str, Any]]:
    url = get_qdrant_url()

    body = {
        "limit": limit,
        "with_payload": True,
        "with_vector": False,
    }

    res = requests.post(
        f"{url}/collections/{collection_name}/points/scroll",
        json=body,
        timeout=12,
    )
    res.raise_for_status()

    data = res.json()
    points = data.get("result", {}).get("points", [])

    return points if isinstance(points, list) else []


# ============================================================
# 3. Generic helpers
# ============================================================

def short_value(value: Any, max_len: int = 160) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, (dict, list, tuple, set)):
        return None

    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)

    if not text:
        return None

    if len(text) > max_len:
        text = text[:max_len].rstrip() + "..."

    return text


def payload_list_value(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [normalize_key(str(x)) for x in value if str(x).strip()]

    if isinstance(value, tuple):
        return [normalize_key(str(x)) for x in value if str(x).strip()]

    if isinstance(value, set):
        return [normalize_key(str(x)) for x in value if str(x).strip()]

    if isinstance(value, str):
        # Supports:
        # "admin,staff,news"
        # "admin|staff|news"
        # "admin staff news"
        raw = re.split(r"[,|;]+", value)
        if len(raw) == 1:
            raw = value.split()
        return [normalize_key(x) for x in raw if x.strip()]

    return []


def get_payload_value(payload: Dict[str, Any], keys: List[str], max_len: int = 160) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        value = short_value(value, max_len=max_len)
        if value:
            return value
    return None


# ============================================================
# 4. Dynamic collection metadata from Qdrant payload
# ============================================================

def infer_module_from_text(text: str) -> str:
    """
    Fallback only.
    Main source should be Qdrant payload:
    module/domain/category/collection_label_bn/collection_label_en.
    New module like news/legal/procurement/training will work if payload has module.
    """
    t = normalize_text(text).replace("_", " ").replace("-", " ")

    keyword_map = [
        ("policy", ["policy", "নীতি", "service rule", "rules", "regulation"]),
        ("notice", ["notice", "circular", "noc", "memo", "office order", "বিজ্ঞপ্তি", "সার্কুলার"]),
        ("hr", ["hr", "employee", "staff", "human resource", "কর্মী", "কর্মচারী"]),
        ("core", ["core", "loan", "loanee", "member", "branch", "ঋণ", "সদস্য"]),
        ("cashflow", ["cashflow", "analytics", "dw", "collection", "financial", "trend"]),
        ("customer_support", ["support", "customer", "complaint", "ticket", "service request"]),
        ("website", ["website", "public", "web", "burobd", "program", "service"]),
        ("memory", ["memory", "history", "conversation"]),
    ]

    for module, keywords in keyword_map:
        if any(k in t for k in keywords):
            return module

    return "unknown"


def extract_meta_from_points(
    collection_name: str,
    points: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """
    Fully dynamic collection metadata.
    Priority:
    1. Qdrant payload metadata
    2. Collection name fallback

    Recommended Qdrant payload:
    {
      "module": "news",
      "domain": "News",
      "collection_label_bn": "সংবাদ ও আপডেট",
      "collection_label_en": "News and Updates",
      "visibility": "public",
      "allowed_roles": ["admin", "staff", "news"],
      "topic_bn": "সাম্প্রতিক সংবাদ",
      "topic_en": "Recent News",
      "title": "Latest organizational news"
    }
    """
    points = points or []

    module_keys = [
        "module",
        "domain",
        "collection_module",
        "collection_type",
        "knowledge_type",
        "source_type",
        "category",
        "intent",
    ]

    label_keys_bn = [
        "collection_label_bn",
        "label_bn",
        "module_bn",
        "domain_bn",
        "category_bn",
        "topic_bn",
        "title_bn",
        "bn_title",
        "notice_title",
        "title",
        "topic",
        "category",
        "domain",
        "module",
    ]

    label_keys_en = [
        "collection_label_en",
        "label_en",
        "module_en",
        "domain_en",
        "category_en",
        "topic_en",
        "title_en",
        "en_title",
        "notice_title",
        "title",
        "topic",
        "category",
        "domain",
        "module",
    ]

    detected_module = "unknown"
    detected_label_bn = None
    detected_label_en = None

    # First detect module from payload
    for point in points:
        payload = point.get("payload", {}) or {}
        module_value = get_payload_value(payload, module_keys, max_len=120)

        if module_value:
            inferred = infer_module_from_text(module_value)

            if inferred != "unknown":
                detected_module = inferred
            else:
                # For future modules like news/legal/training/procurement
                detected_module = normalize_key(module_value)

            break

    # Then detect labels from payload
    for point in points:
        payload = point.get("payload", {}) or {}

        if not detected_label_bn:
            detected_label_bn = get_payload_value(payload, label_keys_bn, max_len=120)

        if not detected_label_en:
            detected_label_en = get_payload_value(payload, label_keys_en, max_len=120)

        if detected_label_bn and detected_label_en:
            break

    # Fallback from collection name only if payload does not have enough metadata
    if detected_module == "unknown":
        detected_module = infer_module_from_text(collection_name)

    readable_collection_name = collection_name.replace("_", " ").replace("-", " ").title()

    if not detected_label_bn:
        detected_label_bn = readable_collection_name

    if not detected_label_en:
        detected_label_en = readable_collection_name

    return {
        "module": detected_module,
        "label_bn": detected_label_bn,
        "label_en": detected_label_en,
    }


def get_collection_meta(
    collection_name: str,
    points: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    return extract_meta_from_points(collection_name, points)


# ============================================================
# 5. Dynamic access/permission from Qdrant payload
# ============================================================

def get_user_role_names(request) -> Set[str]:
    user = getattr(request, "user", None)

    if not user or not getattr(user, "is_authenticated", False):
        return {"anonymous", "public"}

    roles = {"authenticated"}

    if getattr(user, "is_superuser", False):
        roles.add("super_admin")
        roles.add("admin")

    if getattr(user, "is_staff", False):
        roles.add("staff")

    try:
        group_names = set(user.groups.values_list("name", flat=True))
        for group in group_names:
            roles.add(normalize_key(group))
    except Exception:
        pass

    return roles


def is_publicish_collection(collection_name: str, points: List[Dict[str, Any]]) -> bool:
    """
    Safe fallback for website/public collection only.
    For future collection like news, set visibility='public' in payload.
    """
    text_parts = [collection_name]

    for point in points[:5]:
        payload = point.get("payload", {}) or {}
        for key in ["module", "domain", "category", "source", "collection_label_en"]:
            val = short_value(payload.get(key), max_len=80)
            if val:
                text_parts.append(val)

    combined = normalize_text(" ".join(text_parts))

    public_words = ["website", "public", "web", "burobd", "buro website"]
    return any(w in combined for w in public_words)


def collection_access_from_points(
    collection_name: str,
    points: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Reads access policy from Qdrant payload.
    No Python code change needed for new collections.

    Recommended payload:
    "visibility": "public" or "private"
    "allowed_roles": ["admin", "staff", "news"]
    """
    visibility = None
    allowed_roles: Set[str] = set()

    visibility_keys = [
        "visibility",
        "access",
        "access_level",
        "permission",
        "privacy",
    ]

    role_keys = [
        "allowed_roles",
        "roles",
        "allowed_groups",
        "groups",
        "allowed_users",
        "permissions",
    ]

    for point in points or []:
        payload = point.get("payload", {}) or {}

        if not visibility:
            visibility = get_payload_value(payload, visibility_keys, max_len=80)

        for key in role_keys:
            for role in payload_list_value(payload.get(key)):
                allowed_roles.add(role)

    if visibility:
        final_visibility = normalize_key(visibility)
    else:
        # Secure default:
        # only public-like website collections are public by fallback.
        # everything else is private unless payload says public.
        final_visibility = "public" if is_publicish_collection(collection_name, points) else "private"

    return {
        "visibility": final_visibility,
        "allowed_roles": allowed_roles,
    }


def is_collection_allowed_dynamic(
    collection_name: str,
    points: List[Dict[str, Any]],
    request,
) -> bool:
    """
    Dynamic permission rule:
    1. super_admin/admin/staff can see all
    2. visibility=public/open/anonymous can be seen by everyone
    3. otherwise user role/group must match payload allowed_roles
    """
    user_roles = get_user_role_names(request)

    if {"super_admin", "admin", "staff"} & user_roles:
        return True

    access = collection_access_from_points(collection_name, points)
    visibility = access.get("visibility")
    allowed_roles = access.get("allowed_roles", set())

    if visibility in {"public", "open", "anonymous", "guest"}:
        return True

    if allowed_roles and user_roles.intersection(allowed_roles):
        return True

    return False


# ============================================================
# 6. Dynamic topic extraction from Qdrant payload
# ============================================================

IMPORTANT_TOPIC_FIELDS = [
    "collection_label_bn",
    "collection_label_en",

    "topic_bn",
    "topic_en",
    "topic",
    "topic_id",

    "title_bn",
    "title_en",
    "bn_title",
    "en_title",
    "title",

    "category_bn",
    "category_en",
    "category",
    "sub_category",

    "intent",
    "domain",
    "module",

    "question",
    "query",

    "notice_title",
    "file_name",
    "filename",

    "clause",
    "section",
    "source",
]

LONG_TEXT_FIELDS = {
    "text",
    "content",
    "body",
    "answer",
    "description",
    "chunk",
    "page_content",
    "raw_text",
    "embedding_text",
}


def extract_topic_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    parts: List[str] = []

    for field in IMPORTANT_TOPIC_FIELDS:
        val = short_value(payload.get(field), max_len=120)
        if val and val not in parts:
            parts.append(val)

    if parts:
        return " | ".join(parts[:5])

    # Fallback: first useful short string field
    for k, v in payload.items():
        if k in LONG_TEXT_FIELDS:
            continue

        val = short_value(v, max_len=120)
        if val:
            return f"{k}: {val}"

    return None


def extract_topics_from_points(
    points: List[Dict[str, Any]],
    max_topics: int = 10,
) -> List[str]:
    topics: List[str] = []
    seen: Set[str] = set()

    for point in points:
        payload = point.get("payload", {}) or {}
        topic = extract_topic_from_payload(payload)

        if not topic:
            continue

        key = normalize_text(topic)

        if key in seen:
            continue

        seen.add(key)
        topics.append(topic)

        if len(topics) >= max_topics:
            break

    return topics


# ============================================================
# 7. Main capability builder
# ============================================================

def build_dynamic_capability_data(
    request=None,
    max_topics_per_collection: int = 8,
) -> Dict[str, Any]:
    try:
        all_collections = qdrant_get_collections()
    except Exception as e:
        logger.exception("Could not read Qdrant collections")
        return {
            "ok": False,
            "qdrant_url": get_qdrant_url(),
            "error": str(e),
            "collections": [],
        }

    result_items: List[Dict[str, Any]] = []

    for collection in all_collections:
        try:
            points = qdrant_scroll_sample(collection, limit=40)

            if request is not None:
                allowed = is_collection_allowed_dynamic(collection, points, request)
                if not allowed:
                    continue

            meta = get_collection_meta(collection, points)
            topics = extract_topics_from_points(points, max_topics=max_topics_per_collection)
            access = collection_access_from_points(collection, points)

            result_items.append({
                "collection": collection,
                "module": meta.get("module"),
                "label_bn": meta.get("label_bn"),
                "label_en": meta.get("label_en"),
                "visibility": access.get("visibility"),
                "allowed_roles": sorted(list(access.get("allowed_roles", set()))),
                "sample_points_read": len(points),
                "topics": topics,
                "error": None,
            })

        except Exception as e:
            logger.exception("Could not read Qdrant collection: %s", collection)

            meta = get_collection_meta(collection, points=[])

            result_items.append({
                "collection": collection,
                "module": meta.get("module"),
                "label_bn": meta.get("label_bn"),
                "label_en": meta.get("label_en"),
                "visibility": "unknown",
                "allowed_roles": [],
                "sample_points_read": 0,
                "topics": [],
                "error": str(e),
            })

    return {
        "ok": True,
        "qdrant_url": get_qdrant_url(),
        "total_collections": len(all_collections),
        "allowed_collections": len(result_items),
        "collections": result_items,
    }


def render_dynamic_capability_answer(request=None) -> str:
    data = build_dynamic_capability_data(request=request)

    if not data.get("ok"):
        return (
            "আমি এখন live knowledge base থেকে capability list পড়তে পারছি না। "
            "সম্ভবত Qdrant URL, network, service বা collection access issue আছে।\n\n"
            f"Qdrant URL: {data.get('qdrant_url')}\n"
            f"Error: {data.get('error')}"
        )

    collections = data.get("collections", [])

    if not collections:
        return (
            "বর্তমানে আপনার user role অনুযায়ী কোনো readable knowledge collection পাওয়া যায়নি। "
            "Admin role, group permission, visibility বা Qdrant payload metadata check করা প্রয়োজন।"
        )

    lines: List[str] = []

    lines.append(
        "আমি live knowledge base দেখে বলতে পারছি, বর্তমানে আমি নিচের বিষয়গুলোতে সাহায্য করতে পারি:\n"
    )

    for idx, item in enumerate(collections, start=1):
        label_bn = item.get("label_bn") or item.get("collection")
        collection = item.get("collection")
        module = item.get("module")
        visibility = item.get("visibility")

        lines.append(f"{idx}. {label_bn}")
        lines.append(f"   Collection: {collection}")
        lines.append(f"   Module: {module}")
        lines.append(f"   Visibility: {visibility}")

        if item.get("error"):
            lines.append("   Status: collection পাওয়া গেছে, কিন্তু sample topic পড়তে সমস্যা হয়েছে।")
            lines.append(f"   Error: {item.get('error')}")
            lines.append("")
            continue

        topics = item.get("topics") or []

        if topics:
            lines.append("   Available topic/sample:")
            for topic in topics:
                lines.append(f"   - {topic}")
        else:
            lines.append(
                "   Available topic/sample: collection active আছে, তবে readable topic/title payload পাওয়া যায়নি।"
            )

        lines.append("")

    lines.append(
        "সংক্ষেপে: এই capability list fixed text নয়। "
        "এটি Qdrant-এর current collections এবং live payload metadata থেকে dynamically তৈরি হয়েছে। "
        "নতুন collection যেমন news/training/legal/procurement যোগ করলে code change লাগবে না, "
        "শুধু Qdrant payload-এ module, label, visibility এবং topic metadata রাখতে হবে।"
    )

    return "\n".join(lines)


def capability_response_payload(request=None) -> Dict[str, Any]:
    answer = render_dynamic_capability_answer(request=request)

    # Multiple keys are returned so existing frontend can use answer/reply/response
    return {
        "answer": answer,
        "reply": answer,
        "response": answer,
        "source": "qdrant_dynamic_capability",
    }


@require_GET
def dynamic_capability_view(request):
    """
    Test:
    /api/dynamic-capability/
    /api/dynamic-capability/?format=json
    """
    if request.GET.get("format") == "json":
        data = build_dynamic_capability_data(request=request)
        return JsonResponse(data, json_dumps_params={"ensure_ascii": False})

    payload = capability_response_payload(request=request)
    return JsonResponse(payload, json_dumps_params={"ensure_ascii": False})
