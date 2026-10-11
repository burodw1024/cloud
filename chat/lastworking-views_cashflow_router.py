# views_cashflow_router.py
# ------------------------------------------------------------
# CASHFLOW Router (Oracle 11g + NiFi truth) - Upgraded + SQLCoder
#
# ✅ Existing: Bangla prompt support + Active/Inactive + Date range + Metrics Mode
# ✅ NEW: SQLCoder fallback for unsupported queries (NL -> Oracle 11g SQL)
#    - Model: sqlcoder-7b:q4km (Ollama)
#    - Strict: SELECT-only, no DML/DDL, schema-prefix required, optional table allowlist
#
# Notes:
# - Deterministic KPI routes remain primary (safer/faster).
# - SQLCoder is used for ad-hoc analytical queries you didn't hardcode yet.
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

import requests
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

JsonT = Union[Dict[str, Any], List[Any]]


# =========================================================
# Config
# =========================================================
def _env(name: str, default: str) -> str:
    return (os.getenv(name, default) or "").strip()


def _env_int(name: str, default: str) -> int:
    try:
        return int(_env(name, default))
    except Exception:
        return int(default)


# NiFi
NIFI_BASE = _env("NIFI_BASE_CASHFLOW", _env("NIFI_BASE", "http://192.168.1.139:8808")).rstrip("/")
NIFI_PATH = _env("NIFI_CASHFLOW_PATH", "/cashflow/query")
NIFI_CONNECT_TIMEOUT = _env_int("NIFI_CONNECT_TIMEOUT", "10")
NIFI_READ_TIMEOUT = _env_int("NIFI_TIMEOUT", "900")

# Oracle schema
SCHEMA = _env("CASHFLOW_SCHEMA", "SCOTT") or "SCOTT"

# Views/Tables
CUSTOMER_TABLE = _env("CASHFLOW_CUSTOMER_TABLE", "AI_DW_MF_CUSTOMER_INFO") or "AI_DW_MF_CUSTOMER_INFO"
DETAILS_TABLE = _env("CASHFLOW_CUSTOMER_DETAILS_TABLE", "AI_DW_MF_CUSTOMERSDETAILS") or "AI_DW_MF_CUSTOMERSDETAILS"

# Customer table columns
CUSTOMER_ID_COL = _env("CASHFLOW_CUSTOMER_ID_COL", "CUSTOMER_ID") or "CUSTOMER_ID"
CUSTOMER_NAME_COL = _env("CASHFLOW_CUSTOMER_NAME_COL", "NAME_EN") or "NAME_EN"
BRANCH_COL = _env("CASHFLOW_BRANCH_COL", "BRANCHNAME") or "BRANCHNAME"
DISTRICT_COL = _env("CASHFLOW_DISTRICT_COL", "DISTRICT") or "DISTRICT"
CUSTOMER_INSERT_COL = _env("CASHFLOW_CUSTOMER_INSERT_COL", "INSERTDATE") or "INSERTDATE"

# Details table columns
DETAILS_INSERT_COL = _env("CASHFLOW_DETAILS_INSERT_COL", "INSERT_DATE") or "INSERT_DATE"
DETAILS_INSERT_FMT = _env("CASHFLOW_DETAILS_INSERT_FMT", "YYYY-MM-DD HH24:MI:SS") or "YYYY-MM-DD HH24:MI:SS"
DETAILS_STATUS_COL = _env("CASHFLOW_DETAILS_STATUS_COL", "CUSTOMERSTATUS") or "CUSTOMERSTATUS"

ACTIVE_VALUES_CSV = _env("CASHFLOW_ACTIVE_VALUES", "ACTIVE,RUNNING,OPEN") or "ACTIVE,RUNNING,OPEN"
INACTIVE_VALUES_CSV = _env("CASHFLOW_INACTIVE_VALUES", "INACTIVE,CLOSED,BLOCKED") or "INACTIVE,CLOSED,BLOCKED"

DEFAULT_LIMIT = _env_int("CASHFLOW_DEFAULT_LIMIT", "200")
MAX_LIMIT = _env_int("CASHFLOW_MAX_LIMIT", "50000")

# -------- SQLCoder / Ollama config --------
OLLAMA_URL = _env("OLLAMA_URL", "http://ollama.rag.svc.cluster.local:11434").rstrip("/")
SQLCODER_MODEL = _env("SQLCODER_MODEL", "sqlcoder-7b:q4km")
SQLCODER_CONNECT_TIMEOUT = _env_int("SQLCODER_CONNECT_TIMEOUT", "10")
SQLCODER_READ_TIMEOUT = _env_int("SQLCODER_TIMEOUT", "120")

