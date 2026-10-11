# /home/mamun/app/chat/views_cashflow_router.py
# ------------------------------------------------------------
# CASHFLOW Router (Oracle 11g + NiFi truth) - FINAL v6
#
# ✅ Deterministic first (fast + safe KPI/list routes)
# ✅ Fallback to SQLCoder (Ollama) ONLY when unsupported/forced
# ✅ Oracle 11g guardrails (NO ILIKE/LIMIT/OFFSET/FETCH FIRST)
# ✅ Prevent "table-as-function" SQL: SCOTT.TABLE(col1,...)  (blocked)
# ✅ Qdrant schema assist (dw_cashflow_auto) using SCROLL (no /points/search 400)
# ✅ Friendly small explanation in response (not too much)
#
# Force SQLCoder from textbox/UI:
#   {"q":"customers in barisal", "mode":"sqlcoder"}
# or {"q":"...", "use_sqlcoder": true}
# ------------------------------------------------------------

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

JsonT = Union[Dict[str, Any], List[Any]]
log = logging.getLogger("cashflow")

BUILD = "cashflow_router_2026-03-04_oracle11g_qdrant_sqlcoder_final_v6"

# =========================================================
# Config helpers
# =========================================================
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except Exception:
        return int(default)


# =========================================================
# Core Config (NiFi + Oracle)
# =========================================================
NIFI_BASE = _env("NIFI_BASE_CASHFLOW", _env("NIFI_BASE", "http://192.168.1.139:8808")).rstrip("/")
NIFI_PATH = _env("NIFI_CASHFLOW_PATH", "/cashflow/query")
NIFI_CONNECT_TIMEOUT = _env_int("NIFI_CONNECT_TIMEOUT", 10)
NIFI_READ_TIMEOUT = _env_int("NIFI_TIMEOUT", 900)

SCHEMA = _env("CASHFLOW_SCHEMA", "SCOTT") or "SCOTT"

CUSTOMER_TABLE = _env("CASHFLOW_CUSTOMER_TABLE", "AI_DW_MF_CUSTOMER_INFO") or "AI_DW_MF_CUSTOMER_INFO"
DETAILS_TABLE = _env("CASHFLOW_CUSTOMER_DETAILS_TABLE", "AI_DW_MF_CUSTOMERSDETAILS") or "AI_DW_MF_CUSTOMERSDETAILS"

CUSTOMER_ID_COL = _env("CASHFLOW_CUSTOMER_ID_COL", "CUSTOMER_ID") or "CUSTOMER_ID"
CUSTOMER_NAME_COL = _env("CASHFLOW_CUSTOMER_NAME_COL", "NAME_EN") or "NAME_EN"
BRANCH_COL = _env("CASHFLOW_BRANCH_COL", "BRANCHNAME") or "BRANCHNAME"
DISTRICT_COL = _env("CASHFLOW_DISTRICT_COL", "DISTRICT") or "DISTRICT"
CUSTOMER_INSERT_COL = _env("CASHFLOW_CUSTOMER_INSERT_COL", "INSERTDATE") or "INSERTDATE"

DETAILS_INSERT_COL = _env("CASHFLOW_DETAILS_INSERT_COL", "INSERT_DATE") or "INSERT_DATE"
DETAILS_INSERT_FMT = _env("CASHFLOW_DETAILS_INSERT_FMT", "YYYY-MM-DD HH24:MI:SS") or "YYYY-MM-DD HH24:MI:SS"
DETAILS_STATUS_COL = _env("CASHFLOW_DETAILS_STATUS_COL", "CUSTOMERSTATUS") or "CUSTOMERSTATUS"

ACTIVE_VALUES_CSV = _env("CASHFLOW_ACTIVE_VALUES", "ACTIVE,RUNNING,OPEN") or "ACTIVE,RUNNING,OPEN"
INACTIVE_VALUES_CSV = _env("CASHFLOW_INACTIVE_VALUES", "INACTIVE,CLOSED,BLOCKED") or "INACTIVE,CLOSED,BLOCKED"

DEFAULT_LIMIT = _env_int("CASHFLOW_DEFAULT_LIMIT", 200)
MAX_LIMIT = _env_int("CASHFLOW_MAX_LIMIT", 50000)

# =========================================================
# SQLCoder / Ollama
# =========================================================
OLLAMA_URL = _env("OLLAMA_URL", "http://ollama.rag.svc.cluster.local:11434").rstrip("/")
SQLCODER_MODEL = _env("SQLCODER_MODEL", "sqlcoder-7b:q4km")
SQLCODER_CONNECT_TIMEOUT = _env_int("SQLCODER_CONNECT_TIMEOUT", 10)
SQLCODER_READ_TIMEOUT = _env_int("SQLCODER_TIMEOUT", 120)

# =========================================================
# Qdrant schema assist (dw_cashflow_auto)
#   Use /points/scroll ONLY (no vector search) -> avoids 400 errors.
# =========================================================
QDRANT_URL = _env("QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")
QDRANT_API_KEY = _env("QDRANT_API_KEY", "")
QDRANT_SCHEMA_COLLECTION = _env("QDRANT_SCHEMA_COLLECTION", "dw_cashflow_auto")
QDRANT_SCROLL_LIMIT = _env_int("QDRANT_SCROLL_LIMIT", 256)
QDRANT_SCHEMA_CACHE_TTL_SEC = _env_int("QDRANT_SCHEMA_CACHE_TTL_SEC", 300)

_schema_cache: Dict[str, Any] = {"ts": 0, "tables": []}

