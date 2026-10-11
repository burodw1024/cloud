# views_core.py
# Oracle 11g SQL generator + optional NiFi execution; Qdrant named vector "default"
# Adds: (1) parse "limit 20" from question, (2) Chat-UI friendly explanation from Aya (no JSON, no SQL/NiFi mention)

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Union

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
# Parse LIMIT from question: "limit 10", "top 20", "first 5"
# =========================================================
_LIMIT_RE = re.compile(r"\b(?:limit|top|first)\s*(\d{1,5})\b", re.IGNORECASE)


def _limit_from_question(question: str) -> Optional[int]:
    m = _LIMIT_RE.search(question or "")
    if not m:
        return None
    try:
        n = int(m.group(1))
        return n if n > 0 else None
    except Exception:
        return None


# =========================================================
# Config
# =========================================================
@dataclass(frozen=True)
class Cfg:
    OLLAMA_URL: str = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    QDRANT_URL: str = _env("QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")

    GEN_MODEL: str = _env("GEN_MODEL", "aya-23-8b")
    EMBED_MODEL: str = _env("EMBED_MODEL", "nomic-embed-text:latest")

    CORE_COLLECTION: str = _env("CORE_COLLECTION", "buro_core").strip() or "buro_core"
    QDRANT_VECTOR_NAME: str = (_env("QDRANT_VECTOR_NAME", "default").strip() or "default")
    TOP_K_SCHEMA: int = _env_int("TOP_K_SCHEMA", "8")

    GEN_TIMEOUT: float = _env_float("GEN_TIMEOUT", "120")
    EMBED_TIMEOUT: float = _env_float("EMBED_TIMEOUT", "60")
    QDRANT_TIMEOUT: float = _env_float("QDRANT_TIMEOUT", "30")

    # NiFi core query endpoint (POST {sql:"..."})
    NIFI_CORE_URL: str = _env("NIFI_CORE_URL", "http://192.168.1.139:8888/core/query").strip()
    NIFI_TIMEOUT: float = _env_float("NIFI_TIMEOUT", "300")

    DEFAULT_LIMIT: int = _env_int("CORE_DEFAULT_LIMIT", "200")
    MAX_LIMIT: int = _env_int("CORE_MAX_LIMIT", "2000")

    # How many rows to send to Aya to explain (keep prompts small)
    EXPLAIN_MAX_ROWS: int = _env_int("CORE_EXPLAIN_MAX_ROWS", "20")

    DEBUG: bool = (_env("CORE_DEBUG", "0") or "0").lower() in ("1", "true", "yes")


CFG = Cfg()

# =========================================================
# Prompting (Oracle 11g friendly)
# =========================================================
SYSTEM_PROMPT_CORE_PLAN = _env(
    "SYSTEM_PROMPT_CORE_PLAN",
    (
        "You are an Oracle 11g SQL query planner.\n"
        "You MUST use ONLY identifiers that appear in SCHEMA CONTEXT.\n"
        "Return ONLY valid JSON (no markdown, no explanations).\n"
        "Rules:\n"
        "- Produce one SELECT query plan.\n"
        "- Prefer simple joins.\n"
        "- Use SCHEMA.TABLE for tables.\n"
        "- Use SCHEMA.TABLE.COL for columns.\n"
        "- Always include a numeric limit.\n"
        "- Do NOT use FETCH FIRST / OFFSET in SQL.\n"
        "If not possible from schema context, return {}.\n"
        "JSON format:\n"
        "{"
        '"tables":["SCHEMA.TABLE1","SCHEMA.TABLE2"],'
        '"select":["SCHEMA.TABLE1.COL1","SCHEMA.TABLE2.COL2"],'
        '"joins":[{"left":"SCHEMA.TABLE1.COLX","right":"SCHEMA.TABLE2.COLY"}],'
        '"where":[{"col":"SCHEMA.TABLE1.COLZ","op":"=","value":"..."}],'
        '"group_by":[],'
        '"order_by":[{"col":"SCHEMA.TABLE1.COL1","dir":"ASC"}],'
        '"limit":200'
        "}\n"
    ),
)

