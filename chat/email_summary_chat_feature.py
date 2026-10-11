# chat/email_summary_chat_feature.py
from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from django.conf import settings
except Exception:
    settings = None


# ---------------------------------------------------------------------
# Path config
# ---------------------------------------------------------------------

def _project_root() -> Path:
    """
    Return Django project root.

    Expected:
      /home/mamun/finalapp/app
    """
    try:
        if settings is not None and getattr(settings, "BASE_DIR", None):
            return Path(settings.BASE_DIR).resolve()
    except Exception:
        pass

    # this file is: /home/mamun/finalapp/app/chat/email_summary_chat_feature.py
    # parents[1] is: /home/mamun/finalapp/app
    return Path(__file__).resolve().parents[1]


def _default_base_dir() -> Path:
    """
    Correct active email summary directory.

    Correct:
      /home/mamun/finalapp/app/email_process_data

    Old wrong location:
      /home/mamun/finalapp/app/chat/email_process_data
    """
    return _project_root() / "email_process_data"


def _legacy_chat_base_dir() -> Path:
    """
    Old location. Kept as fallback so old summaries still show.
    """
    return Path(__file__).resolve().parent / "email_process_data"


BASE_DIR = Path(os.getenv("EMAIL_PROCESS_BASE_DIR", str(_default_base_dir()))).resolve()
LEGACY_BASE_DIR = _legacy_chat_base_dir().resolve()

BASE_DIR.mkdir(parents=True, exist_ok=True)

SUMMARY_INDEX_FILE = BASE_DIR / "summary_index.json"
LEGACY_SUMMARY_INDEX_FILE = LEGACY_BASE_DIR / "summary_index.json"