# =========================================================
# Helpers
# =========================================================
def _clean(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _safe_int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        x = int(v)
    except Exception:
        x = default
    return max(lo, min(hi, x))


def _safe_parse_int(v: Any) -> int:
    try:
        s = re.sub(r"[^0-9]", "", str(v))
        return int(s) if s else 0
    except Exception:
        return 0


def _quote_sql_literal(v: str) -> str:
    return "'" + (v or "").replace("'", "''") + "'"


def _csv_list(s: str) -> List[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _sql_in_list(vals: List[str]) -> str:
    if not vals:
        return "'__NONE__'"
    return ", ".join(_quote_sql_literal(v.upper()) for v in vals)


def _sql_trim0(expr: str) -> str:
    return f"LTRIM(TRIM({expr}), '0')"


def _sql_eq_trim0(left_expr: str, right_literal_or_expr: str) -> str:
    return f"{_sql_trim0(left_expr)} = {_sql_trim0(right_literal_or_expr)}"


def _plain_bangla(message: str) -> HttpResponse:
    return HttpResponse(message, content_type="text/plain; charset=utf-8")


def _bn_num(n: int) -> str:
    trans = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
    return f"{n:,}".translate(trans)


def _nifi_execute(sql: str, request_id: str) -> JsonT:
    url = f"{NIFI_BASE}{NIFI_PATH}"
    r = requests.post(
        url,
        json={"sql": sql, "request_id": request_id},
        timeout=(NIFI_CONNECT_TIMEOUT, NIFI_READ_TIMEOUT),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    return r.json()


def _to_rows(result_json: JsonT) -> List[Dict[str, Any]]:
    if isinstance(result_json, list):
        return [r for r in result_json if isinstance(r, dict)]
    if isinstance(result_json, dict):
        for k in ("rows", "data", "result", "records"):
            v = result_json.get(k)
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return [x for x in v if isinstance(x, dict)]
        return [result_json]
    return []


# =========================================================
# Mode detection
# =========================================================
def _is_metrics_mode(body: Dict[str, Any], q: str) -> bool:
    if str(body.get("mode", "")).strip().lower() in ("metric", "metrics", "dashboard"):
        return True
    if body.get("metrics") is True:
        return True
    if re.search(r"\b(metric|metrics|kpi|dashboard)\b", (q or "").lower()):
        return True
    return False


def _is_sqlcoder_forced(body: Dict[str, Any]) -> bool:
    m = str(body.get("mode", "")).strip().lower()
    if m in ("sqlcoder", "nl2sql", "sql"):
        return True
    if body.get("use_sqlcoder") is True:
        return True
    return False


# =========================================================
# Minimal Intent (deterministic routes) - cashflow only
# =========================================================
@dataclass
class DateRange:
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    preset: Optional[str] = None  # today, this_month, last_N_days


@dataclass
class Entities:
    limit: int = DEFAULT_LIMIT
    customerid: Optional[str] = None
    branch: Optional[str] = None
    district: Optional[str] = None
    status: Optional[str] = None
    date: DateRange = field(default_factory=DateRange)  # ✅ important


@dataclass
class IntentResult:
    intent: str
    confidence: float
    route: str  # deterministic | sqlcoder
    normalized_question: str
    entities: Entities


def _extract_top_n(text: str) -> Optional[int]:
    t = (text or "").lower()
    m = re.search(r"\btop\s+(\d{1,5})\b", t)
    if m:
        return _safe_int(m.group(1), DEFAULT_LIMIT, 1, MAX_LIMIT)
    m = re.search(r"\blimit\s+(\d{1,5})\b", t)
    if m:
        return _safe_int(m.group(1), DEFAULT_LIMIT, 1, MAX_LIMIT)
    m2 = re.search(r"টপ\s*([0-9]{1,5})", text or "")
    if m2:
        return _safe_int(m2.group(1), DEFAULT_LIMIT, 1, MAX_LIMIT)
    return None


def _extract_customerid(text: str) -> Optional[str]:
    t = (text or "").strip()
    pats = [
        r"\bcustomer[_\s-]*id\s*[:=]?\s*([0-9]{3,20})\b",
        r"\bcustomerid\s*[:=]?\s*([0-9]{3,20})\b",
        r"\bcust[_\s-]*id\s*[:=]?\s*([0-9]{3,20})\b",
        r"(?:কাস্টমার|গ্রাহক)\s*আইডি\s*[:=]?\s*([0-9]{3,20})",
    ]
    last = None
    for p in pats:
        for m in re.finditer(p, t, flags=re.IGNORECASE):
            last = m.group(1)
    if not last:
        nums = re.findall(r"\b[0-9]{3,20}\b", t)
        if len(nums) == 1:
            last = nums[0]
    return last


def _normalize_place_value(val: str) -> Optional[str]:
    if not val:
        return None
    v = val.strip()
    # remove leading junk like "like dhaka", "district dhaka"
    v = re.sub(r"^\s*(like|district|branch)\s+", "", v, flags=re.IGNORECASE).strip()
    # cut on connectors that caused your bug: "and customer status"
    v = re.split(r"\b(and|with|including|status|customer|details)\b", v, flags=re.IGNORECASE)[0].strip()
    # stop at punctuation
    v = re.split(r"[,\.;\|/]", v)[0].strip()
    return v[:80] if v else None


def _extract_value_after_keyword(text: str, keyword: str, bangla_keyword: Optional[str] = None) -> Optional[str]:
    s = (text or "").strip()
    keys = [keyword] + ([bangla_keyword] if bangla_keyword else [])
    for k in keys:
        m = re.search(rf"\b{re.escape(k)}\b\s*[:=\-]?\s*([A-Za-z0-9_ .()]+)", s, flags=re.IGNORECASE)
        if m:
            return _normalize_place_value(m.group(1) or "")
    return None


def _extract_district_from_in(text: str) -> Optional[str]:
    """
    Handles:
      - "customers in barisal"
      - "customers from narayanganj"
      - "customer list in Dhaka district"
    """
    s = (text or "").strip()
    m = re.search(r"\b(customers?|customer)\b.*\b(in|from)\s+([A-Za-z][A-Za-z0-9 _-]{2,40})", s, flags=re.IGNORECASE)
    if m:
        return _normalize_place_value(m.group(3) or "")
    # Bangla: "বরিশালে গ্রাহক", "ঢাকা জেলার গ্রাহক"
    m2 = re.search(r"([^\s]{2,20})\s*(জেলা|জেলার)\s*(গ্রাহক|কাস্টমার)", s)
    if m2:
        return _normalize_place_value(m2.group(1) or "")
    return None


def _parse_status(q: str) -> Optional[str]:
    s = (q or "").lower()
    if re.search(r"\bactive\b", s):
        return "active"
    if re.search(r"\b(inactive|closed|blocked)\b", s):
        return "inactive"
    if "সক্রিয়" in q or "সক্রীয়" in q or "সক্রিয়" in q:
        return "active"
    if "নিষ্ক্রিয়" in q or "বন্ধ" in q or "ব্লক" in q:
        return "inactive"
    return None


def _parse_date_range(q: str) -> DateRange:
    s = (q or "").strip().lower()

    if "today" in s or "আজ" in q:
        return DateRange(preset="today")
    if "this month" in s or "current month" in s or "এই মাস" in q or "চলতি মাস" in q:
        return DateRange(preset="this_month")

    m = re.search(r"\blast\s+(\d{1,3})\s+days\b", s)
    if m:
        d = _safe_int(m.group(1), 7, 1, 365)
        return DateRange(preset=f"last_{d}_days")
    m2 = re.search(r"গত\s*([0-9]{1,3})\s*দিন", q)
    if m2:
        d = _safe_int(m2.group(1), 7, 1, 365)
        return DateRange(preset=f"last_{d}_days")

    m3 = re.search(r"\bfrom\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\s+to\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\b", s)
    if m3:
        return DateRange(date_from=m3.group(1), date_to=m3.group(2))

    m4 = re.search(r"([0-9]{4}-[0-9]{2}-[0-9]{2})\s*থেকে\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", q)
    if m4:
        return DateRange(date_from=m4.group(1), date_to=m4.group(2))

    return DateRange()


def _is_count_customers(q: str) -> bool:
    s = (q or "").lower()
    if "details" in s:
        return False
    if "মোট গ্রাহক" in q or "গ্রাহক কত" in q or "কাস্টমার কত" in q:
        return True
    return bool(re.search(r"\b(how many|count|total|number of)\s+customers?\b", s))


def _is_count_customer_details(q: str) -> bool:
    s = (q or "").lower()
    if ("ডিটেইল" in q or "ডিটেল" in q) and ("কত" in q or "সংখ্যা" in q or "মোট" in q):
        return True
    return bool(re.search(r"\b(how many|count|total|number of)\b", s) and "customer" in s and "details" in s)


def _is_last_details_date(q: str) -> bool:
    s = (q or "").lower()
    has_details = ("customer" in s and "details" in s) or ("ডিটেইল" in q or "ডিটেল" in q)
    if not has_details:
        return False
    if re.search(r"\b(last|latest|most recent|recent)\b", s) and re.search(r"\b(date|datetime|time|record|insert)\b", s):
        return True
    if ("সর্বশেষ" in q or "শেষ" in q or "লেটেস্ট" in q) and ("তারিখ" in q or "সময়" in q or "টাইম" in q or "রেকর্ড" in q):
        return True
    return False


def _is_list_customers(q: str) -> bool:
    s = (q or "").lower()
    if ("তালিকা" in q or "লিস্ট" in q or "দেখাও" in q or "দাও" in q) and ("গ্রাহক" in q or "কাস্টমার" in q):
        if "ডিটেইল" not in q and "ডিটেল" not in q:
            return True
    return bool(re.search(r"\b(list|show)\b", s) and "customer" in s and "details" not in s)


def _is_list_customer_details(q: str) -> bool:
    s = (q or "").lower()
    if ("তালিকা" in q or "লিস্ট" in q or "দেখাও" in q or "দাও" in q) and ("ডিটেইল" in q or "ডিটেল" in q):
        return True
    return bool(re.search(r"\b(list|show)\b", s) and "customer" in s and "details" in s)


def _classify_cashflow(q: str) -> IntentResult:
    nq = _clean(q)
    limit = _extract_top_n(nq) or DEFAULT_LIMIT
    limit = _safe_int(limit, DEFAULT_LIMIT, 1, MAX_LIMIT)

    e = Entities()
    e.limit = limit
    e.customerid = _extract_customerid(nq)
    e.branch = _extract_value_after_keyword(nq, "branch", "ব্রাঞ্চ")
    e.district = _extract_value_after_keyword(nq, "district", "জেলা")
    # ✅ catch "customers in barisal"
    if not e.district:
        e.district = _extract_district_from_in(nq)

    e.status = _parse_status(nq)
    e.date = _parse_date_range(nq)

    # deterministic intents
    if _is_count_customer_details(nq):
        return IntentResult("count_customer_details", 0.95, "deterministic", nq, e)
    if _is_count_customers(nq):
        return IntentResult("count_customers", 0.95, "deterministic", nq, e)
    if _is_last_details_date(nq):
        e.limit = 1
        return IntentResult("last_details_date", 0.95, "deterministic", nq, e)

    if e.customerid and (("details" in nq.lower()) or ("ডিটেইল" in nq) or ("ডিটেল" in nq)):
        if re.search(r"\b(latest|last|most recent|recent)\b", nq.lower()) or ("সর্বশেষ" in nq or "শেষ" in nq or "লেটেস্ট" in nq):
            return IntentResult("latest_customer_details_by_id", 0.92, "deterministic", nq, e)
        return IntentResult("customer_details_by_id", 0.92, "deterministic", nq, e)

    if e.customerid and (("customer info" in nq.lower()) or ("গ্রাহক তথ্য" in nq) or ("কাস্টমার ইনফো" in nq) or ("information" in nq.lower())):
        return IntentResult("customer_info_by_id", 0.92, "deterministic", nq, e)

    # ✅ “customers in barisal / customers from dhaka” -> deterministic list_customers
    if _is_list_customers(nq) or (("customer" in nq.lower() or "customers" in nq.lower()) and (e.district or e.branch)):
        return IntentResult("list_customers", 0.85, "deterministic", nq, e)

    if _is_list_customer_details(nq):
        return IntentResult("list_customer_details", 0.80, "deterministic", nq, e)

    return IntentResult("unsupported", 0.00, "sqlcoder", nq, e)


# =========================================================
# SQL filter helpers (Oracle 11g)
# =========================================================
def _details_insert_dt_expr(alias: str = "d") -> str:
    col = f"{alias}.{DETAILS_INSERT_COL}"
    fmt = DETAILS_INSERT_FMT.replace("'", "''")
    return f"TO_DATE({col}, '{fmt}')"


def _apply_details_date_filter(where: List[str], alias: str, dr: DateRange) -> None:
    dt = _details_insert_dt_expr(alias)

    if dr.date_from and dr.date_to:
        where.append(
            f"{dt} BETWEEN TO_DATE('{dr.date_from}', 'YYYY-MM-DD') "
            f"AND TO_DATE('{dr.date_to}', 'YYYY-MM-DD') + 0.99999"
        )
        return

    if dr.preset:
        if dr.preset == "today":
            where.append(f"TRUNC({dt}) = TRUNC(SYSDATE)")
            return
        if dr.preset == "this_month":
            where.append(f"TRUNC({dt}, 'MM') = TRUNC(SYSDATE, 'MM')")
            return
        m = re.match(r"last_([0-9]{1,3})_days", dr.preset or "")
        if m:
            days = _safe_int(m.group(1), 7, 1, 365)
            where.append(f"{dt} >= SYSDATE - {days}")
            return


def _apply_customer_geo_filter(where: List[str], alias: str, branch: Optional[str], district: Optional[str]) -> None:
    if branch:
        where.append(f"UPPER(NVL({alias}.{BRANCH_COL}, '')) LIKE '%' || UPPER({_quote_sql_literal(branch)}) || '%'")
    if district:
        where.append(f"UPPER(NVL({alias}.{DISTRICT_COL}, '')) LIKE '%' || UPPER({_quote_sql_literal(district)}) || '%'")


def _apply_status_filter(where: List[str], alias: str, status: Optional[str]) -> None:
    if not status:
        return
    active_vals = _csv_list(ACTIVE_VALUES_CSV)
    inactive_vals = _csv_list(INACTIVE_VALUES_CSV)
    if status == "active":
        where.append(f"UPPER(NVL({alias}.{DETAILS_STATUS_COL}, '')) IN ({_sql_in_list(active_vals)})")
    elif status == "inactive":
        where.append(f"UPPER(NVL({alias}.{DETAILS_STATUS_COL}, '')) IN ({_sql_in_list(inactive_vals)})")


# =========================================================
# Deterministic SQL Builders
# =========================================================
def _sql_count_customers(e: Entities) -> str:
    c, d = "c", "d"
    c_t = f"{SCHEMA}.{CUSTOMER_TABLE}"
    d_t = f"{SCHEMA}.{DETAILS_TABLE}"

    need_details = bool(e.status or e.date.date_from or e.date.date_to or e.date.preset)
    where: List[str] = []

    if need_details:
        _apply_status_filter(where, d, e.status)
        _apply_details_date_filter(where, d, e.date)

    _apply_customer_geo_filter(where, c, e.branch, e.district)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    if need_details:
        join_on = _sql_eq_trim0(f"{d}.CUSTOMERID", f"{c}.{CUSTOMER_ID_COL}")
        return f"""
SELECT
  COUNT(DISTINCT {c}.{CUSTOMER_ID_COL}) AS TOTAL_CUSTOMERS,
  TO_CHAR(SYSDATE,'YYYY-MM-DD') AS REPORT_DATE
FROM {c_t} {c}
JOIN {d_t} {d}
  ON {join_on}
{where_sql}
""".strip()

    return f"""
SELECT
  COUNT(1) AS TOTAL_CUSTOMERS,
  TO_CHAR(SYSDATE,'YYYY-MM-DD') AS REPORT_DATE
FROM {c_t} {c}
{where_sql}
""".strip()


def _sql_count_customer_details(e: Entities) -> str:
    d, c = "d", "c"
    d_t = f"{SCHEMA}.{DETAILS_TABLE}"
    c_t = f"{SCHEMA}.{CUSTOMER_TABLE}"

    where: List[str] = []
    _apply_status_filter(where, d, e.status)
    _apply_details_date_filter(where, d, e.date)

    join_customer = bool(e.branch or e.district)
    if join_customer:
        join_on = _sql_eq_trim0(f"{d}.CUSTOMERID", f"{c}.{CUSTOMER_ID_COL}")
        _apply_customer_geo_filter(where, c, e.branch, e.district)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        return f"""
SELECT
  COUNT(1) AS TOTAL_CUSTOMER_DETAILS,
  TO_CHAR(SYSDATE,'YYYY-MM-DD') AS REPORT_DATE
FROM {d_t} {d}
JOIN {c_t} {c}
  ON {join_on}
{where_sql}
""".strip()

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return f"""
SELECT
  COUNT(1) AS TOTAL_CUSTOMER_DETAILS,
  TO_CHAR(SYSDATE,'YYYY-MM-DD') AS REPORT_DATE
FROM {d_t} {d}
{where_sql}
""".strip()


def _sql_last_details_date(e: Entities) -> str:
    d, c = "d", "c"
    d_t = f"{SCHEMA}.{DETAILS_TABLE}"
    c_t = f"{SCHEMA}.{CUSTOMER_TABLE}"
    fmt = DETAILS_INSERT_FMT.replace("'", "''")
    dt_expr = _details_insert_dt_expr(d)

    where: List[str] = []
    _apply_status_filter(where, d, e.status)
    _apply_details_date_filter(where, d, e.date)

    join_customer = bool(e.branch or e.district)
    if join_customer:
        join_on = _sql_eq_trim0(f"{d}.CUSTOMERID", f"{c}.{CUSTOMER_ID_COL}")
        _apply_customer_geo_filter(where, c, e.branch, e.district)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        return f"""
SELECT
  TO_CHAR(MAX({dt_expr}), '{fmt}') AS LAST_RECORD_DATETIME,
  TO_CHAR(SYSDATE,'YYYY-MM-DD') AS REPORT_DATE
FROM {d_t} {d}
JOIN {c_t} {c}
  ON {join_on}
{where_sql}
""".strip()

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return f"""
SELECT
  TO_CHAR(MAX({dt_expr}), '{fmt}') AS LAST_RECORD_DATETIME,
  TO_CHAR(SYSDATE,'YYYY-MM-DD') AS REPORT_DATE
FROM {d_t} {d}
{where_sql}
""".strip()


def _sql_list_customers(e: Entities) -> str:
    c, d = "c", "d"
    c_t = f"{SCHEMA}.{CUSTOMER_TABLE}"
    d_t = f"{SCHEMA}.{DETAILS_TABLE}"

    need_details = bool(e.status or e.date.date_from or e.date.date_to or e.date.preset)
    where: List[str] = []
    _apply_customer_geo_filter(where, c, e.branch, e.district)

    if need_details:
        _apply_status_filter(where, d, e.status)
        _apply_details_date_filter(where, d, e.date)
        join_on = _sql_eq_trim0(f"{d}.CUSTOMERID", f"{c}.{CUSTOMER_ID_COL}")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        return f"""
SELECT *
FROM (
  SELECT DISTINCT
    {c}.{CUSTOMER_ID_COL} AS CUSTOMERID,
    {c}.{CUSTOMER_NAME_COL} AS CUSTOMER_NAME,
    {c}.{BRANCH_COL} AS BRANCHNAME,
    {c}.{DISTRICT_COL} AS DISTRICT,
    {c}.{CUSTOMER_INSERT_COL} AS INSERT_DATE
  FROM {c_t} {c}
  JOIN {d_t} {d}
    ON {join_on}
  {where_sql}
)
WHERE ROWNUM <= {int(e.limit)}
""".strip()

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return f"""
SELECT *
FROM (
  SELECT
    {c}.{CUSTOMER_ID_COL} AS CUSTOMERID,
    {c}.{CUSTOMER_NAME_COL} AS CUSTOMER_NAME,
    {c}.{BRANCH_COL} AS BRANCHNAME,
    {c}.{DISTRICT_COL} AS DISTRICT,
    {c}.{CUSTOMER_INSERT_COL} AS INSERT_DATE
  FROM {c_t} {c}
  {where_sql}
)
WHERE ROWNUM <= {int(e.limit)}
""".strip()


def _sql_list_customer_details(e: Entities) -> str:
    d = "d"
    d_t = f"{SCHEMA}.{DETAILS_TABLE}"
    fmt = DETAILS_INSERT_FMT.replace("'", "''")
    dt_expr = _details_insert_dt_expr(d)

    where: List[str] = []
    _apply_status_filter(where, d, e.status)
    _apply_details_date_filter(where, d, e.date)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return f"""
SELECT *
FROM (
  SELECT
    d.CUSTOMERID AS CUSTOMERID,
    d.{DETAILS_INSERT_COL} AS INSERT_DATE,
    TO_CHAR({dt_expr}, '{fmt}') AS INSERT_DATE_PARSED,
    d.*
  FROM {d_t} d
  {where_sql}
  ORDER BY {dt_expr} DESC
)
WHERE ROWNUM <= {int(e.limit)}
""".strip()


def _sql_customer_info_by_id(customerid: str, limit: int) -> str:
    c_t = f"{SCHEMA}.{CUSTOMER_TABLE}"
    lit = _quote_sql_literal(customerid)
    return f"""
SELECT *
FROM (
  SELECT
    c.{CUSTOMER_ID_COL} AS CUSTOMERID,
    c.{CUSTOMER_NAME_COL} AS CUSTOMER_NAME,
    c.{BRANCH_COL} AS BRANCHNAME,
    c.{DISTRICT_COL} AS DISTRICT,
    c.{CUSTOMER_INSERT_COL} AS INSERT_DATE,
    c.*
  FROM {c_t} c
  WHERE {_sql_eq_trim0(f'c.{CUSTOMER_ID_COL}', lit)}
)
WHERE ROWNUM <= {int(limit)}
""".strip()


def _sql_customer_details_by_id(customerid: str, limit: int, latest_first: bool) -> str:
    d_t = f"{SCHEMA}.{DETAILS_TABLE}"
    lit = _quote_sql_literal(customerid)
    fmt = DETAILS_INSERT_FMT.replace("'", "''")
    dt_expr = _details_insert_dt_expr("d")
    order = f" ORDER BY {dt_expr} DESC" if latest_first else ""
    return f"""
SELECT *
FROM (
  SELECT
    d.CUSTOMERID AS CUSTOMERID,
    d.{DETAILS_INSERT_COL} AS INSERT_DATE,
    TO_CHAR({dt_expr}, '{fmt}') AS INSERT_DATE_PARSED,
    d.*
  FROM {d_t} d
  WHERE {_sql_eq_trim0('d.CUSTOMERID', lit)}
  {order}
)
WHERE ROWNUM <= {int(limit)}
""".strip()


# =========================================================
# Qdrant schema (scroll, cached) - safe
# =========================================================
def _qdrant_headers() -> Dict[str, str]:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if QDRANT_API_KEY:
        h["api-key"] = QDRANT_API_KEY
    return h


def _qdrant_scroll_all_table_schemas() -> List[Dict[str, Any]]:
    now = time.time()
    if _schema_cache["tables"] and (now - float(_schema_cache.get("ts", 0))) < QDRANT_SCHEMA_CACHE_TTL_SEC:
        return _schema_cache["tables"]

    url = f"{QDRANT_URL}/collections/{QDRANT_SCHEMA_COLLECTION}/points/scroll"
    offset = None
    out: List[Dict[str, Any]] = []

    for _ in range(10):
        payload: Dict[str, Any] = {
            "limit": QDRANT_SCROLL_LIMIT,
            "with_payload": True,
            "with_vectors": False,
            "filter": {"must": [{"key": "doc_type", "match": {"value": "table_schema"}}]},
        }
        if offset is not None:
            payload["offset"] = offset

        r = requests.post(url, json=payload, headers=_qdrant_headers(), timeout=(5, 20))
        r.raise_for_status()
        data = r.json() or {}
        result = (data.get("result") or {})
        pts = result.get("points") or []
        for p in pts:
            pl = p.get("payload") or {}
            if isinstance(pl, dict) and (pl.get("doc_type") == "table_schema"):
                out.append(pl)

        offset = result.get("next_page_offset")
        if not offset:
            break

    _schema_cache["ts"] = now
    _schema_cache["tables"] = out
    return out


def _pick_relevant_schema(question: str, max_tables: int = 10) -> Tuple[List[str], str]:
    q = (question or "").lower()
    docs = _qdrant_scroll_all_table_schemas()

    scored: List[Tuple[int, Dict[str, Any]]] = []
    kws = re.findall(r"[a-z0-9_]{3,}", q)

    for pl in docs:
        tname = str(pl.get("table", "")).upper()
        cols = str(pl.get("cols_csv", "")).upper()
        txt = str(pl.get("text", "")).upper()

        score = 0
        for kw in kws:
            kwu = kw.upper()
            if kwu in tname:
                score += 5
            if kwu in cols:
                score += 2
            if kwu in txt:
                score += 1

        # boost likely fields
        if "MOBILE" in cols and ("mobile" in q or "মোবাইল" in question or "phone" in q):
            score += 8
        if "DISTRICT" in cols and ("district" in q or "জেলা" in question or "barisal" in q or "dhaka" in q):
            score += 6

        # always include base customer tables
        if tname in (CUSTOMER_TABLE.upper(), DETAILS_TABLE.upper()):
            score += 50

        if score > 0:
            scored.append((score, pl))

    scored.sort(key=lambda x: x[0], reverse=True)
    picked = [pl for _, pl in scored[:max_tables]]

    tables: List[str] = []
    hints: List[str] = []
    for pl in picked:
        sch = str(pl.get("schema", SCHEMA)).upper() or SCHEMA.upper()
        tbl = str(pl.get("table", "")).upper()
        cols = str(pl.get("cols_csv", "")).upper()
        if not tbl or sch != SCHEMA.upper():
            continue

        tables.append(tbl)
        if cols:
            cc = ",".join([c.strip() for c in cols.split(",") if c.strip()][:30])
            hints.append(f"{SCHEMA}.{tbl}({cc})")
        else:
            hints.append(f"{SCHEMA}.{tbl}")

    # dedupe preserve order
    seen = set()
    tables2: List[str] = []
    for t in tables:
        if t not in seen:
            tables2.append(t)
            seen.add(t)

    hint_str = " | ".join(hints[:max_tables])
    return tables2, hint_str


# =========================================================
# SQLCoder (Oracle 11g) - safety + auto-fix
# =========================================================
_SQL_BLOCKED = re.compile(r"\b(insert|update|delete|merge|drop|alter|truncate|create|grant|revoke|commit|rollback)\b", re.IGNORECASE)
_BAD_ORACLE = re.compile(r"\b(dbms_|utl_|xmltype|httpuri|sys\.)\b", re.IGNORECASE)
_BAD_SYNTAX = re.compile(r"\b(ILIKE|LIMIT|FETCH\s+FIRST|OFFSET)\b", re.IGNORECASE)

# ✅ Block the exact problem you showed: SCOTT.TABLE(...)
_TABLE_AS_FUNCTION = re.compile(rf"\b{re.escape(SCHEMA.upper())}\.[A-Z0-9_]+\s*\(", re.IGNORECASE)


def _strip_sql_noise(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _ensure_rownum_limit(sql: str, limit: int) -> str:
    up = sql.upper()
    if "ROWNUM" in up:
        return sql
    return f"""
SELECT *
FROM (
{sql}
)
WHERE ROWNUM <= {int(limit)}
""".strip()


def _oracleize_like(sql: str) -> str:
    s = sql
    s = re.sub(r"\bILIKE\b", "LIKE", s, flags=re.IGNORECASE)

    # Convert: col LIKE '%text%'  => UPPER(NVL(col,'')) LIKE '%'||UPPER('text')||'%'
    def repl_any(m: re.Match) -> str:
        col = m.group(1)
        pat = m.group(2) or ""
        pat = pat.replace("'", "''")
        # remove leading/trailing % for the UPPER('x') part
        core = pat.strip("%")
        core = core.replace("'", "''")
        return f"UPPER(NVL({col}, '')) LIKE '%' || UPPER('{core}') || '%'"

    # alias.col LIKE '...'
    s = re.sub(r"(\b[A-Z0-9_]+\.[A-Z0-9_]+\b)\s+LIKE\s+'([^']+)'", repl_any, s, flags=re.IGNORECASE)
    # col LIKE '...'
    s = re.sub(r"(\b[A-Z0-9_]+\b)\s+LIKE\s+'([^']+)'", repl_any, s, flags=re.IGNORECASE)

    return s


def _ensure_safe_aggregate_aliases(sql: str) -> str:
    s = sql
    s = re.sub(r"(?is)\bSELECT\s+COUNT\s*\(\s*\*\s*\)\s+FROM\b", "SELECT COUNT(*) AS COUNT_1 FROM", s)
    s = re.sub(r"(?is)\bSELECT\s+SUM\s*\(\s*([A-Z0-9_\.]+)\s*\)\s+FROM\b", r"SELECT SUM(\1) AS SUM_1 FROM", s)
    s = re.sub(r"(?is)\bSELECT\s+AVG\s*\(\s*([A-Z0-9_\.]+)\s*\)\s+FROM\b", r"SELECT AVG(\1) AS AVG_1 FROM", s)
    s = re.sub(r"(?is)\bSELECT\s+MIN\s*\(\s*([A-Z0-9_\.]+)\s*\)\s+FROM\b", r"SELECT MIN(\1) AS MIN_1 FROM", s)
    s = re.sub(r"(?is)\bSELECT\s+MAX\s*\(\s*([A-Z0-9_\.]+)\s*\)\s+FROM\b", r"SELECT MAX(\1) AS MAX_1 FROM", s)
    return s


def _validate_sqlcoder_sql(sql: str, used_tables: List[str]) -> Optional[str]:
    s = _strip_sql_noise(sql)
    if not s:
        return "SQLCoder returned empty SQL."
    if ";" in s:
        return "Semicolon not allowed."
    if re.search(r"(--|/\*)", s):
        return "Comments not allowed."
    if not re.match(r"^\s*(select|with)\b", s, flags=re.IGNORECASE):
        return "Only SELECT/WITH queries are allowed."
    if _SQL_BLOCKED.search(s):
        return "DML/DDL keywords are not allowed."
    if _BAD_ORACLE.search(s):
        return "Dangerous Oracle packages/functions are not allowed."
    if _BAD_SYNTAX.search(s):
        return "Non-Oracle syntax detected (ILIKE/LIMIT/FETCH/OFFSET)."
    if _TABLE_AS_FUNCTION.search(s):
        return "Table used like a function (SCOTT.TABLE(...)) is not allowed."

    up = s.upper()
    schema_up = SCHEMA.upper()

    if f"{schema_up}." not in up:
        return f"SQL must reference schema {schema_up} with schema-qualified names."

    for sch, _tbl in re.findall(r"\b([A-Z0-9_]+)\.([A-Z0-9_]+)\b", up):
        if sch != schema_up:
            return f"Other schema {sch} is not allowed."

    # Soft enforcement: must reference at least one relevant table we hinted
    if used_tables:
        ok = any(f"{schema_up}.{t.upper()}" in up for t in used_tables)
        if not ok:
            return "SQL does not reference expected cashflow tables from schema catalog."

    return None


def _ollama_generate_sqlcoder(question: str, limit: int) -> Tuple[str, List[str], str]:
    used_tables, schema_hint = _pick_relevant_schema(question, max_tables=10)

    # Always include these for stability
    for t in [CUSTOMER_TABLE.upper(), DETAILS_TABLE.upper()]:
        if t not in used_tables:
            used_tables.insert(0, t)
    used_tables = used_tables[:12]

    allowed_tables_hint = schema_hint or " | ".join([f"{SCHEMA}.{t}" for t in used_tables])
    url = f"{OLLAMA_URL}/api/generate"

    prompt = f"""
You are SQLCoder for Oracle 11g.
Return ONLY ONE SQL query. No explanation. No markdown.

STRICT ORACLE 11g RULES:
- ONLY SELECT or WITH ... SELECT
- DO NOT use: ILIKE, LIMIT, OFFSET, FETCH FIRST
- NEVER call tables like functions. ❌ Do NOT write: {SCHEMA}.TABLE(...)
- Use case-insensitive search as:
  UPPER(NVL(col,'')) LIKE '%' || UPPER('text') || '%'
- Always use schema-qualified names: {SCHEMA}.TABLE
- Use ROWNUM for limiting (Oracle 11g):
  ... WHERE ROWNUM <= N  OR wrap with SELECT * FROM (...) WHERE ROWNUM <= N
- Always alias aggregates (ExecuteSQLRecord-safe):
  COUNT(*) AS COUNT_1, SUM(x) AS SUM_1, AVG(x) AS AVG_1

CASHFLOW SCHEMA CATALOG (most relevant tables):
{allowed_tables_hint}

Default limit: {limit}

User request:
{question}

Return only the SQL:
""".strip()

    payload = {"model": SQLCODER_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0}}

    r = requests.post(
        url,
        json=payload,
        timeout=(SQLCODER_CONNECT_TIMEOUT, SQLCODER_READ_TIMEOUT),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    data = r.json() or {}
    sql = _strip_sql_noise(_clean(data.get("response")))
    return sql, used_tables, schema_hint


def _finalize_sqlcoder_sql(sql: str, limit: int) -> str:
    s = _strip_sql_noise(sql)
    s = _oracleize_like(s)
    s = _ensure_safe_aggregate_aliases(s)
    s = _ensure_rownum_limit(s, limit)
    s = _strip_sql_noise(s)
    s = re.sub(r"\s*;\s*$", "", s)
    return s.strip()


# =========================================================
# Friendly short message
# =========================================================
def _friendly_message(intent: str, rows: List[Dict[str, Any]], e: Entities) -> str:
    n = len(rows)
    if intent == "list_customers":
        if e.district:
            return f"Found {n} customers for district '{e.district}'."
        if e.branch:
            return f"Found {n} customers for branch '{e.branch}'."
        return f"Found {n} customers."
    if intent == "list_customer_details":
        return f"Found {n} customer detail records."
    if intent == "customer_info_by_id":
        return f"Customer info loaded (rows: {n})."
    if intent in ("customer_details_by_id", "latest_customer_details_by_id"):
        return f"Customer details loaded (rows: {n})."
    if intent.startswith("count_"):
        return "Count calculated from Oracle."
    return f"Found {n} rows."


# =========================================================
# Endpoint
# =========================================================
@csrf_exempt
@require_POST
def cashflow_query(request):
    t0 = time.time()
    request_id = (request.headers.get("X-Request-Id") or str(uuid.uuid4()))[:36]

    try:
        body = json.loads((request.body or b"{}").decode("utf-8", errors="replace"))
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}

    q = _clean(body.get("q") or body.get("question") or body.get("message"))
    if not q:
        return JsonResponse({"error": "Missing q", "request_id": request_id, "build": BUILD}, status=400)

    metrics_mode = _is_metrics_mode(body, q)
    force_sqlcoder = _is_sqlcoder_forced(body)

    ir = _classify_cashflow(q)
    e = ir.entities
    e.limit = _safe_int(e.limit, DEFAULT_LIMIT, 1, MAX_LIMIT)

    filters = {
        "branch": e.branch,
        "district": e.district,
        "status": e.status,
        "date_from": e.date.date_from,
        "date_to": e.date.date_to,
        "date_preset": e.date.preset,
    }

    log.info(
        "cashflow_query request_id=%s intent=%s conf=%.2f route=%s forced_sqlcoder=%s cust=%s limit=%s build=%s q=%r",
        request_id, ir.intent, ir.confidence, ir.route, force_sqlcoder, e.customerid, e.limit, BUILD, ir.normalized_question
    )

    try:
        # ----------------------------
        # Deterministic first
        # ----------------------------
        if not force_sqlcoder and ir.route == "deterministic":
            if ir.intent == "count_customers":
                sql = _sql_count_customers(e)
                rows = _to_rows(_nifi_execute(sql, request_id))
                total = _safe_parse_int(rows[0].get("TOTAL_CUSTOMERS") if rows else 0)

                if metrics_mode:
                    return JsonResponse(
                        {
                            "metric": "total_customers",
                            "report_date": time.strftime("%Y-%m-%d"),
                            "value": total,
                            "filters": filters,
                            "summary": f"Total customers: {total:,}.",
                            "request_id": request_id,
                            "build": BUILD,
                        },
                        json_dumps_params={"ensure_ascii": False},
                    )

                # short Bangla style output
                return _plain_bangla(
                    f"আসসালামু আলাইকুম।\nমোট গ্রাহক: {_bn_num(total)} জন।\n(রিপোর্ট তারিখ: {time.strftime('%Y-%m-%d')})"
                )

            if ir.intent == "count_customer_details":
                sql = _sql_count_customer_details(e)
                rows = _to_rows(_nifi_execute(sql, request_id))
                total = _safe_parse_int(rows[0].get("TOTAL_CUSTOMER_DETAILS") if rows else 0)

                if metrics_mode:
                    return JsonResponse(
                        {
                            "metric": "total_customer_details_records",
                            "report_date": time.strftime("%Y-%m-%d"),
                            "value": total,
                            "filters": filters,
                            "summary": f"Customer details records: {total:,}.",
                            "request_id": request_id,
                            "build": BUILD,
                        },
                        json_dumps_params={"ensure_ascii": False},
                    )

                return _plain_bangla(
                    f"আসসালামু আলাইকুম।\nCustomer Details মোট রেকর্ড: {_bn_num(total)} টি।\n(রিপোর্ট তারিখ: {time.strftime('%Y-%m-%d')})"
                )

            if ir.intent == "last_details_date":
                sql = _sql_last_details_date(e)
                rows = _to_rows(_nifi_execute(sql, request_id))
                last_dt = _clean(rows[0].get("LAST_RECORD_DATETIME") if rows else "")
                msg = last_dt or "N/A"
                if metrics_mode:
                    return JsonResponse(
                        {
                            "metric": "latest_customer_details_record_datetime",
                            "report_date": time.strftime("%Y-%m-%d"),
                            "value": msg,
                            "filters": filters,
                            "summary": f"Latest record datetime: {msg}.",
                            "request_id": request_id,
                            "build": BUILD,
                        },
                        json_dumps_params={"ensure_ascii": False},
                    )
                return _plain_bangla(
                    f"Customer Details সর্বশেষ রেকর্ড সময়: {msg}\n(রিপোর্ট তারিখ: {time.strftime('%Y-%m-%d')})"
                )

            if ir.intent == "list_customers":
                sql = _sql_list_customers(e)
                rows = _to_rows(_nifi_execute(sql, request_id))
                return JsonResponse(
                    {
                        "rows": rows,
                        "count": len(rows),
                        "message": _friendly_message(ir.intent, rows, e),
                        "filters": filters,
                        "request_id": request_id,
                        "build": BUILD,
                    },
                    json_dumps_params={"ensure_ascii": False},
                )

            if ir.intent == "list_customer_details":
                sql = _sql_list_customer_details(e)
                rows = _to_rows(_nifi_execute(sql, request_id))
                return JsonResponse(
                    {
                        "rows": rows,
                        "count": len(rows),
                        "message": _friendly_message(ir.intent, rows, e),
                        "filters": filters,
                        "request_id": request_id,
                        "build": BUILD,
                    },
                    json_dumps_params={"ensure_ascii": False},
                )

            if ir.intent == "customer_info_by_id":
                if not e.customerid:
                    return JsonResponse({"error": "customerid required", "request_id": request_id, "build": BUILD}, status=400)
                sql = _sql_customer_info_by_id(e.customerid, e.limit)
                rows = _to_rows(_nifi_execute(sql, request_id))
                return JsonResponse(
                    {
                        "rows": rows,
                        "count": len(rows),
                        "message": _friendly_message(ir.intent, rows, e),
                        "filters": filters,
                        "request_id": request_id,
                        "build": BUILD,
                    },
                    json_dumps_params={"ensure_ascii": False},
                )

            if ir.intent in ("customer_details_by_id", "latest_customer_details_by_id"):
                if not e.customerid:
                    return JsonResponse({"error": "customerid required", "request_id": request_id, "build": BUILD}, status=400)
                latest_first = (ir.intent == "latest_customer_details_by_id")
                sql = _sql_customer_details_by_id(e.customerid, e.limit, latest_first=latest_first)
                rows = _to_rows(_nifi_execute(sql, request_id))
                return JsonResponse(
                    {
                        "rows": rows,
                        "count": len(rows),
                        "message": _friendly_message(ir.intent, rows, e),
                        "filters": filters,
                        "request_id": request_id,
                        "build": BUILD,
                    },
                    json_dumps_params={"ensure_ascii": False},
                )

            ir.route = "sqlcoder"

        # =====================================================
        # SQLCODER FALLBACK (unsupported OR forced)
        # =====================================================
        limit = _safe_int(e.limit or DEFAULT_LIMIT, DEFAULT_LIMIT, 1, MAX_LIMIT)

        raw_sql, used_tables, schema_hint = _ollama_generate_sqlcoder(ir.normalized_question, limit=limit)
        sql = _finalize_sqlcoder_sql(raw_sql, limit=limit)

        err = _validate_sqlcoder_sql(sql, used_tables=used_tables)
        if err:
            return JsonResponse(
                {
                    "error": "SQLCoder produced unsafe/invalid SQL",
                    "details": err,
                    "raw_sql": raw_sql,
                    "final_sql": sql,
                    "model": SQLCODER_MODEL,
                    "request_id": request_id,
                    "build": BUILD,
                    "schema_collection": QDRANT_SCHEMA_COLLECTION,
                    "used_tables": used_tables,
                    "schema_hint": schema_hint,
                },
                status=400,
                json_dumps_params={"ensure_ascii": False},
            )

        rows = _to_rows(_nifi_execute(sql, request_id))

        return JsonResponse(
            {
                "mode": "sqlcoder",
                "intent": ir.intent,
                "confidence": ir.confidence,
                "model": SQLCODER_MODEL,
                "sql": sql,
                "rows": rows,
                "count": len(rows),
                "message": _friendly_message("sqlcoder", rows, e),
                "filters": filters,
                "request_id": request_id,
                "build": BUILD,
                "schema_collection": QDRANT_SCHEMA_COLLECTION,
                "used_tables": used_tables,
            },
            json_dumps_params={"ensure_ascii": False},
        )

    except requests.exceptions.Timeout:
        return JsonResponse({"error": "Timeout (NiFi/Ollama/Qdrant)", "request_id": request_id, "build": BUILD}, status=504)
    except requests.exceptions.RequestException as ex:
        return JsonResponse({"error": f"Request failed: {str(ex)}", "request_id": request_id, "build": BUILD}, status=502)
    except Exception as ex:
        return JsonResponse({"error": f"Unexpected error: {str(ex)}", "request_id": request_id, "build": BUILD}, status=500)
    finally:
        ms = int((time.time() - t0) * 1000)
        log.info("cashflow_query done request_id=%s ms=%s build=%s", request_id, ms, BUILD)
