from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set

import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST


# =========================================================
# Config: NiFi (Oracle truth)
# =========================================================
NIFI_BASE = os.getenv("NIFI_BASE", "http://192.168.1.139:8880").rstrip("/")
NIFI_HR_PATH = (os.getenv("NIFI_HR_PATH", "/hr/query") or "/hr/query").strip()
REQ_TIMEOUT = float(os.getenv("REQ_TIMEOUT", "10000"))

# UI safety
UI_MAX_LIST = int(os.getenv("UI_MAX_LIST", "2000"))


# =========================================================
# Config: Ollama (ONLY for intent classification, not HR facts)
# =========================================================
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
INTENT_MODEL = os.getenv("INTENT_MODEL", os.getenv("GEN_MODEL", "aya-23-8b:q5"))
OLLAMA_TIMEOUT_INTENT = float(os.getenv("OLLAMA_TIMEOUT_INTENT", "250"))
USE_LLM_INTENT_FALLBACK = (os.getenv("USE_LLM_INTENT_FALLBACK", "1") or "1").lower() not in ("0", "false", "no")


# =========================================================
# Allowed intents (must match NiFi flow)
# =========================================================
ALLOWED_HR_INTENTS: Set[str] = {
    "employeecode",
    "salary_over",
    "salary_between",
    "top_salary",
    "stats",
    "statsbydesignation",
    "employeebysal",
    "employee_lookup",
    "group_aggregate",
}

ALLOWED_EMP_FIELDS: Set[str] = {"details", "name", "age", "salary"}

ALLOWED_LOOKUP_BY: Set[str] = {"department", "designation", "branch", "gender"}
ALLOWED_GROUP_BY: Set[str] = {"department", "designation", "branch", "gender"}
ALLOWED_METRICS: Set[str] = {"salary", "employee"}
ALLOWED_AGGREGATIONS: Set[str] = {"avg", "count", "min", "max", "sum"}


# =========================================================
# RBAC groups (HR-only)
# =========================================================
GROUP_HR_HO = os.getenv("GROUP_HR_HO", "hr_ho")

HR_DEFAULT_GROUPS = "hr"
HR_ALLOWED_GROUPS = {
    g.strip()
    for g in (os.getenv("HR_ALLOWED_GROUPS", HR_DEFAULT_GROUPS) or HR_DEFAULT_GROUPS).split(",")
    if g.strip()
}


# =========================================================
# LLM prompt
# =========================================================
INTENT_SYSTEM_PROMPT = """
You are an intent classifier for an HR system.

Return JSON ONLY. No markdown. No extra text.

Allowed intents:
- employeecode
- salary_over
- salary_between
- top_salary
- stats
- statsbydesignation
- employeebysal
- employee_lookup
- group_aggregate

Rules:
1) Choose ONLY one intent above. Otherwise return:
   {"intent":"unknown","params":{}}

2) Parameter schemas:

- employeecode:
  {"EMPLOYEECODE":"24123","field":"name|age|salary|details"}

- salary_over:
  {"min_salary":40000}

- salary_between:
  {"min_salary":30000,"max_salary":50000}

- top_salary:
  {"limit":1}

- stats:
  {}

- statsbydesignation:
  {"limit":20}

- employeebysal:
  {"min_salary":30000,"max_salary":50000}

- employee_lookup:
  {"by":"department|designation|branch|gender","value":"HR"}

- group_aggregate:
  {
    "metric":"salary|employee",
    "aggregation":"avg|count|min|max|sum",
    "group_by":"department|designation|branch|gender"
  }

3) If unsure, return unknown.
4) Do not guess missing numbers or IDs.
5) For count questions like "employee count by department", use:
   {"intent":"group_aggregate","params":{"metric":"employee","aggregation":"count","group_by":"department"}}
6) For average salary by department, use:
   {"intent":"group_aggregate","params":{"metric":"salary","aggregation":"avg","group_by":"department"}}
7) For list employees in HR department, use:
   {"intent":"employee_lookup","params":{"by":"department","value":"HR"}}
""".strip()


# =========================================================
# Types
# =========================================================
@dataclass
class ParsedQuery:
    intent: str
    params: Dict[str, Any]
    confidence: float
    method: str


