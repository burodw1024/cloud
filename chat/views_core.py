# views_core.py
# Core Loan NL -> Dynamic Plan -> Oracle 11g SQL -> NiFi Exec
# - Rule-based planner by default
# - Optional LLM plan refinement with USE_LLM_PLANNER=1
# - Oracle 11g limit via ROWNUM wrapper
# - Safe allow-list from Qdrant core_schema index ("TABLE:" / "COL:" lines)
# - Bangla user-friendly analytical answer by default
# - Branch code / branch name support when available in schema
# - SQL/debug output only when debug=true or CORE_DEBUG=1
# - Last working NiFi URL kept: http://192.168.1.139:8888/core/query

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from chat.app_config import APP_CONFIG


JsonT = Union[Dict[str, Any], List[Any]]


# =========================================================
# RBAC - Core finance
# =========================================================
CORE_ALLOWED_GROUPS: Set[str] = {
    "core",
}

ENFORCE_STAFF_ONLY = (
    os.getenv("ENFORCE_STAFF_ONLY", "1") or "1"
).lower() not in ("0", "false", "no")


def _user_group_names(user) -> Set[str]:
    try:
        if not user or not user.is_authenticated:
            return set()
        return set(user.groups.values_list("name", flat=True))
    except Exception:
        return set()


def _deny_core(request) -> Optional[JsonResponse]:
    user = getattr(request, "user", None)

    if not user or not user.is_authenticated:
        return JsonResponse(
            {"ok": False, "error": "Authentication required."},
            status=401,
        )

    if getattr(user, "is_superuser", False):
        return None

    if ENFORCE_STAFF_ONLY and not getattr(user, "is_staff", False):
        return JsonResponse(
            {"ok": False, "error": "Staff access required."},
            status=403,
        )

    groups = _user_group_names(user)

    if groups & CORE_ALLOWED_GROUPS:
        return None

    return JsonResponse(
        {
            "ok": False,
            "error": "Access denied.",
            "required_groups": sorted(CORE_ALLOWED_GROUPS),
            "your_groups": sorted(groups)[:50],
        },
        status=403,
    )


# =========================================================
# Env helpers
# =========================================================
def _env(name: str, default: str) -> str:
    return (os.getenv(name, default) or "").strip()


def _env_float(name: str, default: str) -> float:
    try:
        return float(_env(name, default))
    except Exception:
        return float(default)


def _env_int(name: str, default: str) -> int:
    try:
        return int(_env(name, default))
    except Exception:
        return int(default)


def _clean(v: Any) -> str:
    return ("" if v is None else str(v)).strip()


def _strip_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _safe_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default

    if isinstance(v, bool):
        return v

    s = str(v).strip().lower()

    if s in ("1", "true", "yes", "y", "on"):
        return True

    if s in ("0", "false", "no", "n", "off"):
        return False

    return default


def _safe_limit(v: Any, default_limit: int, max_limit: int) -> int:
    try:
        lim = int(v)
    except Exception:
        lim = default_limit

    if lim <= 0:
        lim = default_limit

    if lim > max_limit:
        lim = max_limit

    return lim


# =========================================================
# Runtime helpers
# =========================================================
def _get_qdrant_url() -> str:
    """
    Runtime Qdrant URL resolver.
    Qdrant primary/secondary IP should stay in chat/app_config.py or environment variables.
    """
    try:
        return _strip_url(APP_CONFIG.qdrant.get_url(check_health=True))
    except Exception:
        return _strip_url(getattr(APP_CONFIG.qdrant, "qdrant_url", ""))


def _qdrant_headers() -> Dict[str, str]:
    api_key = getattr(APP_CONFIG.qdrant, "api_key", "") or ""
    if api_key:
        return {"api-key": api_key}
    return {}


# =========================================================
# Config
# =========================================================
@dataclass(frozen=True)
class Cfg:
    # Keep last working Ollama URL as fallback
    OLLAMA_URL: str = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")

    # Qdrant from central config with failover
    QDRANT_URL: str = APP_CONFIG.qdrant.get_url(check_health=True)

    # Optional planner/refiner
    GEN_MODEL: str = _env("GEN_MODEL", "aya-23-8b")
    USE_LLM_PLANNER: bool = (
        _env("USE_LLM_PLANNER", "0") or "0"
    ).lower() in ("1", "true", "yes")

    EMBED_MODEL: str = _env("EMBED_MODEL", "nomic-embed-text:latest")

    # Core Qdrant collection
    CORE_COLLECTION: str = _env("CORE_COLLECTION", "buro_core").strip() or "buro_core"
    QDRANT_VECTOR_NAME: str = (
        _env(
            "QDRANT_VECTOR_NAME",
            getattr(APP_CONFIG.qdrant, "vector_name", "default"),
        ).strip()
        or "default"
    )

    QDRANT_TIMEOUT: float = _env_float(
        "QDRANT_TIMEOUT",
        str(getattr(APP_CONFIG.qdrant, "timeout", 30.0)),
    )

    GEN_TIMEOUT: float = _env_float("GEN_TIMEOUT", "120")
    EMBED_TIMEOUT: float = _env_float("EMBED_TIMEOUT", "60")

    # Last working NiFi Core endpoint
    NIFI_CORE_URL: str = _env(
        "NIFI_CORE_URL",
        "http://192.168.1.139:8888/core/query",
    ).strip()

    NIFI_TIMEOUT: float = _env_float("NIFI_TIMEOUT", "300")

    DEFAULT_LIMIT: int = _env_int("CORE_DEFAULT_LIMIT", "200")
    MAX_LIMIT: int = _env_int("CORE_MAX_LIMIT", "2000")
    PREVIEW_ROWS: int = _env_int("CORE_PREVIEW_ROWS", "10")

    SCHEMA_SCROLL_LIMIT: int = _env_int("SCHEMA_SCROLL_LIMIT", "1200")

    DEBUG: bool = (
        _env("CORE_DEBUG", "0") or "0"
    ).lower() in ("1", "true", "yes")


