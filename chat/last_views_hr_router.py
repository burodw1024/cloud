# chat/views_hr_router.py
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
REQ_TIMEOUT = float(os.getenv("REQ_TIMEOUT", "600"))

# UI safety (even if backend has "no limit")
UI_MAX_LIST = int(os.getenv("UI_MAX_LIST", "2000"))  # how many pins/names to return to UI


# =========================================================
# Config: Ollama (ONLY for intent classification, not HR facts)
# =========================================================
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")

INTENT_MODEL = os.getenv("INTENT_MODEL", os.getenv("GEN_MODEL", "aya-23-8b"))
OLLAMA_TIMEOUT_INTENT = float(os.getenv("OLLAMA_TIMEOUT_INTENT", "250"))

USE_LLM_INTENT_FALLBACK = (os.getenv("USE_LLM_INTENT_FALLBACK", "1") or "1").lower() not in ("0", "false", "no")


# =========================================================
# Allowed intents (MUST match your NiFi flow)
# =========================================================
ALLOWED_HR_INTENTS: Set[str] = {
    "employeecode",
    "salary_over",
    "top_salary",
    "stats",
    "statsbydesignation",
    "employeebysal",
}

ALLOWED_EMP_FIELDS: Set[str] = {"details", "name", "age", "salary"}


# =========================================================
# RBAC groups (HR-only)
# =========================================================
#GROUP_HR_HO = os.getenv("GROUP_HR_HO", "HR_HO")
#HR_ALLOWED_GROUPS = set((os.getenv("HR_ALLOWED_GROUPS", "DEPT_HR,core") or "DEPT_HR,HEAD_OFFICE").split(","))
#HR_ALLOWED_GROUPS = {g.strip() for g in HR_ALLOWED_GROUPS if g.strip()}

# =========================================================
# RBAC groups (HR-only)
# =========================================================

# Default HR group name
HR_DEFAULT_GROUPS = "hr"

# Load allowed groups from env or use default
HR_ALLOWED_GROUPS = {
    g.strip()
    for g in (os.getenv("HR_ALLOWED_GROUPS", HR_DEFAULT_GROUPS) or HR_DEFAULT_GROUPS).split(",")
    if g.strip()
}