# This is the ONLY place you control "acceptable explanation"
SYSTEM_PROMPT_CORE_EXPLAIN = _env(
    "SYSTEM_PROMPT_CORE_EXPLAIN",
    (
        "You are a helpful assistant explaining query results to an end user.\n"
        "Strict rules:\n"
        "- Do NOT mention SQL, Oracle, NiFi, schema names, tables, views, or columns.\n"
        "- Do NOT output JSON.\n"
        "- Use ONLY the provided items/values. Do not invent or calculate extra facts.\n"
        "- First write 2–3 short sentences explaining what the result is.\n"
        "- Then show the items as a clean numbered list.\n"
        "- Keep the tone professional and clear.\n"
        "Return plain text only.\n"
    ),
)

PROMPT_TEMPLATE_NL = """SCHEMA CONTEXT:
{schema_context}

USER QUESTION:
{question}

Return JSON ONLY:
""".strip()

# allow-list extraction: IDENT or IDENT.IDENT or IDENT.IDENT.IDENT (0-2 dots)
_TOKEN = re.compile(r"\b[A-Z][A-Z0-9_]{1,63}(?:\.[A-Z][A-Z0-9_]{1,63}){0,2}\b")

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
def _ollama_chat_json(system_prompt: str, user_prompt: str) -> Dict[str, Any]:
    data = _http_json(
        "POST",
        f"{CFG.OLLAMA_URL}/api/chat",
        timeout=CFG.GEN_TIMEOUT,
        json_body={
            "model": CFG.GEN_MODEL,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "stream": False,
            "options": {
                "temperature": 0.0,
                "top_p": 0.2,
                "repeat_penalty": 1.1,
                "num_ctx": 2048,
                "num_predict": 220,
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


def _ollama_chat_text(system_prompt: str, user_prompt: str) -> str:
    data = _http_json(
        "POST",
        f"{CFG.OLLAMA_URL}/api/chat",
        timeout=CFG.GEN_TIMEOUT,
        json_body={
            "model": CFG.GEN_MODEL,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "stream": False,
            "options": {
                "temperature": 0.2,
                "top_p": 0.5,
                "repeat_penalty": 1.1,
                "num_ctx": 2048,
                "num_predict": 260,
            },
        },
    )
    return _clean((data.get("message") or {}).get("content")) if isinstance(data, dict) else ""


def _ollama_embed(text: str) -> List[float]:
    last_err: Optional[Exception] = None
    for path, body in [
        ("/api/embeddings", {"model": CFG.EMBED_MODEL, "prompt": text}),
        ("/api/embed", {"model": CFG.EMBED_MODEL, "input": text}),
    ]:
        try:
            data = _http_json("POST", f"{CFG.OLLAMA_URL}{path}", timeout=CFG.EMBED_TIMEOUT, json_body=body)

            if isinstance(data, dict) and isinstance(data.get("embedding"), list):
                vec = data["embedding"]
            elif isinstance(data, dict) and isinstance(data.get("embeddings"), list):
                vec = (data.get("embeddings") or [[]])[0]
            elif isinstance(data, dict) and isinstance(data.get("data"), list):
                vec = ((data.get("data") or [{}])[0]).get("embedding")
            else:
                raise RuntimeError("Unknown embed response shape")

            if not isinstance(vec, list) or len(vec) != 768:
                raise RuntimeError(
                    f"Embedding dim mismatch: got {len(vec) if isinstance(vec, list) else 'na'} expected 768"
                )
            return vec
        except Exception as e:
            last_err = e

    raise RuntimeError(f"Ollama embedding failed: {last_err}")


# =========================================================
# Qdrant (named vector: default)
# =========================================================
def _qdrant_search(vector: List[float], limit: int) -> List[dict]:
    path = f"/collections/{CFG.CORE_COLLECTION}/points/search"
    body = {
        "vector": {"name": CFG.QDRANT_VECTOR_NAME, "vector": vector},
        "limit": int(limit),
        "with_payload": True,
        "with_vector": False,
    }
    data = _http_json("POST", f"{CFG.QDRANT_URL}{path}", timeout=CFG.QDRANT_TIMEOUT, json_body=body)
    res = data.get("result") if isinstance(data, dict) else None
    return res if isinstance(res, list) else []


def _payload_text(p: Dict[str, Any]) -> str:
    for k in ("text", "prompt", "schema", "ddl", "content"):
        v = p.get(k)
        if v:
            return _clean(v)
    v = p.get("value")
    if v:
        return _clean(v)
    return ""


def _build_schema_context(hits: List[dict]) -> str:
    out: List[str] = []
    for i, h in enumerate(hits, 1):
        p = h.get("payload") or {}
        t = _payload_text(p)
        if t:
            out.append(f"[{i}] {t}")
    return "\n\n".join(out).strip()


def _allowed_tokens(schema_context: str) -> Set[str]:
    sc = (schema_context or "").upper()
    toks = set(_TOKEN.findall(sc))
    blocked_prefixes = ("DBMS_", "UTL_", "SYS.", "SYSTEM.")
    return {t for t in toks if not t.startswith(blocked_prefixes)}


# =========================================================
# Plan->SQL (Oracle 11g)  -- allow-list only
# =========================================================
def _table_of(col: str) -> str:
    parts = col.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:2])  # SCHEMA.TABLE
    if len(parts) == 2:
        return parts[0]
    return ""


def _plan_to_sql(plan: Dict[str, Any], allowed: Set[str], default_limit: int, max_limit: int) -> str:
    tables = [str(x).upper().strip() for x in (plan.get("tables") or []) if str(x).strip()]
    select = [str(x).upper().strip() for x in (plan.get("select") or []) if str(x).strip()]
    joins = plan.get("joins") or []
    where = plan.get("where") or []
    group_by = [str(x).upper().strip() for x in (plan.get("group_by") or []) if str(x).strip()]
    order_by = plan.get("order_by") or []
    limit = int(plan.get("limit") or default_limit)

    if not tables or not select:
        raise ValueError("Plan missing tables/select.")

    # Allow-list validation (schema_context-driven)
    for t in tables:
        if t not in allowed:
            raise ValueError(f"Table not allowed: {t}")
    for c in select:
        if c not in allowed:
            raise ValueError(f"Select not allowed: {c}")
    for g in group_by:
        if g not in allowed:
            raise ValueError(f"Group-by not allowed: {g}")

    base_sql = "SELECT " + ", ".join(select) + " FROM " + tables[0]
    used_tables = {tables[0]}

    # JOINs
    for j in joins:
        left = str((j or {}).get("left") or "").upper().strip()
        right = str((j or {}).get("right") or "").upper().strip()
        if not left or not right:
            continue
        if left not in allowed or right not in allowed:
            raise ValueError("Join columns not allowed.")

        lt = _table_of(left)
        rt = _table_of(right)
        candidates = [t for t in (lt, rt) if t and t in tables and t not in used_tables]
        if not candidates:
            continue
        join_table = candidates[0]
        base_sql += f" JOIN {join_table} ON {left} = {right}"
        used_tables.add(join_table)

    # WHERE (simple ops) — keep string literals only to avoid breaking existing behavior
    # (If you later want numeric/date support, add a typed literal function.)
    clauses: List[str] = []
    allowed_ops = {"=", "!=", ">", ">=", "<", "<=", "LIKE"}
    for w in where:
        col = str((w or {}).get("col") or "").upper().strip()
        op = str((w or {}).get("op") or "=").upper().strip()
        val = (w or {}).get("value")
        if not col or col not in allowed:
            raise ValueError("Where column not allowed.")
        if op not in allowed_ops:
            raise ValueError("Where op not allowed.")
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
            if col and col in allowed:
                items.append(f"{col} {'DESC' if direc == 'DESC' else 'ASC'}")
        if items:
            base_sql += " ORDER BY " + ", ".join(items)

    # Oracle 11g limit wrapper (ROWNUM)
    if limit <= 0:
        limit = default_limit
    if limit > max_limit:
        limit = max_limit

    return f"SELECT * FROM ({base_sql}) WHERE ROWNUM <= {int(limit)}"


# =========================================================
# Deterministic fallback plans (no LLM) for common list queries
# =========================================================
def _fallback_plan_for_common_lists(question: str, allowed: Set[str], limit: int) -> Optional[Dict[str, Any]]:
    q = (question or "").lower()

    # Loanee/member list -> LOANEECODE only
    if ("loanee" in q) or ("member" in q):
        tbl = "SCOTT.DW_GC_LOANEE_V"
        col = "SCOTT.DW_GC_LOANEE_V.LOANEECODE"
        if tbl in allowed and col in allowed:
            return {
                "tables": [tbl],
                "select": [col],
                "joins": [],
                "where": [],
                "group_by": [],
                "order_by": [{"col": col, "dir": "ASC"}],
                "limit": int(limit),
            }

    # Saving account list -> SAVINGACCOUNTNO only (avoid broad "account" to reduce false matches)
    if ("saving" in q) or ("savings" in q) or ("saving account" in q):
        tbl = "SCOTT.DW_GC_UPDATE_SAVINGS_VIEW"
        col = "SCOTT.DW_GC_UPDATE_SAVINGS_VIEW.SAVINGACCOUNTNO"
        if tbl in allowed and col in allowed:
            return {
                "tables": [tbl],
                "select": [col],
                "joins": [],
                "where": [],
                "group_by": [],
                "order_by": [{"col": col, "dir": "ASC"}],
                "limit": int(limit),
            }

    return None


# =========================================================
# NiFi execution
# =========================================================
def _nifi_exec(sql: str) -> JsonT:
    body = {"sql": sql}
    r = requests.post(
        CFG.NIFI_CORE_URL,
        json=body,
        timeout=CFG.NIFI_TIMEOUT,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    if not r.ok:
        raise RuntimeError(f"NiFi error {r.status_code}: {r.text[:1000]}")
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:4000]}