CFG = Cfg()


# =========================================================
# HTTP helpers
# =========================================================
def _json_body(request) -> Dict[str, Any]:
    try:
        raw = (request.body or b"{}").decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _http_json(
    method: str,
    url: str,
    *,
    timeout: float,
    json_body: Optional[dict] = None,
    headers: Optional[Dict[str, str]] = None,
) -> JsonT:
    final_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if headers:
        final_headers.update(headers)

    r = requests.request(
        method,
        url,
        json=json_body,
        timeout=timeout,
        headers=final_headers,
    )

    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:800]}")

    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Invalid JSON from {url}: {e}. Body={r.text[:800]}")


# =========================================================
# Optional LLM planner - Aya JSON plan only
# =========================================================
SYSTEM_PROMPT_CORE_PLAN = (
    "You are an Oracle 11g SQL query planner.\n"
    "You MUST use ONLY identifiers that appear in SCHEMA CONTEXT.\n"
    "Return ONLY valid JSON. No markdown. No explanations.\n"
    "Rules:\n"
    "- If user asks DISTINCT / unique / remove duplicates / no duplicate rows, set distinct=true.\n"
    "- Do NOT invent columns.\n"
    "- Use only SCHEMA.TABLE and SCHEMA.TABLE.COL.\n"
    "JSON format:\n"
    "{"
    '"distinct":false,'
    '"tables":["SCHEMA.TABLE1","SCHEMA.TABLE2"],'
    '"select":["SCHEMA.TABLE1.COL1","SCHEMA.TABLE2.COL2"],'
    '"joins":[{"left":"SCHEMA.TABLE1.COLX","right":"SCHEMA.TABLE2.COLY"}],'
    '"where":[{"col":"SCHEMA.TABLE1.COLZ","op":"=","value":"..."}],'
    '"group_by":[],'
    '"order_by":[{"col":"SCHEMA.TABLE1.COL1","dir":"ASC"}],'
    '"limit":20'
    "}\n"
)

PROMPT_TEMPLATE_NL = """SCHEMA CONTEXT:
{schema_context}

JOIN RULES:
{join_rules}

USER QUESTION:
{question}

Return JSON ONLY:
""".strip()


def _ollama_chat_json(system_prompt: str, user_prompt: str) -> Dict[str, Any]:
    if not CFG.OLLAMA_URL:
        return {}

    data = _http_json(
        "POST",
        f"{CFG.OLLAMA_URL}/api/chat",
        timeout=CFG.GEN_TIMEOUT,
        json_body={
            "model": CFG.GEN_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {
                "temperature": 0.0,
                "top_p": 0.2,
                "repeat_penalty": 1.1,
                "num_ctx": 2048,
                "num_predict": 280,
            },
        },
    )

    text = _clean((data.get("message") or {}).get("content")) if isinstance(data, dict) else ""

    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        if CFG.DEBUG:
            return {"_error": "model_non_json", "_raw": text[:1500]}
        return {}


# =========================================================
# Qdrant helpers - scroll for schema and join rules
# =========================================================
def _payload_text(p: Dict[str, Any]) -> str:
    for k in ("text", "prompt", "schema", "ddl", "content", "value"):
        v = p.get(k)
        if v:
            return _clean(v)
    return ""


def _qdrant_scroll(
    *,
    limit: int,
    offset: Optional[int] = None,
    must_conditions: Optional[List[dict]] = None,
    with_vector: bool = False,
) -> Tuple[List[dict], Optional[int]]:
    """
    Qdrant scroll helper.

    Qdrant 422 can happen if filter.must contains empty {}.
    So only send filter when we have at least one valid condition.
    """
    qdrant_url = _get_qdrant_url()

    if not qdrant_url:
        raise RuntimeError("Qdrant URL is not configured.")

    path = f"/collections/{CFG.CORE_COLLECTION}/points/scroll"

    body: Dict[str, Any] = {
        "limit": int(limit),
        "with_payload": True,
        "with_vector": bool(with_vector),
    }

    if offset is not None:
        body["offset"] = offset

    conds = []
    for c in must_conditions or []:
        if isinstance(c, dict) and c:
            conds.append(c)

    if conds:
        body["filter"] = {"must": conds}

    data = _http_json(
        "POST",
        f"{qdrant_url}{path}",
        timeout=CFG.QDRANT_TIMEOUT,
        json_body=body,
        headers=_qdrant_headers(),
    )

    result = data.get("result") if isinstance(data, dict) else {}
    points = result.get("points") if isinstance(result, dict) else None
    next_offset = result.get("next_page_offset") if isinstance(result, dict) else None

    return (
        points if isinstance(points, list) else [],
        next_offset if isinstance(next_offset, int) else None,
    )


def _fetch_all_core_schema_context(limit: int) -> str:
    pts, _ = _qdrant_scroll(
        limit=int(limit),
        must_conditions=[
            {
                "key": "doc_type",
                "match": {"value": "core_schema"},
            }
        ],
        with_vector=False,
    )

    chunks: List[str] = []

    for p in pts:
        payload = p.get("payload") or {}
        t = _payload_text(payload)
        if t:
            chunks.append(t)

    seen = set()
    out = []

    for c in chunks:
        if c not in seen:
            seen.add(c)
            out.append(c)

    return "\n\n".join(out).strip()


def _fetch_join_rules_master() -> str:
    pts, _ = _qdrant_scroll(
        limit=50,
        must_conditions=[
            {
                "key": "doc_type",
                "match": {"value": "join_rules"},
            },
            {
                "key": "chunk_type",
                "match": {"value": "master"},
            },
        ],
        with_vector=False,
    )

    if not pts:
        return ""

    payload = pts[0].get("payload") or {}
    return _payload_text(payload)


