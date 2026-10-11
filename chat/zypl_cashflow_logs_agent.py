from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional

import requests

from .zypl_cashflow_scoring_agent import (
    ZYPL_BASE_URL,
    ZYPL_MODEL_ID,
    ZYPL_PARTNER_ID,
    ZYPL_TIMEOUT_SECONDS,
    ZyplAPIError,
    user_can_use_zypl,
    zypl_login,
)

logger = logging.getLogger(__name__)

SESSION_KEY = "zypl_cashflow_logs_agent_state"

LOG_INTENT_KEYWORDS = [
    "zypl logs",
    "zypl log",
    "zypl result",
    "zypl results",
    "zypl records",
    "zypl record",
    "zypl history",
    "ypl logs",
    "ypl log",
    "ypl result",
    "ypl results",
    "ypl records",
    "ypl record",
    "ypl history",
    "zyp logs",
    "zyp log",
    "zyp result",
    "zyp results",
    "zyp records",
    "zyp record",
    "zyp history",
    "scoring logs",
    "scoring log",
    "scoring result",
    "scoring results",
    "scoring records",
    "score history",
    "previous zypl score",
    "fetch zypl result",
    "get zypl result",
    "show zypl records",
    "show all zypl records",
    "all zypl records",
    "give all zypl records",
    "রেজাল্ট দেখাও",
    "স্কোর রেজাল্ট",
    "স্কোর লগ",
    "জাইপল রেজাল্ট",
]


def is_zypl_logs_intent(message: str) -> bool:
    msg = (message or "").strip().lower()

    if not msg:
        return False

    if any(k in msg for k in LOG_INTENT_KEYWORDS):
        return True

    if any(z in msg for z in ["zypl", "ypl", "zyp"]) and any(
        w in msg for w in ["record", "records", "result", "results", "log", "logs", "history"]
    ):
        return True

    return False


def get_logs_state(request) -> Dict[str, Any]:
    state = request.session.get(SESSION_KEY)

    if not isinstance(state, dict):
        state = {}

    state.setdefault("filters", {})
    state.setdefault("waiting_for_filter", False)

    return state


def save_logs_state(request, state: Dict[str, Any]) -> None:
    request.session[SESSION_KEY] = state
    request.session.modified = True


def clear_logs_state(request) -> None:
    if SESSION_KEY in request.session:
        del request.session[SESSION_KEY]
        request.session.modified = True


def is_reset_message(message: str) -> bool:
    return (message or "").strip().lower() in [
        "reset",
        "clear",
        "cancel",
        "restart",
        "রিসেট",
        "বাতিল",
    ]


def _int_or_zero(value: Any) -> int:
    try:
        if value in [None, ""]:
            return 0
        return int(value)
    except Exception:
        return 0


def _month_range_today() -> Dict[str, str]:
    today = date.today()
    return {
        "start_date": today.replace(day=1).isoformat(),
        "end_date": today.isoformat(),
    }


def parse_logs_filters_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}

    for key in [
        "page",
        "limit",
        "mlmodel_id",
        "start_date",
        "end_date",
        "scoring_id",
        "user_id",
        "client_app_id",
    ]:
        value = payload.get(key)
        if value not in [None, ""]:
            parsed[key] = value

    if isinstance(payload.get("value_filters"), dict):
        parsed["value_filters"] = dict(payload["value_filters"])

    # Friendly top-level customer_id support
    if payload.get("customer_id") not in [None, ""]:
        parsed.setdefault("value_filters", {})
        parsed["value_filters"]["customer_id"] = _int_or_zero(payload.get("customer_id"))

    return parsed