def _result_preview(result: JsonT, max_rows: int) -> Dict[str, Any]:
    # list-of-rows
    if isinstance(result, list):
        rows = result[:max_rows]
        cols: List[str] = []
        if rows and all(isinstance(r, dict) for r in rows):
            cols = sorted(set().union(*[set(r.keys()) for r in rows]))  # type: ignore[arg-type]
        return {"rows": rows, "rows_shown": len(rows), "rows_total": len(result), "columns": cols}

    # dict-of-rows
    if isinstance(result, dict):
        for key in ("result", "rows", "data"):
            if isinstance(result.get(key), list):
                all_rows = result[key]
                rows = all_rows[:max_rows]
                cols: List[str] = []
                if rows and all(isinstance(r, dict) for r in rows):
                    cols = sorted(set().union(*[set(r.keys()) for r in rows]))  # type: ignore[arg-type]
                return {"rows": rows, "rows_shown": len(rows), "rows_total": len(all_rows), "columns": cols}

        keys = list(result.keys())[:30]
        return {"rows": [], "rows_shown": 0, "rows_total": 0, "columns": [], "note": f"Non-tabular keys: {keys}"}

    return {"rows": [], "rows_shown": 0, "rows_total": 0, "columns": [], "note": "Unknown result shape"}


def _make_explain_prompt(question: str, preview: Dict[str, Any], req_limit: int) -> str:
    """
    IMPORTANT: This prompt is designed so Aya produces an "acceptable explanation"
    without leaking JSON / debug labels / SQL terms.
    """
    rows = preview.get("rows") or []
    cols = preview.get("columns") or []
    shown = int(preview.get("rows_shown") or 0)

    # For single-column lists, provide only values (not JSON)
    values: List[str] = []
    if len(cols) == 1:
        col = cols[0]
        for r in rows[:min(len(rows), req_limit)]:
            if isinstance(r, dict) and col in r:
                values.append(str(r[col]))
    else:
        # Multi-column: provide a compact text-only sample
        for r in rows[: min(len(rows), 8)]:
            if isinstance(r, dict):
                kv = ", ".join([f"{k}={r.get(k)}" for k in list(r.keys())[:8]])
                values.append(kv)

    sample_text = "\n".join(values)

    return (
        "Write a user-facing explanation.\n"
        "Output format:\n"
        "1) 2–3 short sentences explaining what this result shows.\n"
        "2) Then a numbered list of the items shown.\n"
        "Rules:\n"
        "- Do NOT mention SQL/Oracle/NiFi/schema/table/view/column names.\n"
        "- Do NOT output JSON.\n"
        "- Use ONLY the items provided below.\n"
        "- Respect the limit implied by the question; list only the items provided.\n"
        "\n"
        f"USER QUESTION: {question}\n"
        f"ITEMS SHOWN: {shown}\n"
        "ITEMS:\n"
        f"{sample_text}\n"
    )