# =========================================================
# Schema index parsing + planner
# =========================================================
_TBL_RE = re.compile(r"^TABLE:\s*([A-Z0-9_]+\.[A-Z0-9_]+)$")
_COL_RE = re.compile(r"^COL:\s*([A-Z0-9_]+\.[A-Z0-9_]+\.[A-Z0-9_]+)$")


def _parse_schema_index(schema_text: str) -> Dict[str, Set[str]]:
    tables: Set[str] = set()
    cols: Set[str] = set()

    for line in (schema_text or "").upper().splitlines():
        line = line.strip()

        m1 = _TBL_RE.match(line)
        if m1:
            tables.add(m1.group(1))
            continue

        m2 = _COL_RE.match(line)
        if m2:
            cols.add(m2.group(1))

    return {
        "tables": tables,
        "cols": cols,
    }


def _pick_col(cols: Set[str], candidates: List[str]) -> Optional[str]:
    for c in candidates:
        c = (c or "").upper().strip()
        if c and c in cols:
            return c
    return None


def _q_has_any(q: str, words: List[str]) -> bool:
    q = (q or "").lower()
    return any(w in q for w in words)


def _extract_top_n(q: str) -> Optional[int]:
    m = re.search(r"\btop\s+(\d{1,4})\b", (q or "").lower())

    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None

    return None


def _extract_branch_code(q: str) -> Optional[str]:
    m = re.search(r"\bbranch\s*code\s*(?:=|:)?\s*([0-9]{1,6})\b", (q or "").lower())

    if m:
        return m.group(1)

    m = re.search(r"\bbranchcode\s*(?:=|:)?\s*([0-9]{1,6})\b", (q or "").lower())

    if m:
        return m.group(1)

    m = re.search(r"\bbranch\s*id\s*(?:=|:)?\s*([0-9]{1,6})\b", (q or "").lower())

    if m:
        return m.group(1)

    m = re.search(r"\bbranchid\s*(?:=|:)?\s*([0-9]{1,6})\b", (q or "").lower())

    if m:
        return m.group(1)

    return None


def _extract_like_prefix(q: str, key: str) -> Optional[str]:
    ql = (q or "").lower()

    m = re.search(
        rf"\b{re.escape(key)}\s*(?:starts with|prefix)\s*([a-z0-9_]+)\b",
        ql,
    )

    if m:
        return m.group(1)

    return None


def _extract_exact_value(q: str, key: str) -> Optional[str]:
    ql = (q or "").lower()

    m = re.search(
        rf"\b{re.escape(key)}\s*(?:=|:)?\s*([0-9]{{5,20}})\b",
        ql,
    )

    if m:
        return m.group(1)

    return None


def _ensure_loanee_join(
    *,
    cols: Set[str],
    tables: Set[str],
    base: str,
    plan_tables: List[str],
    plan_joins: List[dict],
) -> None:
    """
    Add join to SCOTT.DW_GC_LOANEE_V if possible.

    This is useful for:
    - member name
    - phone
    - branch code if not in base
    - branch name if only available in LOANEE_V
    """
    if "SCOTT.DW_GC_LOANEE_V" not in tables:
        return

    if "SCOTT.DW_GC_LOANEE_V" not in plan_tables:
        plan_tables.append("SCOTT.DW_GC_LOANEE_V")

    left = _pick_col(cols, [f"{base}.LOANEENO"])
    right = _pick_col(cols, ["SCOTT.DW_GC_LOANEE_V.LOANEECODE"])

    if not left or not right:
        return

    for j in plan_joins:
        if j.get("left") == left and j.get("right") == right:
            return

    plan_joins.append({"left": left, "right": right})


