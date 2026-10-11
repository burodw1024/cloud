# views_core.py
# Oracle 11g dynamic SQL generator (rule-based planner) + optional NiFi execution
# Qdrant named vector: "default" (for search), but this module can work even without vector search.

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

JsonT = Union[Dict[str, Any], List[Any]]

# =========================================================
# RBAC (core finance)
# =========================================================
CORE_ALLOWED_GROUPS: Set[str] = {
    "DEPT_ACCOUNTS",
    "DEPT_FINANCE",
    "HEAD_OFFICE",
    "DEPT_IT_HEAD",
}

ENFORCE_STAFF_ONLY = (os.getenv("ENFORCE_STAFF_ONLY", "1") or "1").lower() not in ("0", "false", "no")


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
        return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

    if getattr(user, "is_superuser", False):
        return None

    if ENFORCE_STAFF_ONLY and not getattr(user, "is_staff", False):
        return JsonResponse({"ok": False, "error": "Staff access required."}, status=403)

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
# Config
# =========================================================
@dataclass(frozen=True)
class Cfg:
    OLLAMA_URL: str = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    QDRANT_URL: str = _env("QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")

    # optional LLM planner/refiner
    GEN_MODEL: str = _env("GEN_MODEL", "aya-23-8b")
    USE_LLM_PLANNER: bool = (_env("USE_LLM_PLANNER", "0") or "0").lower() in ("1", "true", "yes")

    EMBED_MODEL: str = _env("EMBED_MODEL", "nomic-embed-text:latest")

    CORE_COLLECTION: str = _env("CORE_COLLECTION", "buro_core").strip() or "buro_core"
    QDRANT_VECTOR_NAME: str = (_env("QDRANT_VECTOR_NAME", "default").strip() or "default")

    # if you later want vector search (not required for small schema)
    TOP_K_SCHEMA: int = _env_int("TOP_K_SCHEMA", "8")

    GEN_TIMEOUT: float = _env_float("GEN_TIMEOUT", "120")
    EMBED_TIMEOUT: float = _env_float("EMBED_TIMEOUT", "60")
    QDRANT_TIMEOUT: float = _env_float("QDRANT_TIMEOUT", "30")

    # NiFi core query endpoint (POST {sql:"..."})
    NIFI_CORE_URL: str = _env("NIFI_CORE_URL", "http://192.168.1.139:8888/core/query").strip()
    NIFI_TIMEOUT: float = _env_float("NIFI_TIMEOUT", "300")

    DEFAULT_LIMIT: int = _env_int("CORE_DEFAULT_LIMIT", "200")
    MAX_LIMIT: int = _env_int("CORE_MAX_LIMIT", "2000")

    PREVIEW_ROWS: int = _env_int("CORE_PREVIEW_ROWS", "10")

    DEBUG: bool = (_env("CORE_DEBUG", "0") or "0").lower() in ("1", "true", "yes")


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


def _http_json(method: str, url: str, *, timeout: float, json_body: Optional[dict] = None) -> JsonT:
    r = requests.request(
        method,
        url,
        json=json_body,
        timeout=timeout,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:800]}")
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Invalid JSON from {url}: {e}. Body={r.text[:800]}")