if not SUMMARY_INDEX_FILE.exists():
    try:
        SUMMARY_INDEX_FILE.write_text("[]", encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------
# Intent keywords
# ---------------------------------------------------------------------

EMAIL_SUMMARY_INTENT_KEYWORDS = [
    "email_attachment_summary",
    "email-attachment-summary",
    "email attachment summary",
    "email summary",
    "email_summary",
    "attachment_summary",
    "attachment-summary",
    "attachment summary",
    "mail attachment summary",
    "mail-attachment-summary",
    "mail_attachment_summary",
    "gmail attachment summary",
    "gmail summary",
    "show email summary",
    "show latest email summary",
    "latest email summary",
    "my email attachment",
    "summary my email attachment",
    "summarize my email attachment",
    "summarise my email attachment",
    "email summary status",

    "ইমেইল সংযুক্তির সারাংশ",
    "ইমেইল এটাচমেন্ট সারাংশ",
    "ইমেইল অ্যাটাচমেন্ট সারাংশ",
    "ইমেইল সারাংশ",
    "সংযুক্তি সারাংশ",
    "সর্বশেষ ইমেইল সারাংশ",
    "ইমেইল সারাংশ দেখাও",
    "ইমেইল সারাংশ স্ট্যাটাস",
]

EMAIL_SUMMARY_REFRESH_HINTS = [
    "run email summary",
    "run email attachment summary",
    "refresh email summary",
    "refresh email attachment summary",
    "check new",
    "check new email",
    "check new email summary",
    "process new email",
    "process new email summary",
    "fetch new email",
    "fetch new email summary",
    "start email summary",
    "update email summary",

    "ইমেইল সারাংশ রান করো",
    "ইমেইল সারাংশ চালাও",
    "ইমেইল সারাংশ চালান",
    "ইমেইল সারাংশ আপডেট",
    "নতুন ইমেইল চেক করো",
    "নতুন ইমেইল চেক করুন",
    "নতুন ইমেইল প্রসেস করো",
    "নতুন ইমেইল সারাংশ",
]

EMAIL_SUMMARY_STATUS_HINTS = [
    "email summary status",
    "mail attachment summary status",
    "email attachment summary status",
    "gmail summary status",
    "agent status",
    "email agent status",

    "ইমেইল সারাংশ স্ট্যাটাস",
    "ইমেইল এজেন্ট স্ট্যাটাস",
]

EMAIL_SUMMARY_ALL_HINTS = [
    "all email summary",
    "all email summaries",
    "show all email summary",
    "show all email summaries",
    "all mail attachment summary",
    "all attachment summary",
    "previous email summaries",
    "old email summaries",

    "সব ইমেইল সারাংশ",
    "সকল ইমেইল সারাংশ",
    "সব সংযুক্তির সারাংশ",
    "আগের ইমেইল সারাংশ",
]

EMAIL_SUMMARY_LATEST_HINTS = [
    "latest email summary",
    "show latest email summary",
    "latest mail attachment summary",
    "mail-attachment-summary",
    "email_attachment_summary",
    "email summary",
    "gmail summary",

    "সর্বশেষ ইমেইল সারাংশ",
    "ইমেইল সারাংশ দেখাও",
]


# ---------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------

def _clean(value: Any) -> str:
    return ("" if value is None else str(value)).strip()


def _normalize(value: str) -> str:
    value = (value or "").strip().lower()
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _contains_any(text: str, keywords: List[str]) -> bool:
    raw = (text or "").strip().lower()
    normalized = _normalize(raw)

    if not raw:
        return False

    for keyword in keywords:
        k_raw = keyword.lower().strip()
        k_norm = _normalize(k_raw)

        if k_raw and k_raw in raw:
            return True

        if k_norm and k_norm in normalized:
            return True

    return False


def _strip_html(text: str) -> str:
    text = _clean(text)

    if not text:
        return ""

    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</div\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _remove_noise(text: str) -> str:
    text = _clean(text)

    if not text:
        return ""

    text = text.replace("###", "")
    text = text.replace("##", "")
    text = text.replace("**", "")
    text = text.replace("__", "")

    noise_patterns = [
        r"আবেদনকারীর পরিচিতি\s*\n\s*উল্লেখ নেই",
        r"পারিবারিক অবস্থা\s*\n\s*উল্লেখ নেই",
        r"আর্থিক অবস্থা\s*\n\s*উল্লেখ নেই",
        r"অভিজ্ঞতা ও সক্ষমতা\s*\n\s*উল্লেখ নেই",
        r"সহায়তার উদ্দেশ্য\s*\n\s*উল্লেখ নেই",
        r"ঝুঁকি/যাচাই প্রয়োজন\s*\n\s*উল্লেখ নেই",
        r"সংক্ষিপ্ত সুপারিশ\s*\n\s*উল্লেখ নেই",
    ]

    for pattern in noise_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _extract_best_summary_text(record: Dict[str, Any]) -> str:
    for key in ["summary_markdown", "summary", "answer", "text"]:
        value = _clean(record.get(key))

        if value:
            value = _strip_html(value)
            value = _remove_noise(value)

            if value:
                return value

    html_value = _clean(record.get("summary_html"))

    if html_value:
        value = _strip_html(html_value)
        value = _remove_noise(value)

        if value:
            return value

    return "সারাংশ পাওয়া যায়নি।"


def _record_meta(record: Dict[str, Any]) -> Dict[str, str]:
    return {
        "document": _clean(record.get("attachment_file_name")) or "উল্লেখ নেই",
        "subject": _clean(record.get("email_subject")) or "উল্লেখ নেই",
        "sender": _clean(record.get("email_sender")) or "উল্লেখ নেই",
        "processed_time": _clean(record.get("processed_time")) or "উল্লেখ নেই",
        "model": _clean(record.get("ollama_model")) or "উল্লেখ নেই",
    }


def _record_key(record: Dict[str, Any]) -> str:
    """
    Used to remove duplicate summary records if the same record exists in both
    root email_process_data and old chat/email_process_data.
    """
    parts = [
        _clean(record.get("attachment_file_name")),
        _clean(record.get("email_subject")),
        _clean(record.get("email_sender")),
        _clean(record.get("processed_time")),
        _clean(record.get("summary_file")),
        _clean(record.get("summary_json_file")),
    ]
    return "|".join(parts)


# ---------------------------------------------------------------------
# Intent functions used by views.py
# ---------------------------------------------------------------------

def is_email_summary_intent(message: str) -> bool:
    return (
        _contains_any(message, EMAIL_SUMMARY_INTENT_KEYWORDS)
        or wants_refresh(message)
        or wants_status(message)
        or wants_all_email_summaries(message)
    )


def wants_refresh(message: str) -> bool:
    return _contains_any(message, EMAIL_SUMMARY_REFRESH_HINTS)


def wants_status(message: str) -> bool:
    return _contains_any(message, EMAIL_SUMMARY_STATUS_HINTS)


def wants_all_email_summaries(message: str) -> bool:
    return _contains_any(message, EMAIL_SUMMARY_ALL_HINTS)


def wants_latest_email_summary(message: str) -> bool:
    return _contains_any(message, EMAIL_SUMMARY_LATEST_HINTS)


# ---------------------------------------------------------------------
# Summary index loading
# ---------------------------------------------------------------------

def _load_summary_index_file(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []

        return [item for item in data if isinstance(item, dict)]
    except Exception:
        return []


def load_summary_index() -> List[Dict[str, Any]]:
    """
    Load summaries from correct root directory.

    Also loads old chat/email_process_data/summary_index.json as fallback.
    This fixes the situation where old writer code created summaries under chat/.
    """
    records: List[Dict[str, Any]] = []

    root_records = _load_summary_index_file(SUMMARY_INDEX_FILE)
    legacy_records = []

    if LEGACY_SUMMARY_INDEX_FILE != SUMMARY_INDEX_FILE:
        legacy_records = _load_summary_index_file(LEGACY_SUMMARY_INDEX_FILE)

    seen = set()

    for record in root_records + legacy_records:
        key = _record_key(record)

        if key in seen:
            continue

        seen.add(key)
        records.append(record)

    return records


def get_latest_summaries(limit: int = 1) -> List[Dict[str, Any]]:
    try:
        safe_limit = int(limit or 1)
    except Exception:
        safe_limit = 1

    safe_limit = max(1, min(safe_limit, 10))

    records = load_summary_index()

    records = sorted(
        records,
        key=lambda x: _clean(x.get("processed_time")),
        reverse=True,
    )

    return records[:safe_limit]


# ---------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------

def summary_to_markdown_card(record: Dict[str, Any], index_no: int = 1) -> str:
    meta = _record_meta(record)
    summary = _extract_best_summary_text(record)

    lines = [
        f"ইমেইল সংযুক্তির সারাংশ #{index_no}",
        "",
        f"ডকুমেন্ট: {meta['document']}",
        f"ইমেইল বিষয়: {meta['subject']}",
        f"প্রেরক: {meta['sender']}",
        f"প্রসেস সময়: {meta['processed_time']}",
        "",
        "সারাংশ:",
        summary,
    ]

    return "\n".join(lines).strip()


def summary_to_html_card(record: Dict[str, Any], index_no: int = 1) -> str:
    return summary_to_markdown_card(record, index_no=index_no)


def make_summary_markdown(records: List[Dict[str, Any]]) -> str:
    if not records:
        return make_empty_email_summary_answer()["answer"]

    cards = []

    for i, record in enumerate(records, start=1):
        cards.append(summary_to_markdown_card(record, i))

    return "\n\n------------------------------\n\n".join(cards).strip()


def make_summary_html(records: List[Dict[str, Any]]) -> str:
    return make_summary_markdown(records)


def make_sources(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sources = []

    for record in records:
        sources.append(
            {
                "title": record.get("attachment_file_name", "Email attachment"),
                "email_subject": record.get("email_subject", ""),
                "sender": record.get("email_sender", ""),
                "processed_time": record.get("processed_time", ""),
                "summary_file": record.get("summary_file", ""),
                "summary_json_file": record.get("summary_json_file", ""),
            }
        )

    return sources


# ---------------------------------------------------------------------
# Response payloads
# ---------------------------------------------------------------------

def make_empty_email_summary_answer() -> Dict[str, Any]:
    answer = (
        "ইমেইল সংযুক্তির সারাংশ পাওয়া যায়নি।\n\n"
        "নতুন ইমেইল সংযুক্তি প্রসেস করতে লিখুন:\n"
        "run email summary\n\n"
        "স্ট্যাটাস দেখতে লিখুন:\n"
        "email summary status\n\n"
        "সর্বশেষ সারাংশ দেখতে লিখুন:\n"
        "latest email summary"
    )

    return {
        "ok": True,
        "intent": "email_attachment_summary",
        "answer": answer,
        "answer_markdown": answer,
        "answer_type": "text",
        "sources": [],
        "login_required": True,
        "role_required": True,
    }


def make_run_instruction_answer() -> Dict[str, Any]:
    answer = (
        "Email Summary Agent চালাতে লিখুন:\n\n"
        "run email summary\n\n"
        "অথবা:\n\n"
        "ইমেইল সারাংশ রান করো"
    )

    return {
        "ok": True,
        "intent": "email_attachment_summary",
        "answer": answer,
        "answer_markdown": answer,
        "answer_type": "text",
        "sources": [],
        "login_required": True,
        "role_required": True,
    }


def latest_email_summary_payload(limit: int = 1) -> Dict[str, Any]:
    records = get_latest_summaries(limit=limit)

    if not records:
        return make_empty_email_summary_answer()

    answer = make_summary_markdown(records)

    return {
        "ok": True,
        "intent": "email_attachment_summary",
        "tool": "email_attachment_llm_summarizer",
        "answer": answer,
        "answer_markdown": answer,
        "answer_type": "text",
        "sources": make_sources(records),
        "login_required": True,
        "role_required": True,
    }


def all_email_summary_payload(limit: int = 10) -> Dict[str, Any]:
    records = get_latest_summaries(limit=limit)

    if not records:
        return make_empty_email_summary_answer()

    answer = make_summary_markdown(records)

    return {
        "ok": True,
        "intent": "email_attachment_summary",
        "tool": "email_attachment_llm_summarizer",
        "answer": answer,
        "answer_markdown": answer,
        "answer_type": "text",
        "sources": make_sources(records),
        "login_required": True,
        "role_required": True,
    }


# ---------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------

def handle_email_attachment_summary_intent(message: str) -> Optional[Dict[str, Any]]:
    if not is_email_summary_intent(message):
        return None

    if wants_all_email_summaries(message):
        return all_email_summary_payload(limit=10)

    return latest_email_summary_payload(limit=1)