def _dynamic_plan(
    question: str,
    schema_text: str,
    join_rules_text: str,
    limit: int,
) -> Dict[str, Any]:
    """
    Rule-based planner for loan-related Core questions.

    Supports:
    - DISTINCT / unique / remove duplicates
    - top N
    - order by balance desc
    - branchcode filters
    - branch code/id select
    - branch name select if available in schema
    - loanno prefix filters
    - loaneeno exact filter
    - optional member join to LOANEE_V for name/phone/branch fields when schema exists
    """
    q = (question or "").lower()

    idx = _parse_schema_index(schema_text)
    cols = idx["cols"]
    tables = idx["tables"]

    wants_distinct = _q_has_any(
        q,
        [
            "distinct",
            "unique",
            "remove duplicates",
            "no duplicate",
            "no duplicates",
        ],
    )

    wants_loan = _q_has_any(
        q,
        [
            "loan",
            "loanno",
            "loan no",
            "loan number",
            "principal",
            "disburse",
            "approve",
            "balance",
            "outstanding",
            "due",
            "highest",
            "top",
        ],
    )

    wants_member = _q_has_any(
        q,
        [
            "member",
            "name",
            "firstname",
            "first name",
            "last name",
            "lastname",
            "phone",
            "phoneno",
            "mobile",
        ],
    )

    wants_branch = _q_has_any(
        q,
        [
            "branch",
            "branchcode",
            "branch code",
            "branch id",
            "branchid",
            "branch name",
            "branchname",
            "office",
            "office name",
            "office_name",
        ],
    )

    wants_branch_name = _q_has_any(
        q,
        [
            "branch name",
            "branchname",
            "office name",
            "office_name",
        ],
    )

    base = ""

    if wants_loan:
        if "SCOTT.DW_GC_UPDATE_LOAN_VIEW" in tables:
            base = "SCOTT.DW_GC_UPDATE_LOAN_VIEW"
        elif "SCOTT.DW_GC_LOAN_REGISTER_VIEW" in tables:
            base = "SCOTT.DW_GC_LOAN_REGISTER_VIEW"

    if not base:
        if "SCOTT.DW_GC_UPDATE_LOAN_VIEW" in tables:
            base = "SCOTT.DW_GC_UPDATE_LOAN_VIEW"
        elif "SCOTT.DW_GC_LOAN_REGISTER_VIEW" in tables:
            base = "SCOTT.DW_GC_LOAN_REGISTER_VIEW"
        else:
            base = next(iter(tables), "")

    if not base:
        return {
            "distinct": bool(wants_distinct),
            "tables": [],
            "select": [],
            "joins": [],
            "where": [],
            "group_by": [],
            "order_by": [],
            "limit": int(limit),
            "_planner": "dynamic_rules",
        }

    plan_tables: List[str] = [base]
    plan_select: List[str] = []
    plan_joins: List[dict] = []
    plan_where: List[dict] = []
    plan_order: List[dict] = []

    # Select LOANNO
    if _q_has_any(q, ["loanno", "loan no", "loan number"]):
        c = _pick_col(
            cols,
            [
                f"{base}.LOANNO",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.LOANNO",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.LOANNO",
            ],
        )
        if c:
            plan_select.append(c)

    # Select BALANCE
    if _q_has_any(q, ["balance", "outstanding", "due", "highest", "top"]):
        c = _pick_col(
            cols,
            [
                f"{base}.BALANCE",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BALANCE",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.BALANCE",
            ],
        )
        if c:
            plan_select.append(c)

    # Always include LOANEENO for loan/member/branch context
    if wants_member or wants_loan or wants_branch or "loaneeno" in q:
        c = _pick_col(
            cols,
            [
                f"{base}.LOANEENO",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.LOANEENO",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.LOANEENO",
            ],
        )
        if c:
            plan_select.append(c)

    # Branch code/id select
    if wants_branch:
        c = _pick_col(
            cols,
            [
                f"{base}.BRANCHCODE",
                f"{base}.BRANCH_CODE",
                f"{base}.BRANCHID",
                f"{base}.BRANCH_ID",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BRANCHCODE",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BRANCH_CODE",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BRANCHID",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BRANCH_ID",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.BRANCHCODE",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.BRANCH_CODE",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.BRANCHID",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.BRANCH_ID",
            ],
        )

        if c:
            plan_select.append(c)
        else:
            _ensure_loanee_join(
                cols=cols,
                tables=tables,
                base=base,
                plan_tables=plan_tables,
                plan_joins=plan_joins,
            )
            c = _pick_col(
                cols,
                [
                    "SCOTT.DW_GC_LOANEE_V.BRANCHCODE",
                    "SCOTT.DW_GC_LOANEE_V.BRANCH_CODE",
                    "SCOTT.DW_GC_LOANEE_V.BRANCHID",
                    "SCOTT.DW_GC_LOANEE_V.BRANCH_ID",
                ],
            )
            if c:
                plan_select.append(c)

    # Branch name select
    if wants_branch_name:
        c = _pick_col(
            cols,
            [
                f"{base}.BRANCHNAME",
                f"{base}.BRANCH_NAME",
                f"{base}.OFFICENAME",
                f"{base}.OFFICE_NAME",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BRANCHNAME",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BRANCH_NAME",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.OFFICENAME",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.OFFICE_NAME",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.BRANCHNAME",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.BRANCH_NAME",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.OFFICENAME",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.OFFICE_NAME",
            ],
        )

        if c:
            plan_select.append(c)
        else:
            _ensure_loanee_join(
                cols=cols,
                tables=tables,
                base=base,
                plan_tables=plan_tables,
                plan_joins=plan_joins,
            )
            c = _pick_col(
                cols,
                [
                    "SCOTT.DW_GC_LOANEE_V.BRANCHNAME",
                    "SCOTT.DW_GC_LOANEE_V.BRANCH_NAME",
                    "SCOTT.DW_GC_LOANEE_V.OFFICENAME",
                    "SCOTT.DW_GC_LOANEE_V.OFFICE_NAME",
                ],
            )
            if c:
                plan_select.append(c)

    # Join member table if user asks for name/phone/member info
    if wants_member:
        _ensure_loanee_join(
            cols=cols,
            tables=tables,
            base=base,
            plan_tables=plan_tables,
            plan_joins=plan_joins,
        )

        if _q_has_any(q, ["firstname", "first name", "name", "member"]):
            c = _pick_col(cols, ["SCOTT.DW_GC_LOANEE_V.FIRSTNAME"])
            if c:
                plan_select.append(c)

        if _q_has_any(q, ["lastname", "last name", "name", "member"]):
            c = _pick_col(cols, ["SCOTT.DW_GC_LOANEE_V.LASTNAME"])
            if c:
                plan_select.append(c)

        if _q_has_any(q, ["phone", "phoneno", "mobile"]):
            c = _pick_col(cols, ["SCOTT.DW_GC_LOANEE_V.PHONENO"])
            if c:
                plan_select.append(c)

    # Filter: branchcode / branch id
    bc = _extract_branch_code(q)

    if bc:
        c = _pick_col(
            cols,
            [
                f"{base}.BRANCHCODE",
                f"{base}.BRANCH_CODE",
                f"{base}.BRANCHID",
                f"{base}.BRANCH_ID",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BRANCHCODE",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BRANCH_CODE",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BRANCHID",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BRANCH_ID",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.BRANCHCODE",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.BRANCH_CODE",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.BRANCHID",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.BRANCH_ID",
            ],
        )

        if not c:
            _ensure_loanee_join(
                cols=cols,
                tables=tables,
                base=base,
                plan_tables=plan_tables,
                plan_joins=plan_joins,
            )
            c = _pick_col(
                cols,
                [
                    "SCOTT.DW_GC_LOANEE_V.BRANCHCODE",
                    "SCOTT.DW_GC_LOANEE_V.BRANCH_CODE",
                    "SCOTT.DW_GC_LOANEE_V.BRANCHID",
                    "SCOTT.DW_GC_LOANEE_V.BRANCH_ID",
                ],
            )

        if c:
            plan_where.append(
                {
                    "col": c,
                    "op": "=",
                    "value": bc,
                }
            )

    # Filter: loanno prefix
    loanno_prefix = _extract_like_prefix(q, "loanno")

    if loanno_prefix:
        c = _pick_col(
            cols,
            [
                f"{base}.LOANNO",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.LOANNO",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.LOANNO",
            ],
        )
        if c:
            plan_where.append(
                {
                    "col": c,
                    "op": "LIKE",
                    "value": f"{loanno_prefix}%",
                }
            )

    # Filter: loaneeno exact
    loaneeno_val = _extract_exact_value(q, "loaneeno")

    if loaneeno_val:
        c = _pick_col(
            cols,
            [
                f"{base}.LOANEENO",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.LOANEENO",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.LOANEENO",
            ],
        )
        if c:
            plan_where.append(
                {
                    "col": c,
                    "op": "=",
                    "value": loaneeno_val,
                }
            )

    # TOP / LIMIT
    top_n = _extract_top_n(q)

    if top_n is not None:
        limit = top_n

    # ORDER BY BALANCE DESC
    if "top" in q and _q_has_any(q, ["balance", "outstanding", "due", "highest"]):
        c = _pick_col(
            cols,
            [
                f"{base}.BALANCE",
                "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BALANCE",
                "SCOTT.DW_GC_LOAN_REGISTER_VIEW.BALANCE",
            ],
        )

        if c:
            plan_order.append({"col": c, "dir": "DESC"})

            c2 = _pick_col(cols, [f"{base}.LOANNO", f"{base}.LOANEENO"])
            if c2:
                plan_order.append({"col": c2, "dir": "DESC"})

    # De-duplicate tables/select preserving order
    seen = set()
    plan_tables = [t for t in plan_tables if not (t in seen or seen.add(t))]

    seen = set()
    plan_select = [c for c in plan_select if not (c in seen or seen.add(c))]

    # Fallback select
    if not plan_select:
        for c in (
            _pick_col(cols, [f"{base}.LOANEENO"]),
            _pick_col(cols, [f"{base}.LOANNO"]),
            _pick_col(cols, [f"{base}.BALANCE"]),
            _pick_col(cols, [f"{base}.BRANCHCODE"]),
            _pick_col(cols, [f"{base}.BRANCHID"]),
        ):
            if c:
                plan_select.append(c)

    return {
        "distinct": bool(wants_distinct),
        "tables": plan_tables,
        "select": plan_select,
        "joins": plan_joins,
        "where": plan_where,
        "group_by": [],
        "order_by": plan_order,
        "limit": int(limit),
        "_planner": "dynamic_rules",
    }


