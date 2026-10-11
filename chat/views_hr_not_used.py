from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List

import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

# =========================================================
# Config
# =========================================================
NIFI_BASE = os.getenv("NIFI_BASE", "http://192.168.1.139:8888").rstrip("/")
NIFI_HR_PATH = os.getenv("NIFI_HR_PATH", "/hr/query").strip()

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
GEN_MODEL = os.getenv("GEN_MODEL", "aya-23-8b").strip()

REQ_TIMEOUT = float(os.getenv("REQ_TIMEOUT", "250"))

# --- Base intents you already have in NiFi ---
BASE_HR_INTENTS = {
    "top_salary",
    "salary_over",
    "employee",
    "stats",
}

# --- Optional extra intents (ONLY enable if you implement in NiFi) ---
ENABLE_EXTRA_INTENTS = bool(int(os.getenv("ENABLE_HR_EXTRA_INTENTS", "0")))

EXTRA_HR_INTENTS = {
    # Requires NiFi support if enabled:
    "lowest_salary",        # params: limit
    "salary_between",       # params: min_amount, max_amount, limit
    "employee_by_phone",    # params: phone
    "employee_by_nid",      # params: nid
    "employee_search_name", # params: name
    "count_employees",      # params: none (or filters)
}

ALLOWED_HR_INTENTS = set(BASE_HR_INTENTS) | (set(EXTRA_HR_INTENTS) if ENABLE_EXTRA_INTENTS else set())


# =========================================================
# Types
# =========================================================
@dataclass
class ParsedQuery:
    intent: str
    params: Dict[str, Any]
    confidence: float
    method: str  # "rules" or "llm"