# =========================================================
# API: Natural Language -> Plan -> Oracle SQL -> (optional) NiFi + explanation
# =========================================================
@csrf_exempt
@require_POST
def core_ask(request):
    """
    POST JSON:
      {
        "question": "List of loanee limit 20",
        "limit": 20,          # optional (overrides text)
        "execute": true       # default true
      }

    execute=false => returns plan + oracle11g sql
    execute=true  => sends {"sql": "..."} to NiFi and returns preview + answer
    """
    deny = _deny_core(request)
    if deny:
        return deny

    body = _json_body(request)
    question = _clean(body.get("question") or body.get("message"))
    if not question:
        return JsonResponse({"ok": False, "error": "question is required"}, status=400)

    # limit priority: JSON limit > parsed from question > default
    q_limit = _limit_from_question(question)
    raw_limit = body.get("limit")
    req_limit = _safe_limit(raw_limit if raw_limit is not None else (q_limit or CFG.DEFAULT_LIMIT),
                            CFG.DEFAULT_LIMIT, CFG.MAX_LIMIT)

    execute = _safe_bool(body.get("execute"), default=True)

    try:
        # retrieval boost
        q_for_embed = question
        ql = q_for_embed.lower()
        if ("loanee" in ql) or ("member" in ql):
            q_for_embed += " SCOTT.DW_GC_LOANEE_V LOANEECODE"
        elif ("saving" in ql) or ("savings" in ql):
            q_for_embed += " SCOTT.DW_GC_UPDATE_SAVINGS_VIEW SAVINGACCOUNTNO BRANCHCODE"
        elif ("loan" in ql) or ("disburse" in ql) or ("balance" in ql):
            q_for_embed += " SCOTT.DW_GC_UPDATE_LOAN_VIEW LOANNO BRANCHCODE LOANEENO"

        vec = _ollama_embed(q_for_embed)
        hits = _qdrant_search(vec, limit=CFG.TOP_K_SCHEMA)
        schema_context = _build_schema_context(hits)

        if not schema_context:
            return JsonResponse(
                {
                    "ok": True,
                    "answer": "ডেটা অপর্যাপ্ত।",
                    "plan": None,
                    "sql": None,
                    "result": None,
                    "executed": False,
                    "debug": {"hint": "Qdrant returned no usable schema text.", "collection": CFG.CORE_COLLECTION},
                }
            )

        allowed = _allowed_tokens(schema_context)

        # deterministic fallback first
        plan = _fallback_plan_for_common_lists(question, allowed=allowed, limit=req_limit)

        # otherwise LLM plan
        if not plan:
            prompt = PROMPT_TEMPLATE_NL.format(schema_context=schema_context, question=question)
            plan = _ollama_chat_json(SYSTEM_PROMPT_CORE_PLAN, prompt)

        if not plan or not isinstance(plan, dict):
            return JsonResponse(
                {
                    "ok": True,
                    "answer": "ডেটা অপর্যাপ্ত।",
                    "plan": None,
                    "sql": None,
                    "result": None,
                    "executed": False,
                    "debug": {"hint": "Model returned empty/invalid JSON plan.", "schema_context_preview": schema_context[:1200]},
                }
            )

        # enforce final limit
        plan["limit"] = req_limit
        sql = _plan_to_sql(plan, allowed=allowed, default_limit=req_limit, max_limit=CFG.MAX_LIMIT)

        if not execute:
            return JsonResponse(
                {
                    "ok": True,
                    "question": question,
                    "plan": plan,
                    "sql": sql,
                    "schema_context_preview": schema_context[:1200],
                    "nifi_url": CFG.NIFI_CORE_URL,
                    "executed": False,
                }
            )

        raw_result = _nifi_exec(sql)
        preview = _result_preview(raw_result, max_rows=min(req_limit, CFG.EXPLAIN_MAX_ROWS))

        explain_prompt = _make_explain_prompt(question, preview, req_limit=req_limit)
        answer = _ollama_chat_text(SYSTEM_PROMPT_CORE_EXPLAIN, explain_prompt) or "Result retrieved."

        return JsonResponse(
            {
                "ok": True,
                "question": question,
                "answer": answer,       # ✅ Chat UI should display this
                "plan": plan,
                "sql": sql,
                "result": preview,      # ✅ preview only (avoid huge JSON in UI)
                "executed": True,
                "nifi_url": CFG.NIFI_CORE_URL,
            }
        )

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Core backend error: {e}"}, status=502)