# =========================================================
# Plan -> SQL - Oracle 11g
# =========================================================
def _table_of(col: str) -> str:
    parts = col.split(".")

    if len(parts) >= 3:
        return ".".join(parts[:2])

    if len(parts) == 2:
        return parts[0]

    return ""


def _plan_to_sql(
    plan: Dict[str, Any],
    allowed_cols: Set[str],
    allowed_tables: Set[str],
    default_limit: int,
    max_limit: int,
) -> str:
    tables = [
        str(x).upper().strip()
        for x in plan.get("tables") or []
        if str(x).strip()
    ]

    select = [
        str(x).upper().strip()
        for x in plan.get("select") or []
        if str(x).strip()
    ]

    joins = plan.get("joins") or []
    where = plan.get("where") or []

    group_by = [
        str(x).upper().strip()
        for x in plan.get("group_by") or []
        if str(x).strip()
    ]

    order_by = plan.get("order_by") or []
    limit = int(plan.get("limit") or default_limit)
    distinct = bool(plan.get("distinct"))

    if not tables or not select:
        raise ValueError("Plan missing tables/select.")

    for t in tables:
        if t not in allowed_tables:
            raise ValueError(f"Table not allowed: {t}")

    for c in select:
        if c not in allowed_cols:
            raise ValueError(f"Select not allowed: {c}")

    for g in group_by:
        if g not in allowed_cols:
            raise ValueError(f"Group-by not allowed: {g}")

    select_kw = "SELECT DISTINCT " if distinct else "SELECT "
    base_sql = select_kw + ", ".join(select) + " FROM " + tables[0]

    used_tables = {tables[0]}

    for j in joins:
        left = str((j or {}).get("left") or "").upper().strip()
        right = str((j or {}).get("right") or "").upper().strip()

        if not left or not right:
            continue

        if left not in allowed_cols or right not in allowed_cols:
            raise ValueError("Join columns not allowed.")

        lt = _table_of(left)
        rt = _table_of(right)

        candidates = [
            t
            for t in (lt, rt)
            if t and t in tables and t not in used_tables
        ]

        if not candidates:
            continue

        join_table = candidates[0]
        base_sql += f" JOIN {join_table} ON {left} = {right}"
        used_tables.add(join_table)

    clauses: List[str] = []

    allowed_ops = {
        "=",
        "!=",
        ">",
        ">=",
        "<",
        "<=",
        "LIKE",
        "IS_NOT_NULL",
        "IS_NULL",
        "NOT_EMPTY",
    }

    for w in where:
        col = str((w or {}).get("col") or "").upper().strip()
        op = str((w or {}).get("op") or "=").upper().strip()
        val = (w or {}).get("value")

        if not col or col not in allowed_cols:
            raise ValueError("Where column not allowed.")

        if op not in allowed_ops:
            raise ValueError(f"Where op not allowed: {op}")

        if op == "IS_NOT_NULL":
            clauses.append(f"{col} IS NOT NULL")
            continue

        if op == "IS_NULL":
            clauses.append(f"{col} IS NULL")
            continue

        if op == "NOT_EMPTY":
            clauses.append(f"{col} IS NOT NULL")
            clauses.append(f"TRIM({col}) <> ''")
            continue

        sval = ("" if val is None else str(val)).replace("'", "''")
        clauses.append(f"{col} {op} '{sval}'")

    if clauses:
        base_sql += " WHERE " + " AND ".join(clauses)

    if group_by:
        base_sql += " GROUP BY " + ", ".join(group_by)

    if order_by:
        items: List[str] = []

        for ob in order_by:
            col = str((ob or {}).get("col") or "").upper().strip()
            direc = str((ob or {}).get("dir") or "ASC").upper().strip()

            if col and col in allowed_cols:
                items.append(f"{col} {'DESC' if direc == 'DESC' else 'ASC'}")

        if items:
            base_sql += " ORDER BY " + ", ".join(items)

    if limit <= 0:
        limit = default_limit

    if limit > max_limit:
        limit = max_limit

    return f"SELECT * FROM ({base_sql}) WHERE ROWNUM <= {int(limit)}"