# =========================================================
# Ollama
# =========================================================
SYSTEM_PROMPT_CORE_PLAN = (
    "You are an Oracle 11g SQL query planner.\n"
    "You MUST use ONLY identifiers that appear in SCHEMA CONTEXT.\n"
    "Return ONLY valid JSON (no markdown, no explanations).\n"
    "JSON format:\n"
    "{"
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
    # IMPORTANT: Do NOT send "format" as object; some Ollama versions require a string.
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
                "num_predict": 260,
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


def _ollama_embed(text: str) -> List[float]:
    last_err: Optional[Exception] = None
    for path, body in [
        ("/api/embed", {"model": CFG.EMBED_MODEL, "input": text}),          # newer
        ("/api/embeddings", {"model": CFG.EMBED_MODEL, "prompt": text}),   # older
    ]:
        try:
            data = _http_json("POST", f"{CFG.OLLAMA_URL}{path}", timeout=CFG.EMBED_TIMEOUT, json_body=body)

            vec = None
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                vec = ((data.get("data") or [{}])[0]).get("embedding")
            elif isinstance(data, dict) and isinstance(data.get("embedding"), list):
                vec = data["embedding"]
            elif isinstance(data, dict) and isinstance(data.get("embeddings"), list):
                vec = (data.get("embeddings") or [[]])[0]

            if not isinstance(vec, list):
                raise RuntimeError("Unknown embed response shape")
            if len(vec) != 768:
                raise RuntimeError(f"Embedding dim mismatch: got {len(vec)} expected 768")
            return vec
        except Exception as e:
            last_err = e

    raise RuntimeError(f"Ollama embedding failed: {last_err}")


# =========================================================
# Qdrant helpers
# =========================================================
def _payload_text(p: Dict[str, Any]) -> str:
    # your ingested payload uses "text"
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
    Qdrant 422 happens if filter.must contains an empty {}.
    So we only send filter when we have >=1 valid condition.
    """
    path = f"/collections/{CFG.CORE_COLLECTION}/points/scroll"
    body: Dict[str, Any] = {
        "limit": int(limit),
        "with_payload": True,
        "with_vector": bool(with_vector),
    }
    if offset is not None:
        body["offset"] = offset

    conds = []
    for c in (must_conditions or []):
        if isinstance(c, dict) and c:
            conds.append(c)
    if conds:
        body["filter"] = {"must": conds}

    data = _http_json("POST", f"{CFG.QDRANT_URL}{path}", timeout=CFG.QDRANT_TIMEOUT, json_body=body)
    result = data.get("result") if isinstance(data, dict) else {}
    points = result.get("points") if isinstance(result, dict) else None
    next_offset = result.get("next_page_offset") if isinstance(result, dict) else None
    return (points if isinstance(points, list) else []), (next_offset if isinstance(next_offset, int) else None)


def _fetch_all_core_schema_context(limit: int = 400) -> str:
    pts, _ = _qdrant_scroll(
        limit=limit,
        must_conditions=[{"key": "doc_type", "match": {"value": "core_schema"}}],
        with_vector=False,
    )
    chunks = []
    for p in pts:
        payload = p.get("payload") or {}
        t = _payload_text(payload)
        if t:
            chunks.append(t)

    # de-dup keep order
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
            {"key": "doc_type", "match": {"value": "join_rules"}},
            {"key": "chunk_type", "match": {"value": "master"}},
        ],
        with_vector=False,
    )
    if not pts:
        return ""
    payload = (pts[0].get("payload") or {})
    return _payload_text(payload)


# =========================================================
# Schema index + Dynamic Planner
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
    return {"tables": tables, "cols": cols}


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
    # supports: branchcode 101 | branch code=101 | branch:101
    m = re.search(r"\bbranch\s*code\s*(?:=|:)?\s*([0-9]{1,6})\b", (q or "").lower())
    if m:
        return m.group(1)
    m = re.search(r"\bbranchcode\s*(?:=|:)?\s*([0-9]{1,6})\b", (q or "").lower())
    if m:
        return m.group(1)
    return None


def _extract_like_prefix(q: str, key: str) -> Optional[str]:
    # example: "loanno starts with 23" / "loanno prefix 23"
    ql = (q or "").lower()
    m = re.search(rf"\b{re.escape(key)}\s*(?:starts with|prefix)\s*([a-z0-9_]+)\b", ql)
    if m:
        return m.group(1)
    return None


def _dynamic_plan(question: str, schema_text: str, join_rules_text: str, limit: int) -> Dict[str, Any]:
    q = (question or "").lower()
    idx = _parse_schema_index(schema_text)
    cols = idx["cols"]
    tables = idx["tables"]

    wants_savings = _q_has_any(q, ["saving", "savings", "deposit", "withdraw", "withdrawal", "savingaccount", "saving account"])
    wants_loan = _q_has_any(q, ["loan", "loanno", "loan no", "principal", "disburse", "approve", "balance", "outstanding", "due"])
    wants_member = _q_has_any(q, ["member", "name", "firstname", "last name", "lastname", "phone", "phoneno", "mobile"])

    # base table selection (exists check)
    base = ""
    if wants_savings:
        if "SCOTT.DW_GC_UPDATESAVINGS" in tables:
            base = "SCOTT.DW_GC_UPDATESAVINGS"
        elif "SCOTT.DW_GC_SAVINGREGISTER" in tables:
            base = "SCOTT.DW_GC_SAVINGREGISTER"
    if not base and wants_loan:
        if "SCOTT.DW_GC_UPDATE_LOAN_VIEW" in tables:
            base = "SCOTT.DW_GC_UPDATE_LOAN_VIEW"
        elif "SCOTT.DW_GC_LOAN_REGISTER_VIEW" in tables:
            base = "SCOTT.DW_GC_LOAN_REGISTER_VIEW"
    if not base:
        # fallback to a known table if present
        base = "SCOTT.DW_GC_UPDATE_LOAN_VIEW" if "SCOTT.DW_GC_UPDATE_LOAN_VIEW" in tables else (next(iter(tables), ""))

    if not base:
        return {"tables": [], "select": [], "joins": [], "where": [], "group_by": [], "order_by": [], "limit": int(limit), "_planner": "dynamic_rules"}

    plan_tables: List[str] = [base]
    plan_select: List[str] = []
    plan_joins: List[dict] = []
    plan_where: List[dict] = []
    plan_order: List[dict] = []

    # Select fields (dynamic)
    if "loanno" in q or "loan no" in q:
        c = _pick_col(cols, [f"{base}.LOANNO", "SCOTT.DW_GC_UPDATE_LOAN_VIEW.LOANNO"])
        if c:
            plan_select.append(c)

    if "balance" in q or "outstanding" in q or "due" in q:
        c = _pick_col(cols, [
            f"{base}.BALANCE",
            "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BALANCE",
            "SCOTT.DW_GC_UPDATESAVINGS.BALANCE",
            "SCOTT.DW_GC_SAVINGREGISTER.BALANCE",
        ])
        if c:
            plan_select.append(c)

    # include LOANEENO if loan/savings/member context
    if wants_member or wants_loan or wants_savings or "loaneeno" in q:
        c = _pick_col(cols, [f"{base}.LOANEENO", "SCOTT.DW_GC_UPDATE_LOAN_VIEW.LOANEENO", "SCOTT.DW_GC_LOAN_REGISTER_VIEW.LOANEENO"])
        if c:
            plan_select.append(c)

    # Member join (dynamic)
    if wants_member and "SCOTT.DW_GC_LOANEE_V" in tables:
        plan_tables.append("SCOTT.DW_GC_LOANEE_V")
        left = _pick_col(cols, [f"{base}.LOANEENO"])
        right = _pick_col(cols, ["SCOTT.DW_GC_LOANEE_V.LOANEECODE"])
        if left and right:
            plan_joins.append({"left": left, "right": right})

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

    # WHERE (branch code)
    bc = _extract_branch_code(q)
    if bc:
        c = _pick_col(cols, [f"{base}.BRANCHCODE", "SCOTT.DW_GC_LOANEE_V.BRANCHCODE"])
        if c:
            plan_where.append({"col": c, "op": "=", "value": bc})

    # WHERE (loanno prefix)
    loanno_prefix = _extract_like_prefix(q, "loanno")
    if loanno_prefix:
        c = _pick_col(cols, [f"{base}.LOANNO", "SCOTT.DW_GC_UPDATE_LOAN_VIEW.LOANNO"])
        if c:
            plan_where.append({"col": c, "op": "LIKE", "value": f"{loanno_prefix}%"})

    # ORDER / TOP
    top_n = _extract_top_n(q)
    if top_n is not None:
        limit = top_n

    if "top" in q and ("balance" in q or "outstanding" in q or "due" in q):
        c = _pick_col(cols, [f"{base}.BALANCE", "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BALANCE"])
        if c:
            plan_order.append({"col": c, "dir": "DESC"})

    # De-dup
    seen = set()
    plan_tables = [t for t in plan_tables if not (t in seen or seen.add(t))]
    seen = set()
    plan_select = [c for c in plan_select if not (c in seen or seen.add(c))]

    if not plan_select:
        # If user didn't specify any column, pick a safe minimal set
        c1 = _pick_col(cols, [f"{base}.LOANEENO"])
        c2 = _pick_col(cols, [f"{base}.BRANCHCODE"])
        for c in (c1, c2):
            if c:
                plan_select.append(c)

    return {
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
# Plan -> SQL (Oracle 11g)
# =========================================================
def _table_of(col: str) -> str:
    parts = col.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:2])  # SCHEMA.TABLE
    if len(parts) == 2:
        return parts[0]
    return ""


def _plan_to_sql(plan: Dict[str, Any], allowed_cols: Set[str], allowed_tables: Set[str], default_limit: int, max_limit: int) -> str:
    tables = [str(x).upper().strip() for x in (plan.get("tables") or []) if str(x).strip()]
    select = [str(x).upper().strip() for x in (plan.get("select") or []) if str(x).strip()]
    joins = plan.get("joins") or []
    where = plan.get("where") or []
    group_by = [str(x).upper().strip() for x in (plan.get("group_by") or []) if str(x).strip()]
    order_by = plan.get("order_by") or []
    limit = int(plan.get("limit") or default_limit)

    if not tables or not select:
        raise ValueError("Plan missing tables/select.")

    # Validate against parsed schema (strong allow-list)
    for t in tables:
        if t not in allowed_tables:
            raise ValueError(f"Table not allowed: {t}")
    for c in select:
        if c not in allowed_cols:
            raise ValueError(f"Select not allowed: {c}")
    for g in group_by:
        if g not in allowed_cols:
            raise ValueError(f"Group-by not allowed: {g}")

    base_sql = "SELECT " + ", ".join(select) + " FROM " + tables[0]
    used_tables = {tables[0]}

    # JOINs (only join tables listed in plan.tables)
    for j in joins:
        left = str((j or {}).get("left") or "").upper().strip()
        right = str((j or {}).get("right") or "").upper().strip()
        if not left or not right:
            continue
        if left not in allowed_cols or right not in allowed_cols:
            raise ValueError("Join columns not allowed.")

        lt = _table_of(left)
        rt = _table_of(right)
        candidates = [t for t in (lt, rt) if t and t in tables and t not in used_tables]
        if not candidates:
            continue
        join_table = candidates[0]
        base_sql += f" JOIN {join_table} ON {left} = {right}"
        used_tables.add(join_table)

    # WHERE
    clauses: List[str] = []
    allowed_ops = {"=", "!=", ">", ">=", "<", "<=", "LIKE", "IS_NOT_NULL", "IS_NULL", "NOT_EMPTY"}
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
    r = requests.post(
        CFG.NIFI_CORE_URL,
        json={"sql": sql},
        timeout=CFG.NIFI_TIMEOUT,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    if not r.ok:
        raise RuntimeError(f"NiFi error {r.status_code}: {r.text[:1000]}")
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:4000]}


# =========================================================
# Pretty UI response helper
# =========================================================
def _pretty_answer(question: str, plan: Dict[str, Any], sql: str, result: Optional[JsonT]) -> str:
    tables = plan.get("tables") or []
    select = plan.get("select") or []
    order_by = plan.get("order_by") or []
    limit = plan.get("limit")

    lines: List[str] = []
    lines.append("✅ Query executed successfully." if result is not None else "✅ Query generated successfully.")
    lines.append("")
    lines.append("What I did:")
    lines.append(f"- Question: {question}")
    lines.append(f"- Planner: {plan.get('_planner', 'unknown')}")
    lines.append(f"- Table(s): {', '.join(tables) if tables else '(none)'}")
    lines.append(f"- Columns: {', '.join(select) if select else '(none)'}")

    if order_by:
        ob = []
        for x in order_by:
            c = (x or {}).get("col")
            d = (x or {}).get("dir", "ASC")
            if c:
                ob.append(f"{c} {d}")
        if ob:
            lines.append(f"- Order: {', '.join(ob)}")

    lines.append(f"- Limit: {limit} rows (Oracle 11g uses ROWNUM)")
    lines.append("")
    lines.append("SQL (Oracle 11g):")
    lines.append(sql)

    if isinstance(result, list) and result:
        lines.append("")
        lines.append("Preview:")
        for row in result[: CFG.PREVIEW_ROWS]:
            if isinstance(row, dict):
                items = []
                for k, v in list(row.items())[:6]:
                    items.append(f"{k}={v if v is not None else '(null)'}")
                lines.append("- " + " | ".join(items))

    return "\n".join(lines).strip()


# =========================================================
# API: Natural Language -> Dynamic Plan -> Oracle SQL -> (optional) NiFi
# =========================================================
@csrf_exempt
@require_POST
def core_ask(request):
    """
    POST JSON:
      {
        "question": "Show LOANNO, BALANCE, LOANEENO with member FIRSTNAME, LASTNAME and PHONENO for top 10 by BALANCE",
        "limit": 10,
        "execute": false,
        "pretty": true
      }

    execute=false => returns plan + oracle11g sql
    execute=true  => sends {"sql": "..."} to NiFi and returns result
    """
    deny = _deny_core(request)
    if deny:
        return deny

    body = _json_body(request)
    question = _clean(body.get("question") or body.get("message"))
    if not question:
        return JsonResponse({"ok": False, "error": "question is required"}, status=400)

    req_limit = _safe_limit(body.get("limit"), CFG.DEFAULT_LIMIT, CFG.MAX_LIMIT)
    execute = _safe_bool(body.get("execute"), default=True)
    pretty = _safe_bool(body.get("pretty"), default=True)

    try:
        # 1) Pull ALL schema context (you currently have small number of schema points)
        schema_context = _fetch_all_core_schema_context(limit=600)
        if not schema_context:
            return JsonResponse(
                {"ok": True, "answer": "ডেটা অপর্যাপ্ত। (core_schema পাওয়া যায়নি)", "plan": None, "sql": None, "executed": False}
            )

        # 2) Pull join rules (optional)
        join_rules_text = _fetch_join_rules_master()

        # 3) Build dynamic plan (rule-based) always
        plan = _dynamic_plan(question, schema_context, join_rules_text, req_limit)

        # 4) OPTIONAL: let Aya refine the plan (never block if it fails)
        if CFG.USE_LLM_PLANNER:
            prompt = PROMPT_TEMPLATE_NL.format(schema_context=schema_context, join_rules=join_rules_text, question=question)
            llm_plan = _ollama_chat_json(SYSTEM_PROMPT_CORE_PLAN, prompt)
            # accept only if it looks valid
            if isinstance(llm_plan, dict) and llm_plan.get("tables") and llm_plan.get("select"):
                llm_plan["limit"] = int(req_limit)
                llm_plan["_planner"] = "aya_json_plan"
                plan = llm_plan

        # 5) Build SQL from plan using schema-derived allow-list
        idx = _parse_schema_index(schema_context)
        sql = _plan_to_sql(plan, allowed_cols=idx["cols"], allowed_tables=idx["tables"], default_limit=req_limit, max_limit=CFG.MAX_LIMIT)

        if not execute:
            resp: Dict[str, Any] = {
                "ok": True,
                "question": question,
                "plan": plan,
                "sql": sql,
                "executed": False,
                "nifi_url": CFG.NIFI_CORE_URL,
            }
            if pretty:
                resp["answer_pretty"] = _pretty_answer(question, plan, sql, result=None)
            if CFG.DEBUG:
                resp["debug"] = {
                    "schema_tables_count": len(idx["tables"]),
                    "schema_cols_count": len(idx["cols"]),
                    "join_rules_present": bool(join_rules_text),
                }
            return JsonResponse(resp)

        # 6) Execute via NiFi
        result = _nifi_exec(sql)

        resp2: Dict[str, Any] = {
            "ok": True,
            "question": question,
            "plan": plan,
            "sql": sql,
            "result": result,
            "executed": True,
            "nifi_url": CFG.NIFI_CORE_URL,
        }

        if pretty:
            preview_list = None
            if isinstance(result, list):
                preview_list = result
            elif isinstance(result, dict):
                for k in ("rows", "data", "result"):
                    if isinstance(result.get(k), list):
                        preview_list = result.get(k)
                        break
            resp2["answer_pretty"] = _pretty_answer(question, plan, sql, result=preview_list)

        return JsonResponse(resp2)

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Core backend error: {e}"}, status=502)