# =========================================================
# LLM prompt (NO LIMIT REQUIRED)
# =========================================================
INTENT_SYSTEM_PROMPT = """
You are an intent classifier for an HR system.

Return JSON ONLY (no markdown, no extra text).

Allowed intents:
- employeecode
- salary_over
- top_salary
- stats
- statsbydesignation
- employeebysal

Rules:
1) Choose ONLY one intent from the list, else intent="unknown".
2) Extract params if present. Use these parameter names:
   - employeecode: {"EMPLOYEECODE":"24123","field":"name|age|salary|details"}
   - salary_over: {"min_salary":40000}
   - top_salary: {"limit":1}
   - stats: {}
   - statsbydesignation: {"limit":20}
   - employeebysal: {"min_salary":30000,"max_salary":50000}
3) If unsure, output {"intent":"unknown","params":{}}.
4) Do not guess missing numbers/IDs.
5) "limit" is OPTIONAL. If missing, do NOT invent it.
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
# Basic helpers
# =========================================================
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


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
# LLM helpers (JSON-only parsing)
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
            "options": {"temperature": 0.0, "top_p": 0.7, "num_ctx": 4096, "num_predict": 220},
        },
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    r.raise_for_status()
    j = r.json()
    return ((j.get("message") or {}).get("content") or "").strip()


def llm_resolve_intent(question: str) -> ParsedQuery:
    """
    LLM is used ONLY to classify intent + extract params.
    It does NOT answer HR facts.
    """
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

        # employeecode normalize
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

        # salary_over normalize (NO default limit)
        if intent == "salary_over":
            ms = params.get("min_salary")
            if ms is None:
                m = re.search(r"\b([0-9]{4,})\b", _normalize(question))
                ms = int(m.group(1)) if m else None
            try:
                ms_i = int(ms)
            except Exception:
                return ParsedQuery("unknown", {}, 0.0, "llm")
            params = {"min_salary": ms_i}

        # top_salary normalize
        if intent == "top_salary":
            params = {"limit": int(params.get("limit") or 1)}

        return ParsedQuery(intent, params, 0.70, "llm")
    except Exception:
        return ParsedQuery("unknown", {}, 0.0, "llm")


# =========================================================
# Rules-based parsing (fast + safe)
# =========================================================
def _is_name_only_question(question: str) -> bool:
    q = _normalize(question)
    name_terms = ["name", "full name", "employee name", "নাম", "name only"]
    if any(t in q for t in name_terms):
        if any(x in q for x in ["detail", "details", "profile", "salary", "email", "mobile", "nid", "age", "বয়স", "বয়স"]):
            return False
        return True
    return False


def _is_salary_only_question(question: str) -> bool:
    q = _normalize(question)
    sal_terms = ["salary", "basic salary", "basicsalary", "basic", "বেতন", "বেসিক", "salary only"]
    if any(t in q for t in sal_terms):
        if any(x in q for x in ["detail", "details", "profile", "email", "mobile", "nid", "designation", "dept", "branch", "age", "বয়স", "বয়স"]):
            return False
        return True
    return False


def _is_age_only_question(question: str) -> bool:
    q = _normalize(question)
    age_terms = ["age", "years old", "বয়স", "বয়স"]
    if any(t in q for t in age_terms):
        if any(x in q for x in ["detail", "details", "profile", "salary", "email", "mobile", "nid"]):
            return False
        return True
    return False


def parse_hr_question_rules(question: str) -> ParsedQuery:
    q = _normalize(question)

    # employeecode
    pin_words = ["pin", "পিন", "employee id", "employeeid", "employeed id", "emp id", "id", "code", "কোড", "employeecode"]
    if any(w in q for w in pin_words) and any(x in q for x in ["employee", "employeed", "emp", "কর্মচারী", "এমপ্লয়ি", "পিন", "কোড"]):
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

    # salary_over (NO limit)
    if any(k in q for k in ["salary", "basic", "basicsalary", "বেতন", "বেসিক"]) and any(
        k in q for k in ["over", "above", "greater", ">", "এর বেশি", "উপরে", "বেশি"]
    ):
        m = re.search(r"\b([0-9]{4,})\b", q)
        if m:
            return ParsedQuery("salary_over", {"min_salary": int(m.group(1))}, 0.90, "rules")

    # top_salary
    if any(k in q for k in ["top salary", "highest salary", "সর্বোচ্চ বেতন", "সর্বোচ্চ বেসিক"]):
        return ParsedQuery("top_salary", {"limit": 1}, 0.90, "rules")

    return ParsedQuery("unknown", {}, 0.0, "rules")


def resolve_intent(question: str) -> ParsedQuery:
    pq = parse_hr_question_rules(question)
    if pq.intent != "unknown":
        return pq
    if not USE_LLM_INTENT_FALLBACK:
        return pq
    return llm_resolve_intent(question)


# =========================================================
# Deterministic formatting (facts from Oracle JSON only)
# =========================================================
def _deterministic_name(row: Dict[str, Any]) -> str:
    return (row.get("EMPLOYEENAME") or row.get("EMPLOYEE_NAME") or row.get("NAME") or "").strip() or "ডেটা অপর্যাপ্ত।"


def _deterministic_salary(row: Dict[str, Any]) -> str:
    v = row.get("BASICSALARY_NUM", row.get("BASICSALARY", ""))
    s = ("" if v is None else str(v)).strip()
    return s or "ডেটা অপর্যাপ্ত।"


def _parse_dob_to_date(dob_str: str) -> Optional[date]:
    s = (dob_str or "").strip()
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
    dob_s = (row.get("DATEOFBIRTH") or row.get("DOB") or "").strip()
    dob = _parse_dob_to_date(dob_s)
    if not dob:
        return "ডেটা অপর্যাপ্ত।"
    return f"{_calc_age_years(dob)} বছর"


def format_employee_profile(row: Dict[str, Any]) -> str:
    name = (row.get("EMPLOYEENAME") or "").strip()
    code = (row.get("EMPLOYEECODE") or "").strip()
    empid = (row.get("EMPLOYEEID") or "").strip()

    desig = (row.get("DESIGNATIONNAME") or row.get("DESIGNATION") or "").strip()
    dept = (row.get("DEPARTMENTNAME") or row.get("DEPARTMENTID") or "").strip()
    branch = (row.get("BRANCH_NAME") or row.get("BRANCHID") or "").strip()

    join = (row.get("JOININGDATE") or "").strip()
    dob = (row.get("DATEOFBIRTH") or row.get("DOB") or "").strip()
    gender = (row.get("GENDER") or "").strip()

    salary = (row.get("BASICSALARY_NUM") or row.get("BASICSALARY") or "").strip()

    lines = [
        f"{name} (PIN/Code: {code}, EmployeeID: {empid})".strip(),
        f"পদবি: {desig} | বিভাগ: {dept}".strip(),
        f"শাখা: {branch}".strip(),
        f"Joining: {join} | DOB: {dob} | Gender: {gender}".strip(),
        f"Basic Salary: {salary}".strip(),
    ]
    return "\n".join([ln for ln in lines if ln]).strip() or "ডেটা অপর্যাপ্ত।"


# =========================================================
# NiFi param normalization (NO limit)
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

    if intent == "top_salary":
        limit = int(p.get("limit") or 1)
        return {"limit": limit, "LIMIT": limit}

    if intent == "employeecode":
        code = str(p.get("EMPLOYEECODE") or "").strip()
        return {"EMPLOYEECODE": code}

    return p


def _pick_pin(row: Dict[str, Any]) -> str:
    # strong: handles different key spellings + case-insensitive
    candidates = [
        "EMPLOYEECODE", "EMPLOYEE_CODE", "EMP_CODE",
        "PIN", "PINCODE", "CODE",
        "EMPLOYEENO", "EMPLOYEE_NO", "EMP_NO",
        "EMPLOYEENUMBER", "EMPLOYEE_NUMBER",
    ]

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

    return ""


def _pick_employeeid(row: Dict[str, Any]) -> str:
    candidates = ["EMPLOYEEID", "EMPLOYEE_ID", "EMP_ID", "EMPID"]
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

    return ""


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
        return JsonResponse({"ok": False, "error": "Unsupported or unknown HR intent."}, status=400)

    nifi_params = _params_for_nifi(parsed.intent, parsed.params)

    try:
        data = _call_nifi(parsed.intent, nifi_params)
    except requests.HTTPError as e:
        return JsonResponse({"ok": False, "error": f"NiFi HTTP error: {str(e)}"}, status=502)
    except requests.Timeout:
        return JsonResponse({"ok": False, "error": "NiFi timeout."}, status=504)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"NiFi call failed: {str(e)}"}, status=502)

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

    # -----------------------------------------------------
    # employeecode
    # -----------------------------------------------------
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

    # -----------------------------------------------------
    # salary_over (NO LIMIT REQUIRED)
    # -----------------------------------------------------
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
            nm = (r.get("EMPLOYEENAME") or r.get("EMPLOYEE_NAME") or r.get("NAME") or "").strip()

            if pin:
                pins.append(pin)
            if eid:
                employeeids.append(eid)
            if nm:
                names.append(nm)

        # unique keep order
        seen = set()
        pins = [p for p in pins if not (p in seen or seen.add(p))]
        seen = set()
        employeeids = [e for e in employeeids if not (e in seen or seen.add(e))]
        seen = set()
        names = [n for n in names if not (n in seen or seen.add(n))]

        total = len(rows)

        # UI-safe truncation
        pins_out = pins[:UI_MAX_LIST]
        empids_out = employeeids[:UI_MAX_LIST]
        names_out = names[:UI_MAX_LIST]

        answer = f"Basic Salary {amount} এর বেশি এমন মোট {total} জন রেকর্ড পাওয়া গেছে।"

        # store for follow-up (store truncated to avoid huge session)
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

    # -----------------------------------------------------
    # top_salary
    # -----------------------------------------------------
    if parsed.intent == "top_salary":
        r0 = rows[0] if isinstance(rows[0], dict) else {}
        name = (r0.get("EMPLOYEENAME") or "").strip()
        code = _pick_pin(r0) or (r0.get("EMPLOYEECODE") or "").strip()
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

    # -----------------------------------------------------
    # Others
    # -----------------------------------------------------
    return JsonResponse(
        {
            "ok": True,
            "answer": "ডেটা পাওয়া গেছে।",
            "oracle_json": data,
            "parsed": {"intent": parsed.intent, "params": parsed.params, "method": parsed.method},
        }
    )