# =========================================================
# NiFi execution
# =========================================================
def _nifi_exec(sql: str) -> JsonT:
    if not CFG.NIFI_CORE_URL:
        raise RuntimeError("NIFI_CORE_URL is not configured.")

    r = requests.post(
        CFG.NIFI_CORE_URL,
        json={"sql": sql},
        timeout=CFG.NIFI_TIMEOUT,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )

    if not r.ok:
        raise RuntimeError(f"NiFi error {r.status_code}: {r.text[:1000]}")

    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:4000]}


# =========================================================
# Bangla user-friendly output helpers
# =========================================================
def _format_amount(value: Any) -> str:
    try:
        if value is None:
            return "-"

        text = str(value).replace(",", "").strip()

        if not text:
            return "-"

        num = float(text)

        if num.is_integer():
            return f"{int(num):,}"

        return f"{num:,.2f}"

    except Exception:
        return str(value)


def _get_row_value(row: Dict[str, Any], possible_keys: List[str]) -> Any:
    if not isinstance(row, dict):
        return None

    upper_map = {
        str(k).upper(): v
        for k, v in row.items()
    }

    for key in possible_keys:
        key_upper = key.upper()

        if key_upper in upper_map:
            return upper_map[key_upper]

    for existing_key, value in upper_map.items():
        for key in possible_keys:
            key_upper = key.upper()

            if existing_key.endswith("." + key_upper):
                return value

            if existing_key.endswith("_" + key_upper):
                return value

            if existing_key == key_upper:
                return value

    return None


def _extract_result_rows(result: Optional[JsonT]) -> List[Dict[str, Any]]:
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]

    if isinstance(result, dict):
        for key in ("rows", "data", "result"):
            value = result.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]

    return []


def _has_selected_column(plan: Dict[str, Any], col_name: str) -> bool:
    col_name = col_name.upper()
    select = plan.get("select") or []

    for c in select:
        c = str(c).upper()
        if c.endswith("." + col_name) or c == col_name:
            return True

    return False


