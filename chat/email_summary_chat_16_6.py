# chat/email_summary_chat_feature.py
from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------
# Path config
# ---------------------------------------------------------------------

def _default_base_dir() -> Path:
    return Path(__file__).resolve().parent / "email_process_data"


BASE_DIR = Path(os.getenv("EMAIL_PROCESS_BASE_DIR", str(_default_base_dir())))
SUMMARY_INDEX_FILE = BASE_DIR / "summary_index.json"


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
    "run email summary",
    "run email attachment summary",
    "email summary status",

    "ইমেইল সংযুক্তির সারাংশ",
    "ইমেইল এটাচমেন্ট সারাংশ",
    "ইমেইল অ্যাটাচমেন্ট সারাংশ",
    "ইমেইল সারাংশ",
    "সংযুক্তি সারাংশ",
    "সর্বশেষ ইমেইল সারাংশ",
    "ইমেইল সারাংশ দেখাও",
    "ইমেইল সারাংশ রান করো",
    "ইমেইল সারাংশ চালাও",
    "ইমেইল সারাংশ স্ট্যাটাস",
]

EMAIL_SUMMARY_REFRESH_HINTS = [
    "run",
    "refresh",
    "check new",
    "process new",
    "fetch new",
    "start",
    "update",
    "run email",
    "run summary",

    "রান",
    "চালাও",
    "চালান",
    "চেক করো",
    "চেক করুন",
    "নতুন",
    "আপডেট",
    "প্রসেস",
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

    # Remove markdown decorations but keep readable content.
    text = text.replace("###", "")
    text = text.replace("##", "")
    text = text.replace("**", "")
    text = text.replace("__", "")

    # Remove old fixed-template empty sections if any old HTML/summary was stored.
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
    """
    Return the actual generated summary only.

    Priority:
    1. summary_markdown
    2. summary
    3. answer
    4. text
    5. summary_html, only as fallback after stripping HTML

    No fixed applicant/goat template is applied here.
    """
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


# ---------------------------------------------------------------------
# Intent functions used by views.py
# ---------------------------------------------------------------------

def is_email_summary_intent(message: str) -> bool:
    return _contains_any(message, EMAIL_SUMMARY_INTENT_KEYWORDS)


def wants_refresh(message: str) -> bool:
    return _contains_any(message, EMAIL_SUMMARY_REFRESH_HINTS)


# ---------------------------------------------------------------------
# Summary index loading
# ---------------------------------------------------------------------

def load_summary_index() -> List[Dict[str, Any]]:
    if not SUMMARY_INDEX_FILE.exists():
        return []

    try:
        data = json.loads(SUMMARY_INDEX_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_latest_summaries(limit: int = 1) -> List[Dict[str, Any]]:
    """
    Return latest summaries.

    Default is 1 because user asked for simple latest summary only.
    views.py may pass limit=3, but this function still respects that if needed.
    """
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
# Rendering: summary only, no fixed template
# ---------------------------------------------------------------------

def summary_to_markdown_card(record: Dict[str, Any], index_no: int = 1) -> str:
    meta = _record_meta(record)
    summary = _extract_best_summary_text(record)

    lines = [
        "ইমেইল সংযুক্তির সারাংশ",
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
    """
    Compatibility function.

    Current frontend displays HTML tags as raw text, so this intentionally
    returns plain text.
    """
    return summary_to_markdown_card(record, index_no=index_no)


def make_summary_markdown(records: List[Dict[str, Any]]) -> str:
    if not records:
        return make_empty_email_summary_answer()["answer"]

    cards = []

    for i, record in enumerate(records, start=1):
        cards.append(summary_to_markdown_card(record, i))

    return "\n\n------------------------------\n\n".join(cards).strip()


def make_summary_html(records: List[Dict[str, Any]]) -> str:
    """
    Compatibility with views.py.

    Existing views.py calls make_summary_html(), but we return plain text
    to avoid raw HTML display.
    """
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


def make_empty_email_summary_answer() -> Dict[str, Any]:
    answer = (
        "ইমেইল সংযুক্তির সারাংশ পাওয়া যায়নি।\n\n"
        "নতুন ইমেইল সংযুক্তি প্রসেস করতে লিখুন:\n"
        "run email summary\n\n"
        "স্ট্যাটাস দেখতে লিখুন:\n"
        "email summary status\n\n"
        "সর্বশেষ সারাংশ দেখতে লিখুন:\n"
        "mail-attachment-summary"
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


# ---------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------

def handle_email_attachment_summary_intent(message: str) -> Optional[Dict[str, Any]]:
    if not is_email_summary_intent(message):
        return None

    records = get_latest_summaries(limit=1)

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