# Optional: allowlist tables (comma-separated). If empty -> allow schema-wide (still SELECT-only).
# Recommend: set to only the tables/views you want SQLCoder to use.
# Example: AI_DW_MF_CUSTOMER_INFO,AI_DW_MF_CUSTOMERSDETAILS
ALLOWED_TABLES_CSV = _env("CASHFLOW_ALLOWED_TABLES", f"{CUSTOMER_TABLE},{DETAILS_TABLE}")
ALLOWED_TABLES = [t.strip().upper() for t in (ALLOWED_TABLES_CSV or "").split(",") if t.strip()]


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


def _extract_top_n(text: str) -> Optional[int]:
    t = (text or "").lower()
    m = re.search(r"\btop\s+(\d{1,5})\b", t)
    if m:
        return _safe_int(m.group(1), default=DEFAULT_LIMIT, lo=1, hi=MAX_LIMIT)
    m2 = re.search(r"টপ\s*([0-9]{1,5})", text or "")
    if m2:
        return _safe_int(m2.group(1), default=DEFAULT_LIMIT, lo=1, hi=MAX_LIMIT)
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


def _extract_value_after_keyword(text: str, keyword: str, bangla_keyword: Optional[str] = None) -> Optional[str]:
    s = (text or "").strip()
    keys = [keyword] + ([bangla_keyword] if bangla_keyword else [])
    for k in keys:
        m = re.search(rf"\b{re.escape(k)}\b\s*[:=\-]?\s*([A-Za-z0-9_ .()]+)", s, flags=re.IGNORECASE)
        if m:
            val = (m.group(1) or "").strip()
            val = re.split(
                r"\b(top|limit|customerid|customer id|district|branch|জেলা|ব্রাঞ্চ)\b",
                val,
                flags=re.IGNORECASE,
            )[0].strip()
            return val[:80] if val else None
    return None