def parse_logs_filters_from_message(message: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}

    if not message:
        return parsed

    text = message.strip()
    text_l = text.lower()

    # JSON support
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return parse_logs_filters_from_payload(data)
    except Exception:
        pass

    # Date range support: 2026-05-01 and 2026-05-31
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if len(dates) >= 1:
        parsed["start_date"] = dates[0]
    if len(dates) >= 2:
        parsed["end_date"] = dates[1]

    patterns = {
        "client_app_id": [
            r"client\s*app\s*id\s*(?:=|:|-|is)?\s*([A-Za-z0-9._/-]+)",
            r"client_app_id\s*(?:=|:|-|is)?\s*([A-Za-z0-9._/-]+)",
            r"loan\s*application\s*id\s*(?:=|:|-|is)?\s*([A-Za-z0-9._/-]+)",
            r"application\s*id\s*(?:=|:|-|is)?\s*([A-Za-z0-9._/-]+)",
            r"\b(APP-[A-Za-z0-9._/-]+)\b",
        ],
        "customer_id": [
            r"customer\s*id\s*(?:=|:|-|is)?\s*(\d+)",
            r"customer_id\s*(?:=|:|-|is)?\s*(\d+)",
        ],
        "scoring_id": [
            r"scoring\s*id\s*(?:=|:|-|is)?\s*(\d+)",
            r"scoring_id\s*(?:=|:|-|is)?\s*(\d+)",
            r"score\s*id\s*(?:=|:|-|is)?\s*(\d+)",
            r"record\s*id\s*(?:=|:|-|is)?\s*(\d+)",
            r"record_id\s*(?:=|:|-|is)?\s*(\d+)",
        ],
        "user_id": [
            r"user\s*id\s*(?:=|:|-|is)?\s*(\d+)",
            r"user_id\s*(?:=|:|-|is)?\s*(\d+)",
        ],
        "limit": [
            r"limit\s*(?:=|:|-|is)?\s*(\d+)",
            r"last\s+(\d+)",
            r"top\s+(\d+)",
            r"show\s+(\d+)",
        ],
        "page": [
            r"page\s*(?:=|:|-|is)?\s*(\d+)",
        ],
    }

    for field, pats in patterns.items():
        for pat in pats:
            match = re.search(pat, text, flags=re.IGNORECASE)
            if not match:
                continue

            value = match.group(1).strip()

            if field == "customer_id":
                parsed.setdefault("value_filters", {})
                parsed["value_filters"]["customer_id"] = int(value)
            elif field in ["scoring_id", "user_id", "limit", "page"]:
                parsed[field] = int(value)
            else:
                parsed[field] = value

            break

    # Show all / recent records support.
    # Examples:
    #   show all zypl records between 2026-05-01 and 2026-05-31
    #   zypl logs last 20
    #   show recent zypl results
    #   fetch top 50 zypl scoring logs
    show_all_words = [
        "show all",
        "all records",
        "give all records",
        "all zypl records",
        "all ypl records",
        "all zyp records",
        "recent",
        "latest",
        "last ",
        "top ",
        "সব রেকর্ড",
    ]

    if any(x in text_l for x in show_all_words):
        parsed["show_all"] = True

        # If user did not mention last/top/limit/show number,
        # default to 100 instead of 50 for "show all".
        parsed.setdefault("limit", parsed.get("limit") or 100)

    return parsed