# =========================================================
# Helpers
# =========================================================
def _normalize(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t.lower()


def _safe_int(v: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return default
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).strip().replace(",", "")
        return int(s)
    except Exception:
        return default


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _safe_id(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if re.fullmatch(r"[0-9]+", s) else None


def _digits_only(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = re.sub(r"\D+", "", str(v))
    return s or None


def _call_nifi_hr(intent: str, params: Dict[str, Any]) -> Any:
    url = f"{NIFI_BASE}{NIFI_HR_PATH}"
    payload = {"intent": intent, "params": params}
    r = requests.post(url, json=payload, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}


def _ollama_chat(system: str, user: str) -> str:
    payload = {
        "model": GEN_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.0, "top_p": 0.7, "num_predict": 220},
    }
    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return (data.get("message", {}).get("content") or "").strip()


# =========================================================
# Rule-based parser (expanded)
# =========================================================
HR_KW_SALARY = [
    "salary", "basic", "basicsalary", "gross", "pay", "wage",
    "বেতন", "বেসিক", "গ্রস", "পারিশ্রমিক",
]
HR_KW_EMP = [
    "employee", "emp", "employeeid", "employee id", "id",
    "কর্মচারী", "এমপ্লয়ি", "আইডি", "আইড", "আইডি নম্বর",
]
HR_KW_DEPT = ["department", "dept", "বিভাগ", "ডিপার্টমেন্ট"]
HR_KW_BRANCH = ["branch", "শাখা", "ব্রাঞ্চ"]

KW_TOP = ["top", "highest", "max", "maximum", "most", "largest", "সর্বোচ্চ", "সবচেয়ে বেশি", "সর্বাধিক"]
KW_LOW = ["lowest", "min", "minimum", "least", "smallest", "সর্বনিম্ন", "সবচেয়ে কম"]

KW_AVG = ["average", "avg", "mean", "গড়", "গড়", "এভারেজ"]
KW_COUNT = ["count", "how many", "number of", "কতজন", "কত", "সংখ্যা", "গণনা"]
KW_STATS = ["stats", "summary", "report", "চিত্র", "সারাংশ", "স্ট্যাটস", "স্ট্যাটিস্টিক্স"]

KW_OVER = ["over", "greater than", "more than", "above", ">", "বেশি", "উপরে", "এর বেশি"]
KW_UNDER = ["under", "less than", "below", "<", "কম", "এর কম"]


def _extract_limit(q: str, default: int, lo: int, hi: int) -> int:
    m = re.search(r"\btop\s+(\d+)\b", q)
    if not m:
        m = re.search(r"\blimit\s+(\d+)\b", q)
    if not m:
        m = re.search(r"\b(\d+)\s*(?:জন|টি)\b", q)  # Bangla-ish count marker
    n = _safe_int(m.group(1), default) if m else default
    return _clamp(n or default, lo, hi)


def _extract_amount_after_keywords(q: str, keywords: List[str]) -> Optional[int]:
    # Try: "over 100000" / "greater than 1,00,000" / "বেশি ১০০০০০" (digits only)
    for kw in keywords:
        # allow spaces and symbols; keep digits+comma
        m = re.search(rf"(?:{re.escape(kw)})\s*[:=]?\s*([0-9][0-9,]*)", q)
        if m:
            return _safe_int(m.group(1))
    # fallback: operators
    m = re.search(r"(?:>=|>|<=|<)\s*([0-9][0-9,]*)", q)
    if m:
        return _safe_int(m.group(1))
    return None


def _extract_employee_id(q: str) -> Optional[str]:
    # common patterns
    patterns = [
        r"\b(?:employee\s*id|employeeid|emp\s*id|employee|emp)\s*(?:is\s*)?[:#]?\s*([0-9]+)\b",
        r"\bid\s*(?:is\s*)?[:#]?\s*([0-9]+)\b",
        r"\bআইডি\s*(?:হল|হবে|is)?\s*[:#]?\s*([0-9]+)\b",
        r"\bআইড\s*(?:হল|হবে|is)?\s*[:#]?\s*([0-9]+)\b",
    ]
    for p in patterns:
        m = re.search(p, q)
        if m:
            return m.group(1)
    # if question is like "24123 employee" or just "tell 24123"
    m = re.search(r"\b([0-9]{3,})\b", q)
    return m.group(1) if m else None


def parse_hr_question_rules(question: str) -> ParsedQuery:
    q = _normalize(question)

    # -----------------------------------------------------
    # Employee detail (ID-centric)
    # -----------------------------------------------------
    if any(k in q for k in HR_KW_EMP) or re.search(r"\b[0-9]{3,}\b", q):
        emp_id = _extract_employee_id(q)
        if emp_id and _safe_id(emp_id):
            # If salary keywords exist, still employee intent (employee + salary detail)
            return ParsedQuery("employee", {"id": emp_id}, 0.92, "rules")

    # -----------------------------------------------------
    # Highest / Top salary
    # -----------------------------------------------------
    if any(k in q for k in HR_KW_SALARY) and any(k in q for k in KW_TOP):
        limit = _extract_limit(q, default=1, lo=1, hi=100)
        # if user says "top salary" without N, default=10
        if "top" in q and not re.search(r"\btop\s+\d+\b", q) and limit == 1:
            limit = 10
        return ParsedQuery("top_salary", {"limit": limit}, 0.93, "rules")

    # -----------------------------------------------------
    # Lowest salary (optional intent)
    # If NiFi doesn't support, keep unknown unless ENABLE_HR_EXTRA_INTENTS=1
    # -----------------------------------------------------
    if any(k in q for k in HR_KW_SALARY) and any(k in q for k in KW_LOW):
        if ENABLE_EXTRA_INTENTS and "lowest_salary" in ALLOWED_HR_INTENTS:
            limit = _extract_limit(q, default=10, lo=1, hi=100)
            return ParsedQuery("lowest_salary", {"limit": limit}, 0.85, "rules")

    # -----------------------------------------------------
    # Salary over X
    # -----------------------------------------------------
    if any(k in q for k in HR_KW_SALARY) and any(k in q for k in KW_OVER):
        amount = _extract_amount_after_keywords(q, KW_OVER)
        if amount is not None:
            limit = _extract_limit(q, default=50, lo=1, hi=200)
            return ParsedQuery("salary_over", {"amount": amount, "limit": limit}, 0.90, "rules")

    # -----------------------------------------------------
    # Salary between A and B (optional intent)
    # -----------------------------------------------------
    if ENABLE_EXTRA_INTENTS and "salary_between" in ALLOWED_HR_INTENTS:
        if any(k in q for k in HR_KW_SALARY) and ("between" in q or "থেকে" in q):
            # between 10000 and 20000
            m = re.search(r"\bbetween\s*([0-9][0-9,]*)\s*(?:and|to)\s*([0-9][0-9,]*)\b", q)
            if not m:
                # Bangla style: 10000 থেকে 20000
                m = re.search(r"\b([0-9][0-9,]*)\s*থেকে\s*([0-9][0-9,]*)\b", q)
            if m:
                a = _safe_int(m.group(1))
                b = _safe_int(m.group(2))
                if a is not None and b is not None:
                    lo_amt, hi_amt = (a, b) if a <= b else (b, a)
                    limit = _extract_limit(q, default=50, lo=1, hi=200)
                    return ParsedQuery(
                        "salary_between",
                        {"min_amount": lo_amt, "max_amount": hi_amt, "limit": limit},
                        0.82,
                        "rules",
                    )

    # -----------------------------------------------------
    # Count employees (optional intent)
    # -----------------------------------------------------
    if ENABLE_EXTRA_INTENTS and "count_employees" in ALLOWED_HR_INTENTS:
        if any(k in q for k in KW_COUNT) and any(k in q for k in HR_KW_EMP):
            return ParsedQuery("count_employees", {}, 0.80, "rules")

    # -----------------------------------------------------
    # Stats (department/branch) – avg/count/summary/group-by
    # -----------------------------------------------------
    if any(k in q for k in KW_STATS + KW_AVG + KW_COUNT) or ("group by" in q) or ("ভিত্তিক" in q):
        if any(k in q for k in HR_KW_BRANCH):
            return ParsedQuery("stats", {"group_by": "branch"}, 0.78, "rules")
        if any(k in q for k in HR_KW_DEPT):
            return ParsedQuery("stats", {"group_by": "department"}, 0.78, "rules")

        # If user mentions salary but not group, default dept stats
        if any(k in q for k in HR_KW_SALARY):
            return ParsedQuery("stats", {"group_by": "department"}, 0.60, "rules")

    return ParsedQuery("unknown", {}, 0.0, "rules")


# =========================================================
# Optional LLM parser (fallback) - keep very strict
# =========================================================
LLM_PARSE_SYSTEM = """You convert HR questions into a strict JSON command.
Return ONLY valid JSON. No extra text.

Allowed intents:
- top_salary: requires limit (int)
- salary_over: requires amount (int) and optional limit (int)
- employee: requires id (string digits)
- stats: requires group_by one of ["department","branch"]

If unknown, return {"intent":"unknown","params":{}}.
"""


def parse_hr_question_llm(question: str) -> ParsedQuery:
    prompt = f"""Question: {question}

Return JSON only:
{{
  "intent": "top_salary|salary_over|employee|stats|unknown",
  "params": {{
    "limit": 10,
    "amount": 100000,
    "id": "4411",
    "group_by": "department"
  }}
}}"""
    raw = _ollama_chat(LLM_PARSE_SYSTEM, prompt)

    try:
        obj = json.loads(raw)
    except Exception:
        return ParsedQuery("unknown", {}, 0.0, "llm")

    intent = str(obj.get("intent") or "unknown").strip()
    params = obj.get("params") or {}

    if intent == "top_salary":
        limit = _clamp(_safe_int(params.get("limit"), 10) or 10, 1, 100)
        return ParsedQuery(intent, {"limit": limit}, 0.62, "llm")

    if intent == "salary_over":
        amount = _safe_int(params.get("amount"))
        if amount is None:
            return ParsedQuery("unknown", {}, 0.0, "llm")
        limit = _clamp(_safe_int(params.get("limit"), 50) or 50, 1, 200)
        return ParsedQuery(intent, {"amount": amount, "limit": limit}, 0.62, "llm")

    if intent == "employee":
        emp_id = _safe_id(params.get("id"))
        if emp_id is None:
            return ParsedQuery("unknown", {}, 0.0, "llm")
        return ParsedQuery(intent, {"id": emp_id}, 0.62, "llm")

    if intent == "stats":
        gb = str(params.get("group_by") or "department").lower()
        if gb not in ("department", "branch"):
            gb = "department"
        return ParsedQuery(intent, {"group_by": gb}, 0.55, "llm")

    return ParsedQuery("unknown", {}, 0.0, "llm")


# =========================================================
# Execute via NiFi/Oracle (source of truth)
# =========================================================
def execute_hr(parsed: ParsedQuery) -> Tuple[Optional[Any], Optional[str]]:
    if parsed.intent not in ALLOWED_HR_INTENTS:
        return None, "Unsupported or unknown HR intent."

    params: Dict[str, Any] = dict(parsed.params or {})

    if parsed.intent == "top_salary":
        limit = _clamp(_safe_int(params.get("limit"), 10) or 10, 1, 100)
        params = {"limit": limit}

    elif parsed.intent == "salary_over":
        amount = _safe_int(params.get("amount"))
        if amount is None:
            return None, "Missing/invalid amount."
        limit = _clamp(_safe_int(params.get("limit"), 50) or 50, 1, 200)
        params = {"amount": amount, "limit": limit}

    elif parsed.intent == "employee":
        emp_id = _safe_id(params.get("id"))
        if emp_id is None:
            return None, "Missing/invalid employee id."
        params = {"id": emp_id}

    elif parsed.intent == "stats":
        gb = str(params.get("group_by") or "department").lower()
        if gb not in ("department", "branch"):
            gb = "department"
        params = {"group_by": gb}

    # Optional extras:
    elif parsed.intent == "lowest_salary":
        limit = _clamp(_safe_int(params.get("limit"), 10) or 10, 1, 100)
        params = {"limit": limit}

    elif parsed.intent == "salary_between":
        a = _safe_int(params.get("min_amount"))
        b = _safe_int(params.get("max_amount"))
        if a is None or b is None:
            return None, "Missing/invalid min_amount/max_amount."
        limit = _clamp(_safe_int(params.get("limit"), 50) or 50, 1, 200)
        lo_amt, hi_amt = (a, b) if a <= b else (b, a)
        params = {"min_amount": lo_amt, "max_amount": hi_amt, "limit": limit}

    elif parsed.intent == "employee_by_phone":
        phone = _digits_only(params.get("phone"))
        if not phone:
            return None, "Missing/invalid phone."
        params = {"phone": phone}

    elif parsed.intent == "employee_by_nid":
        nid = _digits_only(params.get("nid"))
        if not nid:
            return None, "Missing/invalid nid."
        params = {"nid": nid}

    elif parsed.intent == "employee_search_name":
        name = (params.get("name") or "").strip()
        if not name:
            return None, "Missing/invalid name."
        params = {"name": name}

    elif parsed.intent == "count_employees":
        params = {}

    try:
        data = _call_nifi_hr(parsed.intent, params)
        return data, None
    except requests.HTTPError as e:
        return None, f"NiFi HTTP error: {str(e)}"
    except Exception as e:
        return None, f"NiFi call failed: {str(e)}"


# =========================================================
# Strict Answer Builder (deterministic first)
# - No extra commentary.
# - If data not usable => "ডেটা অপর্যাপ্ত।"
# =========================================================
def _pick_first_row(data: Any) -> Optional[dict]:
    if isinstance(data, list) and data:
        return data[0] if isinstance(data[0], dict) else None
    if isinstance(data, dict):
        # sometimes wrapped
        if "result" in data and isinstance(data["result"], list) and data["result"]:
            r0 = data["result"][0]
            return r0 if isinstance(r0, dict) else None
    return None


def _as_rows(data: Any) -> List[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("result"), list):
            return [x for x in data["result"] if isinstance(x, dict)]
    return []


def _get_any(row: dict, keys: List[str]) -> Any:
    for k in keys:
        if k in row and row.get(k) is not None:
            return row.get(k)
    return None


def answer_deterministic(parsed: ParsedQuery, data: Any) -> Optional[str]:
    intent = parsed.intent

    # -------------------------
    # top_salary
    # expects at least: name/id/salary in row
    # -------------------------
    if intent == "top_salary":
        row = _pick_first_row(data)
        if not row:
            return "ডেটা অপর্যাপ্ত।"
        salary = _get_any(row, ["BASICSALARY_NUM", "BASICSALARY", "BASIC_SALARY", "SALARY", "SALARY_NUM"])
        name = _get_any(row, ["EMPLOYEENAME", "EMP_NAME", "NAME"])
        empid = _get_any(row, ["EMPLOYEEID", "EMP_ID", "ID"])
        if salary is None or name is None:
            return "ডেটা অপর্যাপ্ত।"
        return f"সর্বোচ্চ বেসিক বেতন = {salary} টাকা (নাম: {name} | আইডি: {empid})"

    # -------------------------
    # salary_over
    # expects list of employees with salary
    # -------------------------
    if intent == "salary_over":
        rows = _as_rows(data)
        if not rows:
            return "ডেটা অপর্যাপ্ত।"
        # build up to 10 lines only (strict, no extra)
        lines = []
        for i, r in enumerate(rows[:10], 1):
            name = _get_any(r, ["EMPLOYEENAME", "EMP_NAME", "NAME"]) or ""
            empid = _get_any(r, ["EMPLOYEEID", "EMP_ID", "ID"]) or ""
            salary = _get_any(r, ["BASICSALARY_NUM", "BASICSALARY", "BASIC_SALARY", "SALARY", "SALARY_NUM"])
            if salary is None and not name:
                continue
            lines.append(f"{i}) {name} (আইডি: {empid}) — বেসিক: {salary}")
        return "\n".join(lines) if lines else "ডেটা অপর্যাপ্ত।"

    # -------------------------
    # employee
    # expects one row with employee details
    # -------------------------
    if intent == "employee":
        row = _pick_first_row(data)
        if not row:
            return "ডেটা অপর্যাপ্ত।"
        name = _get_any(row, ["EMPLOYEENAME", "EMP_NAME", "NAME"]) or ""
        empid = _get_any(row, ["EMPLOYEEID", "EMP_ID", "ID"]) or ""
        dept = _get_any(row, ["DEPARTMENTNAME", "DEPT_NAME", "DEPARTMENT", "DEPARTMENT_ID"]) or ""
        salary = _get_any(row, ["BASICSALARY_NUM", "BASICSALARY", "BASIC_SALARY", "SALARY", "SALARY_NUM"])
        join = _get_any(row, ["JOININGDATE", "JOIN_DATE", "DOJ"]) or ""
        if not (name or empid):
            return "ডেটা অপর্যাপ্ত।"
        # strict single line
        parts = [f"নাম: {name}", f"আইডি: {empid}"]
        if dept:
            parts.append(f"বিভাগ: {dept}")
        if salary is not None:
            parts.append(f"বেসিক: {salary}")
        if join:
            parts.append(f"যোগদান: {join}")
        return " | ".join(parts)

    # -------------------------
    # stats (department/branch)
    # expects rows with group + metrics
    # -------------------------
    if intent == "stats":
        rows = _as_rows(data)
        if not rows:
            return "ডেটা অপর্যাপ্ত।"
        # detect group column
        group = _get_any(rows[0], ["GROUP_NAME", "DEPARTMENTNAME", "DEPT_NAME", "BRANCHNAME", "BRANCH_NAME", "NAME"])
        # metric columns
        # support avg + count + total if present
        out_lines = []
        for i, r in enumerate(rows[:10], 1):
            g = _get_any(r, ["GROUP_NAME", "DEPARTMENTNAME", "DEPT_NAME", "BRANCHNAME", "BRANCH_NAME", "NAME"]) or ""
            avg_salary = _get_any(r, ["AVG_SALARY", "AVG_BASICSALARY", "AVG_BASIC", "AVERAGE_SALARY"])
            cnt = _get_any(r, ["EMP_COUNT", "COUNT_EMP", "TOTAL_EMP", "COUNT"])
            total = _get_any(r, ["TOTAL_SALARY", "SUM_SALARY", "TOTAL_BASIC"])
            bits = [f"{i}) {g}"]
            if avg_salary is not None:
                bits.append(f"গড় বেতন: {avg_salary}")
            if total is not None:
                bits.append(f"মোট বেতন: {total}")
            if cnt is not None:
                bits.append(f"কর্মচারী: {cnt}")
            out_lines.append(" — ".join(bits))
        return "\n".join(out_lines) if out_lines else "ডেটা অপর্যাপ্ত।"

    # Optional extras (if you enabled and NiFi supports)
    if intent == "lowest_salary":
        row = _pick_first_row(data)
        if not row:
            return "ডেটা অপর্যাপ্ত।"
        salary = _get_any(row, ["BASICSALARY_NUM", "BASICSALARY", "BASIC_SALARY", "SALARY", "SALARY_NUM"])
        name = _get_any(row, ["EMPLOYEENAME", "EMP_NAME", "NAME"])
        empid = _get_any(row, ["EMPLOYEEID", "EMP_ID", "ID"])
        if salary is None or name is None:
            return "ডেটা অপর্যাপ্ত।"
        return f"সর্বনিম্ন বেসিক বেতন = {salary} টাকা (নাম: {name} | আইডি: {empid})"

    if intent == "count_employees":
        row = _pick_first_row(data)
        if not row:
            return "ডেটা অপর্যাপ্ত।"
        cnt = _get_any(row, ["EMP_COUNT", "COUNT", "TOTAL", "TOTAL_EMP"])
        return f"মোট কর্মচারী = {cnt}" if cnt is not None else "ডেটা অপর্যাপ্ত।"

    return None


# =========================================================
# Aya strict fallback (last resort only)
# =========================================================
EXPLAIN_SYSTEM = """
তুমি একটি HR রিপোর্ট জেনারেটর।
তোমাকে শুধু ১টি (একটি) বাংলা উত্তর দিতে হবে। অন্য কিছু লিখবে না।

কঠোর নিয়ম:
- শুধুমাত্র বাংলায় লিখবে।
- অতিরিক্ত মন্তব্য/ব্যাখ্যা/শিরোনাম/বুলেট/লিস্ট লিখবে না।
- JSON-এ যা নেই তা বানাবে না।
- সংখ্যা/টাকা/আইডি ঠিক 그대로 রাখবে।
- JSON খালি হলে ঠিক এই বাক্যটি লিখবে: ডেটা অপর্যাপ্ত।
""".strip()


def _clean_strict_one_line_bn(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return "ডেটা অপর্যাপ্ত।"
    # keep only first non-empty line
    t = [x.strip() for x in t.splitlines() if x.strip()]
    t = t[0] if t else "ডেটা অপর্যাপ্ত।"
    # remove obvious meta/english markers if any
    bad = ["explanation", "rule", "context", "json", "intent", "oracle", "nifi", "system", "assistant"]
    if any(b in t.lower() for b in bad):
        return "ডেটা অপর্যাপ্ত।"
    # enforce Bangla-ish ending punctuation
    return t


def explain_with_aya(question: str, oracle_json: Any) -> str:
    user = (
        "প্রশ্ন: " + (question or "")
        + "\nডেটা(JSON):\n"
        + json.dumps(oracle_json, ensure_ascii=False)
        + "\nশুধু ১টি বাংলা উত্তর দাও।"
    )
    raw = _ollama_chat(EXPLAIN_SYSTEM, user)
    return _clean_strict_one_line_bn(raw)


# =========================================================
# Main API: /api/hr/ask  (called by auto router)
# =========================================================
@csrf_exempt
@require_POST
def hr_ask(request):
    """
    POST JSON:
    {
      "question": "...",
      "use_llm_parser": false,
      "explain": true
    }
    """
    try:
        body = json.loads((request.body or b"{}").decode("utf-8", errors="replace"))
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    question = (body.get("question") or "").strip()
    if not question:
        return JsonResponse({"ok": False, "error": "Missing 'question'."}, status=400)

    use_llm_parser = bool(body.get("use_llm_parser", False))
    want_explain = bool(body.get("explain", True))

    # 1) Parse
    parsed = parse_hr_question_rules(question)
    if parsed.intent == "unknown" and use_llm_parser:
        parsed = parse_hr_question_llm(question)

    # 2) Execute (NiFi/Oracle)
    data, err = execute_hr(parsed)
    if err:
        return JsonResponse(
            {
                "ok": False,
                "error": err,
                "parsed": {
                    "intent": parsed.intent,
                    "params": parsed.params,
                    "confidence": parsed.confidence,
                    "method": parsed.method,
                },
            },
            status=400,
        )

    # 3) Deterministic answer (preferred)
    answer = answer_deterministic(parsed, data)

    # 4) Aya fallback ONLY if deterministic couldn't produce
    if (not answer) and want_explain:
        try:
            answer = explain_with_aya(question, data)
        except Exception:
            answer = None

    if not answer:
        answer = "ডেটা অপর্যাপ্ত।"

    # IMPORTANT: answer is clean + strict; no extra commentary in answer
    return JsonResponse(
        {
            "ok": True,
            "answer": answer,
            "oracle_json": data,
            "parsed": {
                "intent": parsed.intent,
                "params": parsed.params,
                "confidence": parsed.confidence,
                "method": parsed.method,
            },
        }
    )