def _nifi_execute(sql: str) -> JsonT:
    url = f"{NIFI_BASE}{NIFI_PATH}"
    r = requests.post(
        url,
        json={"sql": sql},
        timeout=(NIFI_CONNECT_TIMEOUT, NIFI_READ_TIMEOUT),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    return r.json()


def _to_rows(result_json: JsonT) -> List[Dict[str, Any]]:
    if isinstance(result_json, list):
        return [r for r in result_json if isinstance(r, dict)]
    if isinstance(result_json, dict):
        return [result_json]
    return []


def _plain_bangla(message: str) -> HttpResponse:
    return HttpResponse(message, content_type="text/plain; charset=utf-8")


def _bn_num(n: int) -> str:
    trans = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
    return f"{n:,}".translate(trans)


# =========================================================
# Date range parsing
# =========================================================
@dataclass
class DateRange:
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    preset: Optional[str] = None  # last_7_days, last_30_days, this_month, today


def _parse_date_range(q: str) -> DateRange:
    s = (q or "").strip().lower()

    if "today" in s:
        return DateRange(preset="today")
    if "this month" in s or "current month" in s:
        return DateRange(preset="this_month")
    m = re.search(r"\blast\s+(\d{1,3})\s+days\b", s)
    if m:
        d = _safe_int(m.group(1), default=7, lo=1, hi=365)
        return DateRange(preset=f"last_{d}_days")

    if "আজ" in q:
        return DateRange(preset="today")
    if "এই মাস" in q or "চলতি মাস" in q:
        return DateRange(preset="this_month")
    m2 = re.search(r"গত\s*([0-9]{1,3})\s*দিন", q)
    if m2:
        d = _safe_int(m2.group(1), default=7, lo=1, hi=365)
        return DateRange(preset=f"last_{d}_days")

    m3 = re.search(r"\bfrom\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\s+to\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\b", s)
    if m3:
        return DateRange(date_from=m3.group(1), date_to=m3.group(2))

    m4 = re.search(r"\bbetween\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\s+and\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\b", s)
    if m4:
        return DateRange(date_from=m4.group(1), date_to=m4.group(2))

    m5 = re.search(r"([0-9]{4}-[0-9]{2}-[0-9]{2})\s*থেকে\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", q)
    if m5:
        return DateRange(date_from=m5.group(1), date_to=m5.group(2))

    return DateRange()


# =========================================================
# Status parsing
# =========================================================
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


# =========================================================
# Metrics mode parsing
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
    # Force SQLCoder path explicitly
    m = str(body.get("mode", "")).strip().lower()
    if m in ("sqlcoder", "nl2sql", "sql"):
        return True
    if body.get("use_sqlcoder") is True:
        return True
    return False


# =========================================================
# Intent model
# =========================================================
@dataclass
class Intent:
    name: str
    limit: int = DEFAULT_LIMIT
    customerid: Optional[str] = None
    branch: Optional[str] = None
    district: Optional[str] = None
    status: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    date_preset: Optional[str] = None


# =========================================================
# Metric-key direct mapping
# =========================================================
def _metric_key_to_intent(q: str) -> Optional[str]:
    s = (q or "").strip().lower()
    s = s.replace("-", "_")
    s = re.sub(r"\s+", "_", s)

    if "latest_details_record_datetime" in s or "latest_customer_details_record_datetime" in s:
        return "last_details_date"
    if "total_customers" in s:
        return "count_customers"
    if "total_customer_details" in s or "total_customer_details_records" in s:
        return "count_customer_details"
    if "customers_by_branch" in s or "customer_by_branch" in s:
        return "group_customers_by_branch"
    return None


# =========================================================
# Intent detection
# =========================================================
def _is_count_customers(q: str) -> bool:
    s = (q or "").lower()
    if "মোট গ্রাহক" in q or "গ্রাহক কত" in q or "কাস্টমার কত" in q:
        return True
    if "details" in s:
        return False
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
    if bool(re.search(r"\b(last|latest|most recent|recent)\b", s) and re.search(r"\b(date|datetime|time|record|insert)\b", s)):
        return True
    if ("সর্বশেষ" in q or "শেষ" in q or "লেটেস্ট" in q) and ("তারিখ" in q or "সময়" in q or "টাইম" in q or "রেকর্ড" in q):
        return True
    return False


def _is_group_by_branch(q: str) -> bool:
    s = (q or "").lower()
    if "ব্রাঞ্চ" in q and ("ভিত্তিক" in q or "অনুযায়ী" in q or "ওয়াইজ" in q) and ("গ্রাহক" in q or "কাস্টমার" in q):
        return True
    return bool(("customer" in s) and ("branch" in s) and (("group" in s) or ("wise" in s) or ("summary" in s)))


def _is_list_customers(q: str) -> bool:
    s = (q or "").lower()
    if ("তালিকা" in q or "লিস্ট" in q or "দেখাও" in q or "দাও" in q) and ("গ্রাহক" in q or "কাস্টমার" in q) and ("ডিটেইল" not in q and "ডিটেল" not in q):
        return True
    return bool(re.search(r"\b(list|show)\b", s) and "customer" in s and "details" not in s)


def _is_list_customer_details(q: str) -> bool:
    s = (q or "").lower()
    if ("তালিকা" in q or "লিস্ট" in q or "দেখাও" in q or "দাও" in q) and ("ডিটেইল" in q or "ডিটেল" in q):
        return True
    return bool(re.search(r"\b(list|show)\b", s) and "customer" in s and "details" in s)


def _classify(q: str) -> Intent:
    limit = _extract_top_n(q) or DEFAULT_LIMIT
    limit = _safe_int(limit, default=DEFAULT_LIMIT, lo=1, hi=MAX_LIMIT)

    cid = _extract_customerid(q)
    branch = _extract_value_after_keyword(q, "branch", "ব্রাঞ্চ")
    district = _extract_value_after_keyword(q, "district", "জেলা")

    status = _parse_status(q)
    dr = _parse_date_range(q)

    mk = _metric_key_to_intent(q)
    if mk:
        return Intent(
            mk,
            limit=limit,
            customerid=cid,
            branch=branch,
            district=district,
            status=status,
            date_from=dr.date_from,
            date_to=dr.date_to,
            date_preset=dr.preset,
        )

    if _is_last_details_date(q):
        return Intent("last_details_date", limit=1, branch=branch, district=district, status=status, date_from=dr.date_from, date_to=dr.date_to, date_preset=dr.preset)

    if cid and (re.search(r"\bcustomer\s+details?\b", (q or "").lower()) or ("ডিটেইল" in q or "ডিটেল" in q)):
        if re.search(r"\b(latest|last|most recent|recent)\b", (q or "").lower()) or ("সর্বশেষ" in q or "শেষ" in q or "লেটেস্ট" in q):
            return Intent("latest_customer_details_by_id", limit=limit, customerid=cid)
        return Intent("customer_details_by_id", limit=limit, customerid=cid)

    if cid and (
        re.search(r"\bcustomer\s+(info|information)\b", (q or "").lower())
        or ("গ্রাহক তথ্য" in q)
        or ("কাস্টমার ইনফো" in q)
        or ("customer info" in (q or "").lower())
    ):
        return Intent("customer_info_by_id", limit=limit, customerid=cid)

    if _is_count_customer_details(q):
        return Intent("count_customer_details", limit=1, branch=branch, district=district, status=status, date_from=dr.date_from, date_to=dr.date_to, date_preset=dr.preset)

    if _is_count_customers(q):
        return Intent("count_customers", limit=1, branch=branch, district=district, status=status, date_from=dr.date_from, date_to=dr.date_to, date_preset=dr.preset)

    if _is_group_by_branch(q):
        return Intent("group_customers_by_branch", limit=limit, district=district, status=status, date_from=dr.date_from, date_to=dr.date_to, date_preset=dr.preset)

    if branch:
        return Intent("search_customers_by_branch", limit=limit, branch=branch, status=status, date_from=dr.date_from, date_to=dr.date_to, date_preset=dr.preset)

    if district:
        return Intent("search_customers_by_district", limit=limit, district=district, status=status, date_from=dr.date_from, date_to=dr.date_to, date_preset=dr.preset)

    if _is_list_customer_details(q):
        return Intent("list_customer_details", limit=limit, branch=branch, district=district, status=status, date_from=dr.date_from, date_to=dr.date_to, date_preset=dr.preset)

    if _is_list_customers(q):
        return Intent("list_customers", limit=limit, branch=branch, district=district, status=status, date_from=dr.date_from, date_to=dr.date_to, date_preset=dr.preset)

    return Intent("unsupported", limit=limit)


# =========================================================
# SQL helpers (filters)
# =========================================================
def _details_insert_dt_expr(alias: str = "d") -> str:
    col = f"{alias}.{DETAILS_INSERT_COL}"
    fmt = DETAILS_INSERT_FMT.replace("'", "''")
    return f"TO_DATE({col}, '{fmt}')"


def _apply_details_date_filter(where: List[str], alias: str, intent: Intent) -> None:
    dt = _details_insert_dt_expr(alias)

    if intent.date_from and intent.date_to:
        where.append(
            f"{dt} BETWEEN TO_DATE({_quote_sql_literal(intent.date_from)}, 'YYYY-MM-DD') "
            f"AND TO_DATE({_quote_sql_literal(intent.date_to)}, 'YYYY-MM-DD') + 0.99999"
        )
        return

    if intent.date_preset:
        p = intent.date_preset
        if p == "today":
            where.append(f"TRUNC({dt}) = TRUNC(SYSDATE)")
            return
        if p == "this_month":
            where.append(f"TRUNC({dt}, 'MM') = TRUNC(SYSDATE, 'MM')")
            return
        m = re.match(r"last_([0-9]{1,3})_days", p or "")
        if m:
            days = _safe_int(m.group(1), default=7, lo=1, hi=365)
            where.append(f"{dt} >= SYSDATE - {days}")
            return


def _apply_customer_geo_filter(where: List[str], alias: str, intent: Intent) -> None:
    if intent.branch:
        where.append(f"UPPER(NVL({alias}.{BRANCH_COL}, '')) LIKE '%' || {_quote_sql_literal(intent.branch.upper())} || '%'")
    if intent.district:
        where.append(f"UPPER(NVL({alias}.{DISTRICT_COL}, '')) LIKE '%' || {_quote_sql_literal(intent.district.upper())} || '%'")


def _apply_status_filter(where: List[str], alias: str, intent: Intent) -> None:
    if not intent.status:
        return
    active_vals = _csv_list(ACTIVE_VALUES_CSV)
    inactive_vals = _csv_list(INACTIVE_VALUES_CSV)
    if intent.status == "active":
        where.append(f"UPPER(NVL({alias}.{DETAILS_STATUS_COL}, '')) IN ({_sql_in_list(active_vals)})")
    elif intent.status == "inactive":
        where.append(f"UPPER(NVL({alias}.{DETAILS_STATUS_COL}, '')) IN ({_sql_in_list(inactive_vals)})")


# =========================================================
# Deterministic SQL Builders (your original behavior)
# =========================================================
def _sql_count_customers(intent: Intent) -> str:
    c = "c"
    d = "d"
    c_t = f"{SCHEMA}.{CUSTOMER_TABLE}"
    d_t = f"{SCHEMA}.{DETAILS_TABLE}"

    need_details = bool(intent.status or intent.date_from or intent.date_to or intent.date_preset)
    where: List[str] = []

    if need_details:
        _apply_status_filter(where, d, intent)
        _apply_details_date_filter(where, d, intent)

    _apply_customer_geo_filter(where, c, intent)
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


def _sql_count_customer_details(intent: Intent) -> str:
    d = "d"
    c = "c"
    d_t = f"{SCHEMA}.{DETAILS_TABLE}"
    c_t = f"{SCHEMA}.{CUSTOMER_TABLE}"

    where: List[str] = []
    _apply_status_filter(where, d, intent)
    _apply_details_date_filter(where, d, intent)

    join_customer = bool(intent.branch or intent.district)
    if join_customer:
        join_on = _sql_eq_trim0(f"{d}.CUSTOMERID", f"{c}.{CUSTOMER_ID_COL}")
        _apply_customer_geo_filter(where, c, intent)
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


def _sql_last_details_date(intent: Intent) -> str:
    d = "d"
    c = "c"
    d_t = f"{SCHEMA}.{DETAILS_TABLE}"
    c_t = f"{SCHEMA}.{CUSTOMER_TABLE}"
    fmt = DETAILS_INSERT_FMT.replace("'", "''")
    dt_expr = _details_insert_dt_expr(d)

    where: List[str] = []
    _apply_status_filter(where, d, intent)
    _apply_details_date_filter(where, d, intent)

    join_customer = bool(intent.branch or intent.district)
    if join_customer:
        join_on = _sql_eq_trim0(f"{d}.CUSTOMERID", f"{c}.{CUSTOMER_ID_COL}")
        _apply_customer_geo_filter(where, c, intent)
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


def _sql_list_customers(intent: Intent) -> str:
    c = "c"
    d = "d"
    c_t = f"{SCHEMA}.{CUSTOMER_TABLE}"
    d_t = f"{SCHEMA}.{DETAILS_TABLE}"

    need_details = bool(intent.status or intent.date_from or intent.date_to or intent.date_preset)
    where: List[str] = []
    _apply_customer_geo_filter(where, c, intent)

    if need_details:
        _apply_status_filter(where, d, intent)
        _apply_details_date_filter(where, d, intent)
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
WHERE ROWNUM <= {intent.limit}
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
WHERE ROWNUM <= {intent.limit}
""".strip()


def _sql_list_customer_details(intent: Intent) -> str:
    d = "d"
    c = "c"
    d_t = f"{SCHEMA}.{DETAILS_TABLE}"
    c_t = f"{SCHEMA}.{CUSTOMER_TABLE}"
    fmt = DETAILS_INSERT_FMT.replace("'", "''")
    dt_expr = _details_insert_dt_expr(d)

    where: List[str] = []
    _apply_status_filter(where, d, intent)
    _apply_details_date_filter(where, d, intent)

    join_customer = bool(intent.branch or intent.district)
    if join_customer:
        join_on = _sql_eq_trim0(f"{d}.CUSTOMERID", f"{c}.{CUSTOMER_ID_COL}")
        _apply_customer_geo_filter(where, c, intent)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        return f"""
SELECT *
FROM (
  SELECT
    {d}.CUSTOMERID,
    {d}.{DETAILS_INSERT_COL} AS INSERT_DATE,
    TO_CHAR({dt_expr}, '{fmt}') AS INSERT_DATE_PARSED,
    {d}.*
  FROM {d_t} {d}
  JOIN {c_t} {c}
    ON {join_on}
  {where_sql}
  ORDER BY {dt_expr} DESC
)
WHERE ROWNUM <= {intent.limit}
""".strip()

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return f"""
SELECT *
FROM (
  SELECT
    {d}.CUSTOMERID,
    {d}.{DETAILS_INSERT_COL} AS INSERT_DATE,
    TO_CHAR({dt_expr}, '{fmt}') AS INSERT_DATE_PARSED,
    {d}.*
  FROM {d_t} {d}
  {where_sql}
  ORDER BY {dt_expr} DESC
)
WHERE ROWNUM <= {intent.limit}
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
WHERE ROWNUM <= {limit}
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
    d.CUSTOMERID,
    d.{DETAILS_INSERT_COL} AS INSERT_DATE,
    TO_CHAR({dt_expr}, '{fmt}') AS INSERT_DATE_PARSED,
    d.*
  FROM {d_t} d
  WHERE {_sql_eq_trim0('d.CUSTOMERID', lit)}
  {order}
)
WHERE ROWNUM <= {limit}
""".strip()


def _sql_group_customers_by_branch(intent: Intent) -> str:
    c = "c"
    d = "d"
    c_t = f"{SCHEMA}.{CUSTOMER_TABLE}"
    d_t = f"{SCHEMA}.{DETAILS_TABLE}"
    where: List[str] = []

    need_details = bool(intent.status or intent.date_from or intent.date_to or intent.date_preset)
    if need_details:
        _apply_status_filter(where, d, intent)
        _apply_details_date_filter(where, d, intent)

    if intent.district:
        where.append(f"UPPER(NVL({c}.{DISTRICT_COL}, '')) LIKE '%' || {_quote_sql_literal(intent.district.upper())} || '%'")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    if need_details:
        join_on = _sql_eq_trim0(f"{d}.CUSTOMERID", f"{c}.{CUSTOMER_ID_COL}")
        return f"""