# =========================================================
# Helpers
# =========================================================
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _title_clean(text: Any) -> str:
    return ("" if text is None else str(text)).strip()


def _as_list(data: Any) -> list:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("result", "data", "rows", "items", "payload"):
            v = data.get(k)
            if isinstance(v, list):
                return v
        return [data]
    if isinstance(data, str):
        s = data.strip()
        if s.startswith("[") or s.startswith("{"):
            try:
                return _as_list(json.loads(s))
            except Exception:
                return []
    return []


def _user_group_names(user) -> Set[str]:
    try:
        if not user or not user.is_authenticated:
            return set()
        return set(user.groups.values_list("name", flat=True))
    except Exception:
        return set()


def _deny_if_not_hr_allowed(request) -> Optional[JsonResponse]:
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

    if getattr(user, "is_superuser", False):
        return None

    groups = _user_group_names(user)
    if groups & HR_ALLOWED_GROUPS:
        return None
    if GROUP_HR_HO in groups:
        return None

    return JsonResponse({"ok": False, "error": "Access denied (HR only)."}, status=403)


def _call_nifi(intent: str, params: Dict[str, Any]) -> Any:
    url = f"{NIFI_BASE}{NIFI_HR_PATH}"
    payload = {"intent": intent, "params": params}
    r = requests.post(url, json=payload, timeout=REQ_TIMEOUT, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


# =========================================================
# Ollama helpers
# =========================================================
def _strip_code_fences(s: str) -> str:
    t = (s or "").strip()
    t = re.sub(r"^\s*```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def _ollama_chat_raw(model: str, system_prompt: str, user_prompt: str, timeout: float) -> str:
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {
                "temperature": 0.0,
                "top_p": 0.7,
                "num_ctx": 4096,
                "num_predict": 220,
            },
        },
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    r.raise_for_status()
    j = r.json()
    return ((j.get("message") or {}).get("content") or "").strip()


def llm_resolve_intent(question: str) -> ParsedQuery:
    try:
        raw = _ollama_chat_raw(INTENT_MODEL, INTENT_SYSTEM_PROMPT, question, timeout=OLLAMA_TIMEOUT_INTENT)
        raw = _strip_code_fences(raw)
        obj = json.loads(raw) if raw else {}
        if not isinstance(obj, dict):
            return ParsedQuery("unknown", {}, 0.0, "llm")

        intent = str(obj.get("intent") or "unknown").strip()
        params = obj.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        if intent not in ALLOWED_HR_INTENTS:
            return ParsedQuery("unknown", {}, 0.0, "llm")

        if intent == "employeecode":
            field = str(params.get("field") or "details").strip().lower()
            if field not in ALLOWED_EMP_FIELDS:
                field = "details"
            params["field"] = field

            code = params.get("EMPLOYEECODE")
            if code is None:
                m = re.search(r"\b([0-9]{3,})\b", _normalize(question))
                if m:
                    code = m.group(1)
            code = str(code).strip() if code is not None else ""
            if not re.fullmatch(r"[0-9]{3,}", code):
                return ParsedQuery("unknown", {}, 0.0, "llm")
            params["EMPLOYEECODE"] = code

        elif intent == "salary_over":
            ms = params.get("min_salary")
            if ms is None:
                m = re.search(r"\b([0-9]{4,})\b", _normalize(question))
                ms = int(m.group(1)) if m else None
            try:
                params = {"min_salary": int(ms)}
            except Exception:
                return ParsedQuery("unknown", {}, 0.0, "llm")

        elif intent in {"salary_between", "employeebysal"}:
            mn = params.get("min_salary")
            mx = params.get("max_salary")
            nums = re.findall(r"\b([0-9]{4,})\b", _normalize(question))
            if mn is None and len(nums) >= 1:
                mn = nums[0]
            if mx is None and len(nums) >= 2:
                mx = nums[1]
            try:
                mn_i = int(mn)
                mx_i = int(mx)
                if mn_i > mx_i:
                    mn_i, mx_i = mx_i, mn_i
                params = {"min_salary": mn_i, "max_salary": mx_i}
            except Exception:
                return ParsedQuery("unknown", {}, 0.0, "llm")

        elif intent == "top_salary":
            params = {"limit": int(params.get("limit") or 1)}

        elif intent == "employee_lookup":
            by = str(params.get("by") or "").strip().lower()
            value = _title_clean(params.get("value"))
            if by not in ALLOWED_LOOKUP_BY or not value:
                return ParsedQuery("unknown", {}, 0.0, "llm")
            params = {"by": by, "value": value}

        elif intent == "group_aggregate":
            metric = str(params.get("metric") or "").strip().lower()
            aggregation = str(params.get("aggregation") or "").strip().lower()
            group_by = str(params.get("group_by") or "").strip().lower()
            if metric not in ALLOWED_METRICS or aggregation not in ALLOWED_AGGREGATIONS or group_by not in ALLOWED_GROUP_BY:
                return ParsedQuery("unknown", {}, 0.0, "llm")
            params = {
                "metric": metric,
                "aggregation": aggregation,
                "group_by": group_by,
            }

        return ParsedQuery(intent, params, 0.70, "llm")
    except Exception:
        return ParsedQuery("unknown", {}, 0.0, "llm")


# =========================================================
# Rule-based parsing
# =========================================================
def _is_name_only_question(question: str) -> bool:
    q = _normalize(question)
    name_terms = ["name", "full name", "employee name", "নাম", "name only"]
    return any(t in q for t in name_terms) and not any(
        x in q for x in ["detail", "details", "profile", "salary", "email", "mobile", "nid", "age", "বয়স", "বয়স"]
    )


def _is_salary_only_question(question: str) -> bool:
    q = _normalize(question)
    sal_terms = ["salary", "basic salary", "basicsalary", "basic", "বেতন", "বেসিক", "salary only"]
    return any(t in q for t in sal_terms) and not any(
        x in q for x in ["detail", "details", "profile", "email", "mobile", "nid", "designation", "dept", "branch", "age", "বয়স", "বয়স"]
    )


def _is_age_only_question(question: str) -> bool:
    q = _normalize(question)
    age_terms = ["age", "years old", "বয়স", "বয়স"]
    return any(t in q for t in age_terms) and not any(
        x in q for x in ["detail", "details", "profile", "salary", "email", "mobile", "nid"]
    )


def _extract_lookup_value(question: str, keywords: List[str]) -> str:
    q = question.strip()
    ql = _normalize(q)
    for kw in keywords:
        pattern = rf"{kw}\s+(.+)$"
        m = re.search(pattern, ql, flags=re.IGNORECASE)
        if m:
            raw = m.group(1).strip(" :,-")
            return raw
    return ""


def parse_hr_question_rules(question: str) -> ParsedQuery:
    q = _normalize(question)

    # employeecode
    pin_words = ["pin", "পিন", "employee id", "employeeid", "emp id", "id", "code", "কোড", "employeecode"]
    if any(w in q for w in pin_words) and any(x in q for x in ["employee", "emp", "কর্মচারী", "এমপ্লয়ি", "পিন", "কোড"]):
        m = re.search(r"\b([0-9]{3,})\b", q)
        if m:
            field = "details"
            if _is_name_only_question(question):
                field = "name"
            elif _is_age_only_question(question):
                field = "age"
            elif _is_salary_only_question(question):
                field = "salary"
            return ParsedQuery("employeecode", {"EMPLOYEECODE": m.group(1), "field": field}, 0.95, "rules")

    # salary between
    if (
        any(k in q for k in ["salary", "basic", "basicsalary", "বেতন", "বেসিক"])
        and any(k in q for k in ["between", "range", "from", "থেকে", "মধ্যে"])
    ):
        nums = re.findall(r"\b([0-9]{4,})\b", q)
        if len(nums) >= 2:
            mn = int(nums[0])
            mx = int(nums[1])
            if mn > mx:
                mn, mx = mx, mn
            return ParsedQuery("salary_between", {"min_salary": mn, "max_salary": mx}, 0.92, "rules")

    # salary over
    if any(k in q for k in ["salary", "basic", "basicsalary", "বেতন", "বেসিক"]) and any(
        k in q for k in ["over", "above", "greater", ">", "এর বেশি", "উপরে", "বেশি"]
    ):
        m = re.search(r"\b([0-9]{4,})\b", q)
        if m:
            return ParsedQuery("salary_over", {"min_salary": int(m.group(1))}, 0.90, "rules")

    # top salary
    if any(k in q for k in ["top salary", "highest salary", "সর্বোচ্চ বেতন", "সর্বোচ্চ বেসিক"]):
        return ParsedQuery("top_salary", {"limit": 1}, 0.90, "rules")

    # employee lookup by department
    if any(k in q for k in ["employee by department", "employees by department", "department wise employee", "list employee in department", "department hr", "বিভাগভিত্তিক কর্মী"]):
        value = _extract_lookup_value(question, ["department", "dept", "বিভাগ"])
        if value:
            return ParsedQuery("employee_lookup", {"by": "department", "value": value}, 0.90, "rules")

    # employee lookup by designation
    if any(k in q for k in ["employee by designation", "employees by designation", "designation wise employee", "list employee in designation", "পদবিভিত্তিক কর্মী"]):
        value = _extract_lookup_value(question, ["designation", "পদবি"])
        if value:
            return ParsedQuery("employee_lookup", {"by": "designation", "value": value}, 0.90, "rules")

    # employee lookup by branch
    if any(k in q for k in ["employee by branch", "employees by branch", "branch wise employee", "list employee in branch", "শাখাভিত্তিক কর্মী"]):
        value = _extract_lookup_value(question, ["branch", "শাখা"])
        if value:
            return ParsedQuery("employee_lookup", {"by": "branch", "value": value}, 0.90, "rules")

    # employee lookup by gender
    if any(k in q for k in ["employee by gender", "employees by gender", "gender wise employee", "লিঙ্গভিত্তিক কর্মী"]):
        value = _extract_lookup_value(question, ["gender", "লিঙ্গ"])
        if value:
            return ParsedQuery("employee_lookup", {"by": "gender", "value": value}, 0.90, "rules")

    # grouped analytics
    group_map = {
        "department": ["department", "dept", "বিভাগ"],
        "designation": ["designation", "পদবি"],
        "branch": ["branch", "শাখা"],
        "gender": ["gender", "লিঙ্গ"],
    }

    agg = None
    if any(k in q for k in ["average", "avg", "গড়", "গড়"]):
        agg = "avg"
    elif any(k in q for k in ["count", "total", "number of employee", "number of employees", "মোট", "সংখ্যা"]):
        agg = "count"
    elif any(k in q for k in ["max", "maximum", "highest", "সর্বোচ্চ"]):
        agg = "max"
    elif any(k in q for k in ["min", "minimum", "lowest", "সর্বনিম্ন"]):
        agg = "min"
    elif any(k in q for k in ["sum", "total salary sum", "মোট বেতন"]):
        agg = "sum"

    metric = None
    if any(k in q for k in ["salary", "basic", "basicsalary", "বেতন", "বেসিক"]):
        metric = "salary"
    elif any(k in q for k in ["employee", "employees", "staff", "কর্মী", "জনবল"]):
        metric = "employee"

    group_by = None
    for gb, keywords in group_map.items():
        if any(k in q for k in keywords):
            group_by = gb
            break

    if agg and group_by:
        if agg == "count" and not metric:
            metric = "employee"
        elif not metric:
            metric = "salary"
        return ParsedQuery(
            "group_aggregate",
            {"metric": metric, "aggregation": agg, "group_by": group_by},
            0.94,
            "rules",
        )

    # stats
    if q in {"stats", "hr stats", "employee stats", "summary", "সারাংশ", "পরিসংখ্যান"}:
        return ParsedQuery("stats", {}, 0.80, "rules")

    return ParsedQuery("unknown", {}, 0.0, "rules")


def resolve_intent(question: str) -> ParsedQuery:
    pq = parse_hr_question_rules(question)
    if pq.intent != "unknown":
        return pq
    if not USE_LLM_INTENT_FALLBACK:
        return pq
    return llm_resolve_intent(question)


# =========================================================
# Deterministic formatting
# =========================================================
def _deterministic_name(row: Dict[str, Any]) -> str:
    return _title_clean(row.get("EMPLOYEENAME") or row.get("EMPLOYEE_NAME") or row.get("NAME")) or "ডেটা অপর্যাপ্ত।"


def _deterministic_salary(row: Dict[str, Any]) -> str:
    v = row.get("BASICSALARY_NUM", row.get("BASICSALARY", ""))
    s = _title_clean(v)
    return s or "ডেটা অপর্যাপ্ত।"


def _parse_dob_to_date(dob_str: str) -> Optional[date]:
    s = _title_clean(dob_str)
    if not s:
        return None
    fmts = ["%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y"]
    for f in fmts:
        try:
            return datetime.strptime(s, f).date()
        except Exception:
            continue
    return None


def _calc_age_years(dob: date, today: Optional[date] = None) -> int:
    t = today or date.today()
    years = t.year - dob.year
    if (t.month, t.day) < (dob.month, dob.day):
        years -= 1
    return years


def _deterministic_age(row: Dict[str, Any]) -> str:
    dob_s = _title_clean(row.get("DATEOFBIRTH") or row.get("DOB"))
    dob = _parse_dob_to_date(dob_s)
    if not dob:
        return "ডেটা অপর্যাপ্ত।"
    return f"{_calc_age_years(dob)} বছর"


def format_employee_profile(row: Dict[str, Any]) -> str:
    name = _title_clean(row.get("EMPLOYEENAME"))
    code = _title_clean(row.get("EMPLOYEECODE"))
    empid = _title_clean(row.get("EMPLOYEEID"))
    desig = _title_clean(row.get("DESIGNATIONNAME") or row.get("DESIGNATION"))
    dept = _title_clean(row.get("DEPARTMENTNAME") or row.get("DEPARTMENTID"))
    branch = _title_clean(row.get("BRANCH_NAME") or row.get("BRANCHID"))
    join = _title_clean(row.get("JOININGDATE"))
    dob = _title_clean(row.get("DATEOFBIRTH") or row.get("DOB"))
    gender = _title_clean(row.get("GENDER"))
    salary = _title_clean(row.get("BASICSALARY_NUM") or row.get("BASICSALARY"))

    lines = [
        f"{name} (PIN/Code: {code}, EmployeeID: {empid})".strip(),
        f"পদবি: {desig} | বিভাগ: {dept}".strip(),
        f"শাখা: {branch}".strip(),
        f"Joining: {join} | DOB: {dob} | Gender: {gender}".strip(),
        f"Basic Salary: {salary}".strip(),
    ]
    return "\n".join([ln for ln in lines if ln]).strip() or "ডেটা অপর্যাপ্ত।"


def _pick_first(row: Dict[str, Any], candidates: List[str], default: str = "") -> str:
    for k in candidates:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()

    lower_map = {str(k).lower(): k for k in row.keys()}
    for ck in [c.lower() for c in candidates]:
        real_k = lower_map.get(ck)
        if real_k:
            v = row.get(real_k)
            if v is not None and str(v).strip():
                return str(v).strip()
    return default


def _pick_pin(row: Dict[str, Any]) -> str:
    return _pick_first(
        row,
        [
            "EMPLOYEECODE", "EMPLOYEE_CODE", "EMP_CODE",
            "PIN", "PINCODE", "CODE",
            "EMPLOYEENO", "EMPLOYEE_NO", "EMP_NO",
            "EMPLOYEENUMBER", "EMPLOYEE_NUMBER",
        ],
    )


def _pick_employeeid(row: Dict[str, Any]) -> str:
    return _pick_first(row, ["EMPLOYEEID", "EMPLOYEE_ID", "EMP_ID", "EMPID"])


def _pick_department(row: Dict[str, Any]) -> str:
    return _pick_first(
        row,
        ["DEPARTMENTNAME", "DEPARTMENT_NAME", "DEPARTMENT", "DEPTNAME", "DEPT_NAME", "DEPT"],
        "Unknown",
    )


def _pick_designation(row: Dict[str, Any]) -> str:
    return _pick_first(
        row,
        ["DESIGNATIONNAME", "DESIGNATION_NAME", "DESIGNATION", "TITLE"],
        "Unknown",
    )


def _pick_branch(row: Dict[str, Any]) -> str:
    return _pick_first(
        row,
        ["BRANCH_NAME", "BRANCHNAME", "BRANCH", "BRANCHID"],
        "Unknown",
    )


def _pick_gender(row: Dict[str, Any]) -> str:
    return _pick_first(row, ["GENDER", "SEX"], "Unknown")


def _pick_group_value(row: Dict[str, Any], group_by: str) -> str:
    if group_by == "department":
        return _pick_department(row)
    if group_by == "designation":
        return _pick_designation(row)
    if group_by == "branch":
        return _pick_branch(row)
    if group_by == "gender":
        return _pick_gender(row)
    return "Unknown"


def _pick_aggregate_value(row: Dict[str, Any], aggregation: str, metric: str) -> str:
    agg = aggregation.lower()
    met = metric.lower()

    candidates = []
    if agg == "avg" and met == "salary":
        candidates = ["AVG_SALARY", "AVG_SALARY_NUM", "AVERAGE_SALARY", "AVG_BASIC", "AVG_BASICSALARY", "AVG"]
    elif agg == "count":
        candidates = ["TOTAL_EMPLOYEE", "TOTAL_EMPLOYEES", "EMPLOYEE_COUNT", "TOTAL_COUNT", "CNT", "COUNT", "TOTAL"]
    elif agg == "max" and met == "salary":
        candidates = ["MAX_SALARY", "MAX_SALARY_NUM", "MAX_BASIC", "MAX"]
    elif agg == "min" and met == "salary":
        candidates = ["MIN_SALARY", "MIN_SALARY_NUM", "MIN_BASIC", "MIN"]
    elif agg == "sum" and met == "salary":
        candidates = ["SUM_SALARY", "SUM_SALARY_NUM", "TOTAL_SALARY", "SUM_BASIC", "SUM"]

    return _pick_first(row, candidates, "")


def _format_employee_lookup(rows: List[Dict[str, Any]], by: str, value: str) -> str:
    lines = [f"Employee list by {by}: {value}"]
    count = 0
    for r in rows[:UI_MAX_LIST]:
        if not isinstance(r, dict):
            continue
        name = _pick_first(r, ["EMPLOYEENAME", "EMPLOYEE_NAME", "NAME"], "Unknown")
        pin = _pick_pin(r)
        desig = _pick_designation(r)
        dept = _pick_department(r)
        branch = _pick_branch(r)
        lines.append(f"- {name} | PIN: {pin} | Designation: {desig} | Department: {dept} | Branch: {branch}")
        count += 1

    if count == 0:
        return "ডেটা পাওয়া যায়নি।"
    return "\n".join(lines)


def _format_group_aggregate(rows: List[Dict[str, Any]], metric: str, aggregation: str, group_by: str) -> str:
    title = f"{aggregation.upper()} {metric.upper()} by {group_by.upper()}:"
    lines = [title]

    count = 0
    for r in rows[:UI_MAX_LIST]:
        if not isinstance(r, dict):
            continue
        group_value = _pick_group_value(r, group_by)
        agg_value = _pick_aggregate_value(r, aggregation, metric)
        if agg_value:
            lines.append(f"- {group_value}: {agg_value}")
            count += 1

    if count == 0:
        return "ডেটা পাওয়া গেছে কিন্তু aggregate field পাওয়া যায়নি।"
    return "\n".join(lines)


# =========================================================
# NiFi param normalization
# =========================================================
def _params_for_nifi(intent: str, params: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(params or {})

    if intent == "salary_over":
        ms = int(p.get("min_salary") or 0)
        return {
            "min_salary": ms,
            "MIN_SALARY": ms,
            "amount": ms,
            "AMOUNT": ms,
            "salary": ms,
            "SALARY": ms,
        }

    if intent in {"salary_between", "employeebysal"}:
        mn = int(p.get("min_salary") or 0)
        mx = int(p.get("max_salary") or 0)
        return {
            "min_salary": mn,
            "max_salary": mx,
            "MIN_SALARY": mn,
            "MAX_SALARY": mx,
        }

    if intent == "top_salary":
        limit = int(p.get("limit") or 1)
        return {"limit": limit, "LIMIT": limit}

    if intent == "employeecode":
        code = str(p.get("EMPLOYEECODE") or "").strip()
        return {"EMPLOYEECODE": code}

    if intent == "employee_lookup":
        by = str(p.get("by") or "").strip().lower()
        value = _title_clean(p.get("value"))
        return {"by": by, "value": value, "BY": by, "VALUE": value}

    if intent == "group_aggregate":
        metric = str(p.get("metric") or "").strip().lower()
        aggregation = str(p.get("aggregation") or "").strip().lower()
        group_by = str(p.get("group_by") or "").strip().lower()
        return {
            "metric": metric,
            "aggregation": aggregation,
            "group_by": group_by,
            "METRIC": metric,
            "AGGREGATION": aggregation,
            "GROUP_BY": group_by,
        }

    return p


# =========================================================
# Main API
# =========================================================
@csrf_exempt
@require_POST
def hr_ask(request):
    deny = _deny_if_not_hr_allowed(request)
    if deny:
        return deny

    try:
        body = json.loads((request.body or b"{}").decode("utf-8", errors="replace"))
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    question = (body.get("question") or "").strip()
    if not question:
        return JsonResponse({"ok": False, "error": "Missing 'question'."}, status=400)

    parsed = resolve_intent(question)
    if parsed.intent not in ALLOWED_HR_INTENTS:
        return JsonResponse(
            {
                "ok": False,
                "error": "Unsupported or unknown HR intent.",
                "debug": {
                    "question": question,
                    "resolved_intent": parsed.intent,
                    "method": parsed.method,
                    "allowed_intents": sorted(list(ALLOWED_HR_INTENTS)),
                },
            },
            status=400,
        )

    nifi_params = _params_for_nifi(parsed.intent, parsed.params)

    try:
        data = _call_nifi(parsed.intent, nifi_params)
    except requests.HTTPError as e:
        return JsonResponse({"ok": False, "error": f"NiFi HTTP error: {str(e)}", "intent": parsed.intent}, status=502)
    except requests.Timeout:
        return JsonResponse({"ok": False, "error": "NiFi timeout.", "intent": parsed.intent}, status=504)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"NiFi call failed: {str(e)}", "intent": parsed.intent}, status=502)

    rows = _as_list(data)
    if not rows:
        return JsonResponse(
            {
                "ok": True,
                "answer": "ডেটা পাওয়া যায়নি।",
                "oracle_json": data,
                "parsed": {"intent": parsed.intent, "params": parsed.params, "method": parsed.method},
            }
        )

    # employeecode
    if parsed.intent == "employeecode":
        row = rows[0] if isinstance(rows[0], dict) else {}
        field = str(parsed.params.get("field") or "details").strip().lower()
        if field not in ALLOWED_EMP_FIELDS:
            field = "details"

        if field == "name":
            answer = _deterministic_name(row)
        elif field == "age":
            answer = _deterministic_age(row)
        elif field == "salary":
            answer = _deterministic_salary(row)
        else:
            answer = format_employee_profile(row)

        return JsonResponse(
            {
                "ok": True,
                "answer": answer,
                "oracle_json": data,
                "parsed": {"intent": parsed.intent, "params": parsed.params, "method": parsed.method},
            }
        )

    # salary_over
    if parsed.intent == "salary_over":
        amount = int(parsed.params.get("min_salary") or 0)
        pins: List[str] = []
        employeeids: List[str] = []
        names: List[str] = []

        for r in rows:
            if not isinstance(r, dict):
                continue
            pin = _pick_pin(r)
            eid = _pick_employeeid(r)
            nm = _pick_first(r, ["EMPLOYEENAME", "EMPLOYEE_NAME", "NAME"], "")

            if pin:
                pins.append(pin)
            if eid:
                employeeids.append(eid)
            if nm:
                names.append(nm)

        seen = set()
        pins = [p for p in pins if not (p in seen or seen.add(p))]
        seen = set()
        employeeids = [e for e in employeeids if not (e in seen or seen.add(e))]
        seen = set()
        names = [n for n in names if not (n in seen or seen.add(n))]

        total = len(rows)
        pins_out = pins[:UI_MAX_LIST]
        empids_out = employeeids[:UI_MAX_LIST]
        names_out = names[:UI_MAX_LIST]

        answer = f"Basic Salary {amount} এর বেশি এমন মোট {total} জন রেকর্ড পাওয়া গেছে।"

        try:
            request.session["last_hr_salary_over"] = {
                "min_salary": amount,
                "total": total,
                "pins": pins_out,
                "employeeids": empids_out,
                "names": names_out,
            }
            request.session.modified = True
        except Exception:
            pass

        return JsonResponse(
            {
                "ok": True,
                "answer": answer,
                "total": total,
                "pins": pins_out,
                "employeeids": empids_out,
                "names": names_out,
                "oracle_json": data,
                "parsed": {"intent": parsed.intent, "params": parsed.params, "method": parsed.method},
            }
        )

    # salary_between / employeebysal
    if parsed.intent in {"salary_between", "employeebysal"}:
        mn = int(parsed.params.get("min_salary") or 0)
        mx = int(parsed.params.get("max_salary") or 0)
        total = len(rows)
        answer = f"Basic Salary {mn} থেকে {mx} এর মধ্যে মোট {total} জন রেকর্ড পাওয়া গেছে।"
        return JsonResponse(
            {
                "ok": True,
                "answer": answer,
                "rows": [r for r in rows if isinstance(r, dict)][:UI_MAX_LIST],
                "oracle_json": data,
                "parsed": {"intent": parsed.intent, "params": parsed.params, "method": parsed.method},
            }
        )

    # top_salary
    if parsed.intent == "top_salary":
        r0 = rows[0] if isinstance(rows[0], dict) else {}
        name = _pick_first(r0, ["EMPLOYEENAME", "EMPLOYEE_NAME", "NAME"], "")
        code = _pick_pin(r0) or _pick_first(r0, ["EMPLOYEECODE"], "")
        sal = r0.get("BASICSALARY_NUM", r0.get("BASICSALARY", ""))
        answer = f"সর্বোচ্চ বেসিক বেতন: {sal} — {name} (Code: {code})" if (name or code or sal) else "ডেটা পাওয়া গেছে।"
        return JsonResponse(
            {
                "ok": True,
                "answer": answer,
                "oracle_json": data,
                "parsed": {"intent": parsed.intent, "params": parsed.params, "method": parsed.method},
            }
        )

    # employee_lookup
    if parsed.intent == "employee_lookup":
        by = str(parsed.params.get("by") or "")
        value = _title_clean(parsed.params.get("value"))
        safe_rows = [r for r in rows if isinstance(r, dict)]
        answer = _format_employee_lookup(safe_rows, by, value)
        return JsonResponse(
            {
                "ok": True,
                "answer": answer,
                "rows": safe_rows[:UI_MAX_LIST],
                "oracle_json": data,
                "parsed": {"intent": parsed.intent, "params": parsed.params, "method": parsed.method},
            }
        )

    # group_aggregate
    if parsed.intent == "group_aggregate":
        metric = str(parsed.params.get("metric") or "")
        aggregation = str(parsed.params.get("aggregation") or "")
        group_by = str(parsed.params.get("group_by") or "")
        safe_rows = [r for r in rows if isinstance(r, dict)]
        answer = _format_group_aggregate(safe_rows, metric, aggregation, group_by)
        return JsonResponse(
            {
                "ok": True,
                "answer": answer,
                "rows": safe_rows[:UI_MAX_LIST],
                "oracle_json": data,
                "parsed": {"intent": parsed.intent, "params": parsed.params, "method": parsed.method},
            }
        )

    # stats / statsbydesignation / others
    return JsonResponse(
        {
            "ok": True,
            "answer": "ডেটা পাওয়া গেছে।",
            "oracle_json": data,
            "parsed": {"intent": parsed.intent, "params": parsed.params, "method": parsed.method},
        }
    )