def build_scoring_logs_payload(filters: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build Zypl scoring_logs payload.

    Important:
    Do NOT send empty/zero filters such as:
      scoring_id: 0
      user_id: 0
      client_app_id: ""
      value_filters: {}

    Some APIs treat those as real filters and return no records.
    """
    month_range = _month_range_today()

    payload: Dict[str, Any] = {
        "page": _int_or_zero(filters.get("page")) or 1,
        "limit": _int_or_zero(filters.get("limit")) or 50,
        "mlmodel_id": filters.get("mlmodel_id") or ZYPL_MODEL_ID,
        "start_date": filters.get("start_date") or month_range["start_date"],
        "end_date": filters.get("end_date") or month_range["end_date"],
    }

    scoring_id = _int_or_zero(filters.get("scoring_id"))
    if scoring_id > 0:
        payload["scoring_id"] = scoring_id

    user_id = _int_or_zero(filters.get("user_id"))
    if user_id > 0:
        payload["user_id"] = user_id

    client_app_id = str(filters.get("client_app_id") or "").strip()
    if client_app_id:
        payload["client_app_id"] = client_app_id

    value_filters = filters.get("value_filters")
    if isinstance(value_filters, dict):
        clean_value_filters = {
            k: v for k, v in value_filters.items()
            if v not in [None, ""]
        }
        if clean_value_filters:
            payload["value_filters"] = clean_value_filters

    return payload


def has_enough_filter(filters: Dict[str, Any]) -> bool:
    if filters.get("show_all"):
        return True

    if filters.get("client_app_id"):
        return True

    if _int_or_zero(filters.get("scoring_id")):
        return True

    if _int_or_zero(filters.get("user_id")):
        return True

    value_filters = filters.get("value_filters")
    if isinstance(value_filters, dict) and any(v not in [None, ""] for v in value_filters.values()):
        return True

    # Date range query is also valid.
    if filters.get("start_date") and filters.get("end_date"):
        return True

    return False


def build_filter_question() -> str:
    return (
        "কোন Zypl scoring logs/result দেখতে চান?\n"
        "Please provide a filter.\n\n"
        "Examples:\n"
        "- show all zypl records between 2026-05-01 and 2026-05-31\n"
        "- show all zypl records between 2026-05-01 and 2026-05-31 limit 100\n"
        "- zypl result scoring_id 145\n"
        "- zypl result customer_id 2605137949\n"
        "- zypl logs client_app_id APP-20260513-E2C7949B\n"
        "- zypl logs last 20\n"
        "- zypl logs limit 100 page 2"
    )


def zypl_fetch_scoring_logs(filters: Dict[str, Any]) -> Dict[str, Any]:
    token = zypl_login()

    url = f"{ZYPL_BASE_URL}/partner/{ZYPL_PARTNER_ID}/scoring_logs"
    payload = build_scoring_logs_payload(filters)

    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=ZYPL_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ZyplAPIError(f"Could not connect to Zypl scoring_logs endpoint: {exc}") from exc

    if response.status_code in [401, 403]:
        token = zypl_login(force_refresh=True)

        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=ZYPL_TIMEOUT_SECONDS,
        )

    if response.status_code >= 400:
        raise ZyplAPIError(
            f"Zypl scoring_logs failed. HTTP {response.status_code}: {response.text[:2000]}"
        )

    try:
        raw = response.json()
    except Exception as exc:
        raise ZyplAPIError(
            f"Zypl scoring_logs returned non-JSON response: {response.text[:500]}"
        ) from exc

    return {
        "payload_used": payload,
        "raw_response": raw,
    }


def extract_records(raw_response: Any) -> List[Dict[str, Any]]:
    """
    Extract records from possible Zypl response shapes.

    Supports:
      list
      {"results": [...]}
      {"data": [...]}
      {"data": {"items": [...]}}
      {"result": [...]}
      {"result": {"data": [...]}}
      {"payload": {"data": [...]}}
    """
    if isinstance(raw_response, list):
        return [x for x in raw_response if isinstance(x, dict)]

    if not isinstance(raw_response, dict):
        return []

    direct_keys = [
        "results",
        "data",
        "items",
        "logs",
        "scoring_logs",
        "scoring",
        "records",
        "rows",
    ]

    for key in direct_keys:
        value = raw_response.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

    nested_keys = ["data", "result", "payload", "response"]
    nested_list_keys = [
        "results",
        "data",
        "items",
        "logs",
        "scoring_logs",
        "scoring",
        "records",
        "rows",
    ]

    for nk in nested_keys:
        nested = raw_response.get(nk)
        if isinstance(nested, dict):
            for lk in nested_list_keys:
                value = nested.get(lk)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]

    if raw_response.get("id") or raw_response.get("client_app_id"):
        return [raw_response]

    return []


def summarize_record(row: Dict[str, Any]) -> Dict[str, Any]:
    request_data = row.get("request_data") if isinstance(row.get("request_data"), dict) else {}

    return {
        "id": row.get("id") or row.get("scoring_id"),
        "client_app_id": row.get("client_app_id"),
        "customer_id": row.get("customer_id") or request_data.get("customer_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "status": row.get("status"),
        "prediction": row.get("prediction"),
        "prediction_score": row.get("prediction_score"),
        "is_approved": row.get("is_approved"),
        "risk_status": row.get("scoring_risk_status") or row.get("risk_status"),
        "threshold": row.get("threshold"),
        "reason": row.get("reason"),
        "model_id": row.get("model_id"),
    }


def build_logs_answer(raw_response: Any, payload_used: Dict[str, Any]) -> Dict[str, Any]:
    records = extract_records(raw_response)

    requested_limit = _int_or_zero(payload_used.get("limit"))

    # Display as many records as requested by API limit.
    # Example:
    #   limit 20  => show 20
    #   limit 100 => show 100
    #
    # Safety cap:
    #   Avoid producing an extremely huge chat response.
    #   Increase this value if you really want more than 500 shown in chat.
    max_display_limit = 500

    if requested_limit > 0:
        display_limit = min(requested_limit, max_display_limit)
    else:
        display_limit = min(len(records), max_display_limit)

    preview = [summarize_record(x) for x in records[:display_limit]]

    lines = [
        "Zypl Scoring Logs Result",
        "",
        f"Records returned: {len(records)}",
        f"Records displayed: {len(preview)}",
        f"Page: {payload_used.get('page')}",
        f"Limit: {payload_used.get('limit')}",
        f"Date Range: {payload_used.get('start_date')} to {payload_used.get('end_date')}",
    ]

    if payload_used.get("client_app_id"):
        lines.append(f"Client App ID: {payload_used.get('client_app_id')}")

    if payload_used.get("scoring_id"):
        lines.append(f"Scoring ID: {payload_used.get('scoring_id')}")

    if payload_used.get("user_id"):
        lines.append(f"User ID: {payload_used.get('user_id')}")

    if payload_used.get("value_filters"):
        lines.append(f"Value Filters: {payload_used.get('value_filters')}")

    if not preview:
        lines.append("")
        lines.append("No scoring log record found.")

        return {
            "answer": "\n".join(lines),
            "records_preview": preview,
            "records_count": len(records),
            "records_displayed": len(preview),
        }

    lines.append("")
    lines.append("Preview:")

    for i, row in enumerate(preview, start=1):
        lines.append("")
        lines.append(f"{i}. Scoring ID: {row.get('id')}")
        lines.append(f"   Client App ID: {row.get('client_app_id')}")
        lines.append(f"   Customer ID: {row.get('customer_id')}")
        lines.append(f"   Status: {row.get('status')}")
        lines.append(f"   Approved: {row.get('is_approved')}")
        lines.append(f"   Risk: {row.get('risk_status')}")
        lines.append(f"   Prediction: {row.get('prediction')}")
        lines.append(f"   Created: {row.get('created_at')}")

        if row.get("reason"):
            lines.append(f"   Reason: {row.get('reason')}")

    if requested_limit > max_display_limit:
        lines.append("")
        lines.append(
            f"Note: Requested limit was {requested_limit}, but chat display is capped at "
            f"{max_display_limit}. The raw_response still contains the API response."
        )

    if requested_limit > 0 and len(records) >= requested_limit:
        lines.append("")
        lines.append(
            "Note: More records may exist on the next page. "
            f"Try: zypl logs limit {requested_limit} page {int(payload_used.get('page') or 1) + 1}"
        )

    return {
        "answer": "\n".join(lines),
        "records_preview": preview,
        "records_count": len(records),
        "records_displayed": len(preview),
    }


def process_zypl_logs_agent(request, payload: Dict[str, Any]) -> Dict[str, Any]:
    message = str(
        payload.get("message")
        or payload.get("query")
        or payload.get("question")
        or ""
    ).strip()

    if not user_can_use_zypl(request):
        return {
            "ok": False,
            "intent": "zypl_cashflow_scoring_logs",
            "tool": "zypl_cashflow_logs_agent",
            "status": "permission_denied",
            "answer": "এই Zypl scoring logs service ব্যবহার করার জন্য cashflow role/group access প্রয়োজন।",
            "required_role": "cashflow",
            "public": False,
            "login_required": True,
        }

    if is_reset_message(message):
        clear_logs_state(request)

        return {
            "ok": True,
            "intent": "zypl_cashflow_scoring_logs",
            "tool": "zypl_cashflow_logs_agent",
            "status": "reset",
            "answer": "Zypl scoring logs session reset করা হয়েছে।",
            "public": False,
            "login_required": True,
        }

    state = get_logs_state(request)
    filters = state.get("filters") or {}

    filters.update(parse_logs_filters_from_payload(payload))
    filters.update(parse_logs_filters_from_message(message))

    if state.get("waiting_for_filter") and message and not has_enough_filter(filters):
        if message.strip().isdigit():
            filters.setdefault("value_filters", {})
            filters["value_filters"]["customer_id"] = int(message.strip())

    state["filters"] = filters
    save_logs_state(request, state)

    if not has_enough_filter(filters):
        state["waiting_for_filter"] = True
        save_logs_state(request, state)

        return {
            "ok": True,
            "intent": "zypl_cashflow_scoring_logs",
            "tool": "zypl_cashflow_logs_agent",
            "status": "need_more_input",
            "answer": build_filter_question(),
            "filters_collected": filters,
            "public": False,
            "login_required": True,
        }

    try:
        result = zypl_fetch_scoring_logs(filters)
        formatted = build_logs_answer(result["raw_response"], result["payload_used"])

        clear_logs_state(request)

        return {
            "ok": True,
            "intent": "zypl_cashflow_scoring_logs",
            "tool": "zypl_cashflow_logs_agent",
            "status": "fetched",
            "answer": formatted["answer"],
            "records_count": formatted["records_count"],
            "records_displayed": formatted["records_displayed"],
            "records_preview": formatted["records_preview"],
            "payload_used": result["payload_used"],
            "raw_response": result["raw_response"],
            "public": False,
            "login_required": True,
        }

    except ZyplAPIError as exc:
        return {
            "ok": False,
            "intent": "zypl_cashflow_scoring_logs",
            "tool": "zypl_cashflow_logs_agent",
            "status": "error",
            "answer": str(exc),
            "payload_that_would_be_sent": build_scoring_logs_payload(filters),
            "public": False,
            "login_required": True,
        }

    except Exception as exc:
        logger.exception("Unexpected Zypl scoring logs error")

        return {
            "ok": False,
            "intent": "zypl_cashflow_scoring_logs",
            "tool": "zypl_cashflow_logs_agent",
            "status": "error",
            "answer": f"Unexpected Zypl scoring logs error: {exc}",
            "public": False,
            "login_required": True,
        }


def try_handle_zypl_logs_agent(request, user_message: str) -> Optional[Dict[str, Any]]:
    if request.session.get(SESSION_KEY) or is_zypl_logs_intent(user_message):
        return process_zypl_logs_agent(request, {"message": user_message})

    return None