SELECT *
FROM (
  SELECT
    NVL({c}.{BRANCH_COL}, 'UNKNOWN') AS BRANCHNAME,
    COUNT(DISTINCT {c}.{CUSTOMER_ID_COL}) AS CUSTOMER_COUNT
  FROM {c_t} {c}
  JOIN {d_t} {d}
    ON {join_on}
  {where_sql}
  GROUP BY NVL({c}.{BRANCH_COL}, 'UNKNOWN')
  ORDER BY CUSTOMER_COUNT DESC
)
WHERE ROWNUM <= {intent.limit}
""".strip()

    return f"""
SELECT *
FROM (
  SELECT
    NVL({c}.{BRANCH_COL}, 'UNKNOWN') AS BRANCHNAME,
    COUNT(1) AS CUSTOMER_COUNT
  FROM {c_t} {c}
  {where_sql}
  GROUP BY NVL({c}.{BRANCH_COL}, 'UNKNOWN')
  ORDER BY CUSTOMER_COUNT DESC
)
WHERE ROWNUM <= {intent.limit}
""".strip()


# =========================================================
# SQLCoder integration
# =========================================================
_SQL_BLOCKED = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|truncate|create|grant|revoke|commit|rollback)\b",
    re.IGNORECASE,
)

def _strip_sql_noise(s: str) -> str:
    s = (s or "").strip()
    # remove code fences
    s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _validate_sqlcoder_sql(sql: str) -> Optional[str]:
    """
    Returns error string if invalid, else None.
    """
    sql = _strip_sql_noise(sql)
    if not sql:
        return "SQLCoder returned empty SQL."

    # forbid multiple statements via semicolon (Oracle allows ; in clients; we disallow)
    if ";" in sql:
        return "Semicolon not allowed (multiple statements risk)."

    # forbid comments
    if re.search(r"(--|/\*)", sql):
        return "Comments not allowed."

    # must start with SELECT or WITH
    if not re.match(r"^\s*(select|with)\b", sql, flags=re.IGNORECASE):
        return "Only SELECT/WITH queries are allowed."

    # block DDL/DML keywords
    if _SQL_BLOCKED.search(sql):
        return "DML/DDL keywords are not allowed."

    # require schema prefix (SCOTT.)
    if SCHEMA.upper() + "." not in sql.upper():
        return f"SQL must reference schema {SCHEMA} with schema-qualified table names."

    # allowlist check (optional but recommended)
    if ALLOWED_TABLES:
        # Extract occurrences of SCHEMA.TABLE tokens
        tokens = re.findall(rf"\b{re.escape(SCHEMA)}\.([A-Z0-9_]+)\b", sql.upper())
        used = set(tokens)
        # If it references schema but not via direct tokens (rare), still safe fallback
        for t in used:
            if t not in ALLOWED_TABLES:
                return f"Table {SCHEMA}.{t} is not allowed for SQLCoder."

    return None


def _ollama_generate_sqlcoder(question: str, limit: int) -> str:
    """
    Calls Ollama /api/generate with SQLCoder model to return Oracle 11g SQL.
    """
    url = f"{OLLAMA_URL}/api/generate"

    # Provide minimal schema guidance (you can expand later with your Qdrant schema)
    allowed_tables_hint = ", ".join([f"{SCHEMA}.{t}" for t in ALLOWED_TABLES]) if ALLOWED_TABLES else f"{SCHEMA}.*"

    # Force stable constraints
    prompt = f"""