def _bangla_core_answer(
    question: str,
    plan: Dict[str, Any],
    sql: str,
    result: Optional[JsonT],
    show_debug: bool = False,
) -> str:
    rows = _extract_result_rows(result)
    limit = int(plan.get("limit") or CFG.PREVIEW_ROWS)
    order_by = plan.get("order_by") or []
    tables = plan.get("tables") or []
    select = plan.get("select") or []

    q_lower = (question or "").lower()

    wants_top_balance = (
        "top" in q_lower
        and any(word in q_lower for word in ["balance", "outstanding", "due", "highest"])
    )

    wants_member = any(
        word in q_lower
        for word in ["member", "name", "phone", "mobile", "phoneno"]
    )

    lines: List[str] = []

    if result is not None:
        lines.append("✅ ঋণ সংক্রান্ত তথ্য সফলভাবে পাওয়া গেছে।")
    else:
        lines.append("✅ ঋণ সংক্রান্ত অনুসন্ধানের জন্য কুয়েরি প্রস্তুত করা হয়েছে।")

    lines.append("")

    if wants_top_balance:
        lines.append("### ঋণ ব্যালেন্স বিশ্লেষণ")
        lines.append("")
        lines.append(
            f"আপনার প্রশ্ন অনুযায়ী সর্বোচ্চ বকেয়া/আউটস্ট্যান্ডিং ব্যালেন্স থাকা "
            f"শীর্ষ {limit}টি ঋণ হিসাব খুঁজে দেখা হয়েছে।"
        )
    else:
        lines.append("### ঋণ তথ্যের সারসংক্ষেপ")
        lines.append("")
        lines.append("আপনার প্রশ্ন অনুযায়ী অনুমোদিত Core ডেটা থেকে সংশ্লিষ্ট ঋণ তথ্য যাচাই করা হয়েছে।")

    if rows:
        balances: List[float] = []
        branch_codes: Set[str] = set()
        branch_names: Set[str] = set()

        for row in rows:
            bal = _get_row_value(row, ["BALANCE"])
            try:
                balances.append(float(str(bal).replace(",", "")))
            except Exception:
                pass

            branch_code = _get_row_value(
                row,
                ["BRANCHCODE", "BRANCH_CODE", "BRANCHID", "BRANCH_ID"],
            )
            if branch_code not in (None, ""):
                branch_codes.add(str(branch_code))

            branch_name = _get_row_value(
                row,
                ["BRANCHNAME", "BRANCH_NAME", "OFFICENAME", "OFFICE_NAME"],
            )
            if branch_name not in (None, ""):
                branch_names.add(str(branch_name))

        lines.append("")
        lines.append(f"**প্রদর্শিত মোট রেকর্ড:** {len(rows)}")

        if branch_codes:
            lines.append(f"**প্রদর্শিত ব্রাঞ্চ সংখ্যা:** {len(branch_codes)}")

        if branch_names:
            lines.append(f"**প্রদর্শিত ব্রাঞ্চ নামের সংখ্যা:** {len(branch_names)}")

        if balances:
            max_balance = max(balances)
            min_balance = min(balances)
            total_balance = sum(balances)
            avg_balance = total_balance / len(balances) if balances else 0

            lines.append(f"**সর্বোচ্চ ব্যালেন্স:** {_format_amount(max_balance)}")
            lines.append(f"**সর্বনিম্ন ব্যালেন্স:** {_format_amount(min_balance)}")
            lines.append(f"**গড় ব্যালেন্স:** {_format_amount(avg_balance)}")
            lines.append(f"**প্রদর্শিত মোট ব্যালেন্স:** {_format_amount(total_balance)}")

            lines.append("")
            lines.append("### বিশ্লেষণমূলক পর্যবেক্ষণ")
            lines.append("")

            if max_balance == min_balance:
                lines.append(
                    "- প্রদর্শিত সব ঋণ হিসাবের ব্যালেন্স একই। তাই এগুলো একই পর্যায়ের উচ্চ ব্যালেন্সধারী ঋণ হিসাব হিসেবে বিবেচিত হতে পারে।"
                )
            else:
                diff = max_balance - min_balance
                lines.append(
                    f"- প্রদর্শিত ফলাফলে সর্বোচ্চ ও সর্বনিম্ন ব্যালেন্সের পার্থক্য {_format_amount(diff)}।"
                )

            if wants_top_balance:
                lines.append(
                    "- যেহেতু প্রশ্নে সর্বোচ্চ ব্যালেন্স চাওয়া হয়েছে, ফলাফলটি ব্যালেন্স অনুযায়ী বড় থেকে ছোট ক্রমে সাজানো হয়েছে।"
                )

            if branch_codes:
                lines.append(
                    "- ফলাফলে ব্রাঞ্চ কোড/আইডি যুক্ত থাকায় কোন ব্রাঞ্চে উচ্চ ব্যালেন্সধারী ঋণ হিসাব আছে তা সহজে পর্যবেক্ষণ করা যাবে।"
                )

            if len(rows) >= limit:
                lines.append(
                    "- ফলাফলে নির্ধারিত সীমা অনুযায়ী রেকর্ড দেখানো হয়েছে। আরও বিস্তারিত বিশ্লেষণের জন্য limit বাড়ানো যেতে পারে।"
                )

    if rows:
        lines.append("")
        lines.append("### ঋণ হিসাবের তালিকা")
        lines.append("")

        has_loanno = _has_selected_column(plan, "LOANNO") or any(
            _get_row_value(row, ["LOANNO"]) is not None for row in rows
        )

        has_balance = _has_selected_column(plan, "BALANCE") or any(
            _get_row_value(row, ["BALANCE"]) is not None for row in rows
        )

        has_branch_code = any(
            _get_row_value(
                row,
                ["BRANCHCODE", "BRANCH_CODE", "BRANCHID", "BRANCH_ID"],
            )
            is not None
            for row in rows
        )

        has_branch_name = any(
            _get_row_value(
                row,
                ["BRANCHNAME", "BRANCH_NAME", "OFFICENAME", "OFFICE_NAME"],
            )
            is not None
            for row in rows
        )

        has_name = wants_member or any(
            _get_row_value(row, ["FIRSTNAME", "LASTNAME"]) is not None for row in rows
        )

        has_phone = wants_member or any(
            _get_row_value(row, ["PHONENO", "PHONE", "MOBILE"]) is not None for row in rows
        )

        headers = ["ক্রমিক", "Loanee No"]

        if has_loanno:
            headers.append("Loan No")

        if has_balance:
            headers.append("Outstanding Balance")

        if has_branch_code:
            headers.append("Branch Code/ID")

        if has_branch_name:
            headers.append("Branch Name")

        if has_name:
            headers.append("সদস্যের নাম")

        if has_phone:
            headers.append("মোবাইল/ফোন")

        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["---:" if h == "ক্রমিক" else "---" for h in headers]) + "|")

        for idx, row in enumerate(rows[: CFG.PREVIEW_ROWS], start=1):
            loaneeno = _get_row_value(row, ["LOANEENO", "LOANEECODE"]) or "-"
            loanno = _get_row_value(row, ["LOANNO"]) or "-"
            balance = _get_row_value(row, ["BALANCE"])

            branch_code = (
                _get_row_value(
                    row,
                    ["BRANCHCODE", "BRANCH_CODE", "BRANCHID", "BRANCH_ID"],
                )
                or "-"
            )

            branch_name = (
                _get_row_value(
                    row,
                    ["BRANCHNAME", "BRANCH_NAME", "OFFICENAME", "OFFICE_NAME"],
                )
                or "-"
            )

            firstname = _get_row_value(row, ["FIRSTNAME"]) or ""
            lastname = _get_row_value(row, ["LASTNAME"]) or ""
            member_name = f"{firstname} {lastname}".strip() or "-"

            phone = _get_row_value(row, ["PHONENO", "PHONE", "MOBILE"]) or "-"

            cells = [
                str(idx),
                str(loaneeno),
            ]

            if has_loanno:
                cells.append(str(loanno))

            if has_balance:
                cells.append(_format_amount(balance))

            if has_branch_code:
                cells.append(str(branch_code))

            if has_branch_name:
                cells.append(str(branch_name))

            if has_name:
                cells.append(str(member_name))

            if has_phone:
                cells.append(str(phone))

            lines.append("| " + " | ".join(cells) + " |")

    else:
        lines.append("")
        lines.append("কোনো মিল থাকা ঋণ রেকর্ড পাওয়া যায়নি।")

    lines.append("")
    lines.append("### কীভাবে ফলাফল প্রস্তুত করা হয়েছে")
    lines.append("")
    lines.append("- অনুমোদিত Core schema থেকে সংশ্লিষ্ট ঋণ ডেটা নির্বাচন করা হয়েছে।")
    lines.append("- শুধুমাত্র অনুমোদিত table/column ব্যবহার করা হয়েছে।")

    if order_by:
        lines.append("- আপনার প্রশ্ন অনুযায়ী ফলাফল sort করা হয়েছে।")

    lines.append("- উত্তরটি সহজবোধ্য রাখার জন্য নির্দিষ্ট সংখ্যক রেকর্ড দেখানো হয়েছে।")

    if show_debug:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("### Technical Details")
        lines.append("")
        lines.append(f"**Question:** {question}")
        lines.append(f"**Planner:** {plan.get('_planner', 'unknown')}")

        if plan.get("distinct"):
            lines.append("**Distinct:** true")

        lines.append(f"**Table(s):** {', '.join(tables) if tables else '(none)'}")
        lines.append(f"**Columns:** {', '.join(select) if select else '(none)'}")

        if order_by:
            ob = []

            for x in order_by:
                c = (x or {}).get("col")
                d = (x or {}).get("dir", "ASC")

                if c:
                    ob.append(f"{c} {d}")

            if ob:
                lines.append(f"**Order:** {', '.join(ob)}")

        lines.append(f"**Limit:** {limit}")
        lines.append("")
        lines.append("```sql")
        lines.append(sql)
        lines.append("```")

    return "\n".join(lines).strip()