You are SQLCoder. Produce ONLY a single Oracle 11g SELECT query. No explanation. No markdown.

Rules:
- Only SELECT or WITH ... SELECT
- No INSERT/UPDATE/DELETE/MERGE/DDL
- No comments, no semicolons
- Always use schema-qualified names: {SCHEMA}.TABLE
- Use ROWNUM for limiting. If user asks top/limit, apply WHERE ROWNUM <= N.
- Prefer safe predicates. For customer ids: compare ignoring leading zeros:
  LTRIM(TRIM(col), '0') = LTRIM(TRIM(:id), '0')  (but use literal in this system)
- Allowed tables/views: {allowed_tables_hint}
- Default limit: {limit}

User request:
{question}

Return only the SQL:
""".strip()

    payload = {
        "model": SQLCODER_MODEL,
        "prompt": prompt,
        "stream": False,
        # keep it deterministic
        "options": {
            "temperature": 0,
        },
    }

    r = requests.post(
        url,
        json=payload,
        timeout=(SQLCODER_CONNECT_TIMEOUT, SQLCODER_READ_TIMEOUT),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    data = r.json()
    sql = _strip_sql_noise(_clean(data.get("response")))
    return sql


def _ensure_rownum_limit(sql: str, limit: int) -> str:
    """
    If SQLCoder forgets limit, we wrap with ROWNUM <= limit safely.
    """
    up = sql.upper()
    if "ROWNUM" in up:
        return sql

    # Wrap as subquery
    return f"""