def _pretty_answer(
    question: str,
    plan: Dict[str, Any],
    sql: str,
    result: Optional[JsonT],
    show_debug: bool = False,
) -> str:
    return _bangla_core_answer(
        question=question,
        plan=plan,
        sql=sql,
        result=result,
        show_debug=show_debug,
    )


# =========================================================
# API: Natural Language -> Dynamic Plan -> Oracle SQL -> NiFi
# =========================================================
@csrf_exempt
@require_POST
def core_ask(request):
    """
    POST JSON:
    {
        "question": "Find top 40 loans with highest outstanding balance and show branch code.",
        "limit": 40,
        "execute": true,
        "pretty": true,
        "debug": false
    }
    """
    deny = _deny_core(request)

    if deny:
        return deny

    body = _json_body(request)

    question = _clean(body.get("question") or body.get("message"))

    if not question:
        return JsonResponse(
            {
                "ok": False,
                "error": "question is required",
            },
            status=400,
        )

    req_limit = _safe_limit(
        body.get("limit"),
        CFG.DEFAULT_LIMIT,
        CFG.MAX_LIMIT,
    )

    execute = _safe_bool(body.get("execute"), default=True)
    pretty = _safe_bool(body.get("pretty"), default=True)
    want_debug = _safe_bool(body.get("debug"), default=False)

    show_debug = bool(want_debug or CFG.DEBUG)

    try:
        # 1. Pull schema context from Qdrant
        schema_context = _fetch_all_core_schema_context(
            limit=CFG.SCHEMA_SCROLL_LIMIT
        )

        if not schema_context:
            return JsonResponse(
                {
                    "ok": True,
                    "answer": "ডেটা অপর্যাপ্ত। Core schema পাওয়া যায়নি।",
                    "answer_pretty": "ডেটা অপর্যাপ্ত। Core schema পাওয়া যায়নি।",
                    "plan": None,
                    "sql": None,
                    "result": None,
                    "executed": False,
                    "nifi_url": CFG.NIFI_CORE_URL,
                }
            )

        # 2. Pull join rules master, optional
        join_rules_text = _fetch_join_rules_master()

        # 3. Rule-based dynamic plan
        plan = _dynamic_plan(
            question,
            schema_context,
            join_rules_text,
            req_limit,
        )

        # 4. Optional Aya plan refinement
        if CFG.USE_LLM_PLANNER:
            prompt = PROMPT_TEMPLATE_NL.format(
                schema_context=schema_context,
                join_rules=join_rules_text,
                question=question,
            )

            llm_plan = _ollama_chat_json(
                SYSTEM_PROMPT_CORE_PLAN,
                prompt,
            )

            if (
                isinstance(llm_plan, dict)
                and llm_plan.get("tables")
                and llm_plan.get("select")
            ):
                llm_plan["limit"] = int(req_limit)
                llm_plan.setdefault("distinct", False)
                llm_plan["_planner"] = "aya_json_plan"
                plan = llm_plan

        # 5. Compile SQL using allow-list from schema index
        idx = _parse_schema_index(schema_context)

        sql = _plan_to_sql(
            plan,
            allowed_cols=idx["cols"],
            allowed_tables=idx["tables"],
            default_limit=req_limit,
            max_limit=CFG.MAX_LIMIT,
        )

        # 6. If execute=false, return generated answer only
        if not execute:
            resp: Dict[str, Any] = {
                "ok": True,
                "question": question,
                "executed": False,
                "nifi_url": CFG.NIFI_CORE_URL,
            }

            if show_debug:
                resp["plan"] = plan
                resp["sql"] = sql

            if pretty:
                resp["answer_pretty"] = _pretty_answer(
                    question,
                    plan,
                    sql,
                    result=None,
                    show_debug=show_debug,
                )
                resp["answer"] = resp["answer_pretty"]

            return JsonResponse(resp)

        # 7. Execute via NiFi
        result = _nifi_exec(sql)

        resp2: Dict[str, Any] = {
            "ok": True,
            "question": question,
            "executed": True,
            "nifi_url": CFG.NIFI_CORE_URL,
            "result": result,
        }

        if show_debug:
            resp2["plan"] = plan
            resp2["sql"] = sql

        if pretty:
            resp2["answer_pretty"] = _pretty_answer(
                question,
                plan,
                sql,
                result=result,
                show_debug=show_debug,
            )
            resp2["answer"] = resp2["answer_pretty"]

        return JsonResponse(resp2)

    except Exception as e:
        return JsonResponse(
            {
                "ok": False,
                "error": f"Core backend error: {e}",
                "nifi_url": CFG.NIFI_CORE_URL,
            },
            status=502,
        )