SELECT *
FROM (
{sql}
)
WHERE ROWNUM <= {int(limit)}
""".strip()


# =========================================================
# Response formatting
# =========================================================
def _metric_response(
    metric: str,
    value: Union[int, float, str, None],
    filters: Dict[str, Any],
    summary_bn: str,
    summary_en: str,
    extra: Optional[Dict[str, Any]] = None,
) -> JsonResponse:
    report_date = time.strftime("%Y-%m-%d")
    payload: Dict[str, Any] = {
        "metric": metric,
        "report_date": report_date,
        "value": value,
        "filters": filters,
        "summary_bn": summary_bn,
        "summary_en": summary_en,
    }
    if extra:
        payload.update(extra)
    return JsonResponse(payload, json_dumps_params={"ensure_ascii": False})


def _kpi_bangla_email(subject_line: str, body_line: str) -> HttpResponse:
    report_date = time.strftime("%Y-%m-%d")
    msg = (
        "আসসালামু আলাইকুম।\n\n"
        f"{subject_line}\n\n"
        f"{body_line}\n\n"
        "আপনাদের সহযোগিতা ও আস্থার জন্য আন্তরিক কৃতজ্ঞতা প্রকাশ করছি।\n\n"
        "ধন্যবাদান্তে,\n"
        "ক্যাশফ্লো ড্যাশবোর্ড\n"
        f"(রিপোর্ট তারিখ: {report_date})"
    )
    return _plain_bangla(msg)


# =========================================================
# Endpoint
# =========================================================
@csrf_exempt
@require_POST
def cashflow_query(request):
    # body
    try:
        body = json.loads((request.body or b"{}").decode("utf-8", errors="replace"))
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}

    q = _clean(body.get("q") or body.get("question") or body.get("message"))
    if not q:
        return JsonResponse({"error": "Missing q"}, status=400)

    metrics_mode = _is_metrics_mode(body, q)
    force_sqlcoder = _is_sqlcoder_forced(body)

    intent = _classify(q)

    filters = {
        "branch": intent.branch,
        "district": intent.district,
        "status": intent.status,
        "date_from": intent.date_from,
        "date_to": intent.date_to,
        "date_preset": intent.date_preset,
    }

    try:
        # ----------------------------
        # KPI / Metrics: total customers
        # ----------------------------
        if intent.name == "count_customers" and not force_sqlcoder:
            sql = _sql_count_customers(intent)
            rows = _to_rows(_nifi_execute(sql))
            total = _safe_parse_int(rows[0].get("TOTAL_CUSTOMERS") if rows else 0)

            if metrics_mode:
                return _metric_response(
                    metric="total_customers",
                    value=total,
                    filters=filters,
                    summary_bn=f"মোট গ্রাহক: {_bn_num(total)} জন।",
                    summary_en=f"Total customers: {total:,}.",
                )

            return _kpi_bangla_email(
                subject_line="বিষয়: মোট গ্রাহকের সংখ্যা (সিস্টেম রিপোর্ট)",
                body_line=f"বিনীতভাবে জানাচ্ছি যে, বর্তমানে আমাদের সিস্টেমে মোট নিবন্ধিত গ্রাহকের সংখ্যা {_bn_num(total)} জন।",
            )

        # ----------------------------
        # KPI / Metrics: total customer details
        # ----------------------------
        if intent.name == "count_customer_details" and not force_sqlcoder:
            sql = _sql_count_customer_details(intent)
            rows = _to_rows(_nifi_execute(sql))
            total = _safe_parse_int(rows[0].get("TOTAL_CUSTOMER_DETAILS") if rows else 0)

            if metrics_mode:
                return _metric_response(
                    metric="total_customer_details_records",
                    value=total,
                    filters=filters,
                    summary_bn=f"Customer Details রেকর্ড: {_bn_num(total)} টি।",
                    summary_en=f"Customer details records: {total:,}.",
                )

            return _kpi_bangla_email(
                subject_line="বিষয়: Customer Details রেকর্ড সংখ্যা (সিস্টেম রিপোর্ট)",
                body_line=f"বিনীতভাবে জানাচ্ছি যে, বর্তমানে Customer Details ডেটাসেটে মোট রেকর্ডের সংখ্যা {_bn_num(total)} টি।",
            )

        # ----------------------------
        # KPI / Metrics: latest details record datetime
        # ----------------------------
        if intent.name == "last_details_date" and not force_sqlcoder:
            sql = _sql_last_details_date(intent)
            rows = _to_rows(_nifi_execute(sql))
            last_dt = _clean(rows[0].get("LAST_RECORD_DATETIME") if rows else "")

            if metrics_mode:
                return _metric_response(
                    metric="latest_customer_details_record_datetime",
                    value=last_dt or None,
                    filters=filters,
                    summary_bn=f"Customer Details সর্বশেষ রেকর্ড সময়: {last_dt}" if last_dt else "Customer Details সর্বশেষ রেকর্ড সময় নির্ণয় করা যায়নি।",
                    summary_en=f"Latest customer details record datetime: {last_dt}" if last_dt else "Latest record datetime could not be determined.",
                )

            if not last_dt or last_dt.lower() == "null":
                return _kpi_bangla_email(
                    subject_line="বিষয়: Customer Details সর্বশেষ রেকর্ড সময় (সিস্টেম রিপোর্ট)",
                    body_line=f"বিনীতভাবে জানাচ্ছি যে, Customer Details ডেটাসেটে সর্বশেষ রেকর্ডের সময় নির্ণয় করা যায়নি। অনুগ্রহ করে {DETAILS_INSERT_COL} ফিল্ডের ফরম্যাট যাচাই করুন।",
                )

            return _kpi_bangla_email(
                subject_line="বিষয়: Customer Details সর্বশেষ রেকর্ড সময় (সিস্টেম রিপোর্ট)",
                body_line=f"বিনীতভাবে জানাচ্ছি যে, Customer Details ডেটাসেটে সর্বশেষ রেকর্ড সংরক্ষিত হয়েছে: {last_dt}।",
            )

        # ----------------------------
        # Data outputs (deterministic)
        # ----------------------------
        if intent.name == "list_customers" and not force_sqlcoder:
            sql = _sql_list_customers(intent)
            return JsonResponse({"rows": _to_rows(_nifi_execute(sql))}, json_dumps_params={"ensure_ascii": False})

        if intent.name == "list_customer_details" and not force_sqlcoder:
            sql = _sql_list_customer_details(intent)
            return JsonResponse({"rows": _to_rows(_nifi_execute(sql))}, json_dumps_params={"ensure_ascii": False})

        if intent.name == "customer_info_by_id" and not force_sqlcoder:
            if not intent.customerid:
                return JsonResponse({"error": "customerid required"}, status=400)
            sql = _sql_customer_info_by_id(intent.customerid, intent.limit)
            return JsonResponse({"rows": _to_rows(_nifi_execute(sql))}, json_dumps_params={"ensure_ascii": False})

        if intent.name == "customer_details_by_id" and not force_sqlcoder:
            if not intent.customerid:
                return JsonResponse({"error": "customerid required"}, status=400)
            sql = _sql_customer_details_by_id(intent.customerid, intent.limit, latest_first=False)
            return JsonResponse({"rows": _to_rows(_nifi_execute(sql))}, json_dumps_params={"ensure_ascii": False})

        if intent.name == "latest_customer_details_by_id" and not force_sqlcoder:
            if not intent.customerid:
                return JsonResponse({"error": "customerid required"}, status=400)
            sql = _sql_customer_details_by_id(intent.customerid, intent.limit, latest_first=True)
            return JsonResponse({"rows": _to_rows(_nifi_execute(sql))}, json_dumps_params={"ensure_ascii": False})

        if intent.name in ("search_customers_by_branch", "search_customers_by_district") and not force_sqlcoder:
            sql = _sql_list_customers(intent)
            return JsonResponse({"rows": _to_rows(_nifi_execute(sql))}, json_dumps_params={"ensure_ascii": False})

        if intent.name == "group_customers_by_branch" and not force_sqlcoder:
            sql = _sql_group_customers_by_branch(intent)
            rows = _to_rows(_nifi_execute(sql))
            if metrics_mode:
                return _metric_response(
                    metric="customers_by_branch",
                    value=len(rows),
                    filters=filters,
                    summary_bn="ব্রাঞ্চ অনুযায়ী গ্রাহক সংখ্যার সারসংক্ষেপ প্রস্তুত হয়েছে।",
                    summary_en="Branch-wise customer counts are prepared.",
                    extra={"series": rows},
                )
            return JsonResponse({"rows": rows}, json_dumps_params={"ensure_ascii": False})

        # =====================================================
        # SQLCODER FALLBACK (or forced mode)
        # =====================================================
        # At this point: either unsupported intent OR user forced SQLCoder.
        limit = intent.limit or DEFAULT_LIMIT
        limit = _safe_int(limit, DEFAULT_LIMIT, 1, MAX_LIMIT)

        sql = _ollama_generate_sqlcoder(q, limit=limit)
        err = _validate_sqlcoder_sql(sql)
        if err:
            return JsonResponse(
                {
                    "error": "SQLCoder produced unsafe/invalid SQL",
                    "details": err,
                    "model": SQLCODER_MODEL,
                    "raw_sql": sql,
                },
                status=400,
                json_dumps_params={"ensure_ascii": False},
            )

        sql = _ensure_rownum_limit(sql, limit)
        rows = _to_rows(_nifi_execute(sql))

        # Keep same API style as your data mode:
        return JsonResponse(
            {
                "mode": "sqlcoder",
                "model": SQLCODER_MODEL,
                "sql": sql,
                "rows": rows,
                "count": len(rows),
            },
            json_dumps_params={"ensure_ascii": False},
        )

    except requests.exceptions.Timeout:
        return JsonResponse({"error": "Timeout"}, status=504)
    except requests.exceptions.RequestException as e:
        # Could be NiFi or Ollama depending on which call happened
        return JsonResponse({"error": f"Request failed: {str(e)}"}, status=502)
    except Exception as e:
        return JsonResponse({"error": f"Unexpected error: {str(e)}"}, status=500)
