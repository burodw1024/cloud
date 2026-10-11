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
        {"ok": False, "error": "Access denied.", "required_groups": sorted(CORE_ALLOWED_GROUPS), "your_groups": sorted(groups)[:50]},
        status=403,
    )


# =========================================================
# Env helpers
# =========================================================
def _env(name: str, default: str) -> str:
    return (os.getenv(name, default) or "").strip()


def _env_int(name: str, default: str) -> int:
    try:
        return int(_env(name, default))
    except Exception:
        return int(default)


def _env_float(name: str, default: str) -> float:
    try:
        return float(_env(name, default))
    except Exception:
        return float(default)


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

    GEN_MODEL: str = _env("GEN_MODEL", "aya-23-8b").strip() or "aya-23-8b"
    EMBED_MODEL: str = _env("EMBED_MODEL", "nomic-embed-text:latest").strip() or "nomic-embed-text:latest"

    CORE_COLLECTION: str = _env("CORE_COLLECTION", "buro_core").strip() or "buro_core"
    QDRANT_VECTOR_NAME: str = (_env("QDRANT_VECTOR_NAME", "default").strip() or "default")
    TOP_K_SCHEMA: int = _env_int("TOP_K_SCHEMA", "8")  # you currently have 8 points

    GEN_TIMEOUT: float = _env_float("GEN_TIMEOUT", "1200")
    EMBED_TIMEOUT: float = _env_float("EMBED_TIMEOUT", "600")
    QDRANT_TIMEOUT: float = _env_float("QDRANT_TIMEOUT", "300")

    NIFI_CORE_URL: str = _env("NIFI_CORE_URL", "http://192.168.1.139:8888/core/query").strip()
    NIFI_TIMEOUT: float = _env_float("NIFI_TIMEOUT", "3000")

    DEFAULT_LIMIT: int = _env_int("CORE_DEFAULT_LIMIT", "200")
    MAX_LIMIT: int = _env_int("CORE_MAX_LIMIT", "2000")

    DEBUG: bool = (_env("CORE_DEBUG", "1") or "1").lower() in ("1", "true", "yes")  # default ON for you


CFG = Cfg()

# =========================================================
# Prompting (PLAN only)
# =========================================================
SYSTEM_PROMPT_CORE_PLAN = _env(
    "SYSTEM_PROMPT_CORE_PLAN",
    (
        "You are an Oracle 11g SQL query planner.\n"
        "You MUST use ONLY identifiers that appear in SCHEMA CONTEXT.\n"
        "Return ONLY valid JSON (no markdown, no explanation).\n"
        "Rules:\n"
        "- Produce ONE SELECT plan\n"
        "- Use SCHEMA.TABLE and SCHEMA.TABLE.COL only\n"
        "- No FETCH FIRST / OFFSET\n"
        "- For 'not empty' use op 'NOT_EMPTY'\n"
        "- Always include numeric limit\n"
        "If not possible from schema context, return {}.\n"
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

# parse loanee code
_LOANEE_CODE_RE = re.compile(r"\b(?:loanee\s*code|loanee|member)\s*[:=]?\s*([0-9]{6,15})\b", re.IGNORECASE)


def _infer_loanee_code(q: str) -> Optional[str]:
    m = _LOANEE_CODE_RE.search(q or "")
    return m.group(1).strip() if m else None


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
            "options": {"temperature": 0.0, "top_p": 0.2, "repeat_penalty": 1.1, "num_ctx": 2048, "num_predict": 240},
        },
    )
    text = _clean((data.get("message") or {}).get("content")) if isinstance(data, dict) else ""
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {"_raw": text[:1500]} if CFG.DEBUG else {}


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
            else:
                raise RuntimeError("Unknown embed response shape")

            if not isinstance(vec, list) or len(vec) != 768:
                raise RuntimeError("Embedding dim mismatch (need 768).")
            return vec
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Ollama embedding failed: {last_err}")


# =========================================================
# Qdrant
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


def _build_schema_context(hits: List[dict], max_chars: int = 24000) -> str:
    out: List[str] = []
    total = 0
    for i, h in enumerate(hits, 1):
        p = h.get("payload") or {}
        t = _clean(p.get("text") or "")
        if not t:
            continue
        block = f"[{i}] {t}"
        out.append(block)
        total += len(block) + 2
        if total >= max_chars:
            break
    return "\n\n".join(out).strip()


def _allowed_tokens(schema_context: str) -> Set[str]:
    sc = (schema_context or "").upper()
    toks = set(_TOKEN.findall(sc))
    blocked_prefixes = ("DBMS_", "UTL_", "SYS.", "SYSTEM.")
    return {t for t in toks if not t.startswith(blocked_prefixes)}


def _allowed_tables(allowed: Set[str]) -> Set[str]:
    return {t for t in allowed if t.count(".") == 1}


# =========================================================
# Deterministic fallback (correct priority!)
# =========================================================
def _fallback_plan(question: str, allowed: Set[str], allowed_tables: Set[str], limit: int) -> Optional[Dict[str, Any]]:
    ql = (question or "").lower()
    loanee_code = _infer_loanee_code(question)

    def has(x: str) -> bool:
        return x.upper() in allowed

    # 1) Loan number + balance for a specific loanee code
    if loanee_code and (("loan" in ql) or ("loanno" in ql) or ("loan no" in ql)) and ("balance" in ql):
        tbl = "SCOTT.DW_GC_UPDATE_LOAN_VIEW"
        c1 = "SCOTT.DW_GC_UPDATE_LOAN_VIEW.LOANNO"
        c2 = "SCOTT.DW_GC_UPDATE_LOAN_VIEW.BALANCE"
        wcol = "SCOTT.DW_GC_UPDATE_LOAN_VIEW.LOANEENO"
        if tbl in allowed_tables and has(c1) and has(c2) and has(wcol):
            return {
                "tables": [tbl],
                "select": [c1, c2],
                "joins": [],
                "where": [{"col": wcol, "op": "=", "value": loanee_code}],
                "group_by": [],
                "order_by": [{"col": c1, "dir": "ASC"}],
                "limit": int(limit),
            }

    # 2) Loanee phone queries
    if ("phone" in ql or "phoneno" in ql) and ("loanee" in ql or "member" in ql):
        tbl = "SCOTT.DW_GC_LOANEE_V"
        c_code = "SCOTT.DW_GC_LOANEE_V.LOANEECODE"
        c_phone = "SCOTT.DW_GC_LOANEE_V.PHONENO"
        c_fn = "SCOTT.DW_GC_LOANEE_V.FIRSTNAME"
        c_ln = "SCOTT.DW_GC_LOANEE_V.LASTNAME"
        if tbl in allowed_tables and has(c_code) and has(c_phone):
            sel = [c_code, c_phone]
            if "name" in ql:
                if has(c_fn): sel.append(c_fn)
                if has(c_ln): sel.append(c_ln)
            wh = []
            if "not empty" in ql or "not null" in ql:
                wh = [{"col": c_phone, "op": "NOT_EMPTY"}]
            return {
                "tables": [tbl],
                "select": sel,
                "joins": [],
                "where": wh,
                "group_by": [],
                "order_by": [{"col": c_code, "dir": "ASC"}],
                "limit": int(limit),
            }

    # 3) Simple loanee list ONLY if user asked list only (no loan/balance/phone)
    if ("loanee" in ql or "member" in ql) and ("loan" not in ql) and ("balance" not in ql) and ("phone" not in ql):
        tbl = "SCOTT.DW_GC_LOANEE_V"
        c = "SCOTT.DW_GC_LOANEE_V.LOANEECODE"
        if tbl in allowed_tables and has(c):
            return {
                "tables": [tbl],
                "select": [c],
                "joins": [],
                "where": [],
                "group_by": [],
                "order_by": [{"col": c, "dir": "ASC"}],
                "limit": int(limit),
            }

    return None


# =========================================================
# Plan -> Oracle 11g SQL (ROWNUM)
# =========================================================
def _plan_to_sql(plan: Dict[str, Any], allowed: Set[str], allowed_tables: Set[str], default_limit: int, max_limit: int) -> str:
    tables = [str(x).upper().strip() for x in (plan.get("tables") or []) if str(x).strip()]
    select = [str(x).upper().strip() for x in (plan.get("select") or []) if str(x).strip()]
    where = plan.get("where") or []
    order_by = plan.get("order_by") or []
    limit = int(plan.get("limit") or default_limit)

    if not tables or not select:
        raise ValueError("Plan missing tables/select.")

    for t in tables:
        if t not in allowed_tables:
            raise ValueError(f"Table not allowed: {t}")
    for c in select:
        if c not in allowed:
            raise ValueError(f"Select not allowed: {c}")

    sql = "SELECT " + ", ".join(select) + " FROM " + tables[0]

    clauses: List[str] = []
    allowed_ops = {"=", "!=", ">", ">=", "<", "<=", "LIKE", "NOT_EMPTY"}
    for w in where:
        col = str((w or {}).get("col") or "").upper().strip()
        op = str((w or {}).get("op") or "=").upper().strip()
        val = (w or {}).get("value")

        if not col or col not in allowed:
            raise ValueError("Where column not allowed.")
        if op not in allowed_ops:
            raise ValueError(f"Where op not allowed: {op}")

        if op == "NOT_EMPTY":
            clauses.append(f"TRIM({col}) IS NOT NULL AND TRIM({col}) <> ''")
        else:
            sval = ("" if val is None else str(val)).replace("'", "''")
            clauses.append(f"{col} {op} '{sval}'")

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    if order_by:
        items: List[str] = []
        for ob in order_by:
            c = str((ob or {}).get("col") or "").upper().strip()
            d = str((ob or {}).get("dir") or "ASC").upper().strip()
            if c and c in allowed:
                items.append(f"{c} {'DESC' if d == 'DESC' else 'ASC'}")
        if items:
            sql += " ORDER BY " + ", ".join(items)

    if limit <= 0:
        limit = default_limit
    if limit > max_limit:
        limit = max_limit

    return f"SELECT * FROM ({sql}) WHERE ROWNUM <= {int(limit)}"


# =========================================================
# NiFi exec
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
# API
# =========================================================
@csrf_exempt
@require_POST
def core_ask(request):
    deny = _deny_core(request)
    if deny:
        return deny

    body = _json_body(request)
    question = _clean(body.get("question") or body.get("message"))
    if not question:
        return JsonResponse({"ok": False, "error": "question is required"}, status=400)

    req_limit = _safe_limit(body.get("limit"), CFG.DEFAULT_LIMIT, CFG.MAX_LIMIT)
    execute = _safe_bool(body.get("execute"), default=True)

    try:
        # Retrieval hints to pull correct schema chunk
        ql = question.lower()
        q_for_embed = question

        if "loan" in ql or "balance" in ql or "loanno" in ql or "loan no" in ql:
            q_for_embed += " TABLE: SCOTT.DW_GC_UPDATE_LOAN_VIEW COL: SCOTT.DW_GC_UPDATE_LOAN_VIEW.LOANNO COL: SCOTT.DW_GC_UPDATE_LOAN_VIEW.BALANCE COL: SCOTT.DW_GC_UPDATE_LOAN_VIEW.LOANEENO"
        if "phone" in ql or "phoneno" in ql or "name" in ql:
            q_for_embed += " TABLE: SCOTT.DW_GC_LOANEE_V COL: SCOTT.DW_GC_LOANEE_V.PHONENO COL: SCOTT.DW_GC_LOANEE_V.FIRSTNAME COL: SCOTT.DW_GC_LOANEE_V.LASTNAME"

        vec = _ollama_embed(q_for_embed)
        hits = _qdrant_search(vec, limit=CFG.TOP_K_SCHEMA)
        schema_context = _build_schema_context(hits)

        if not schema_context:
            return JsonResponse({"ok": True, "answer": "ডেটা অপর্যাপ্ত।", "plan": None, "sql": None, "executed": False})

        allowed = _allowed_tokens(schema_context)
        allowed_tables = _allowed_tables(allowed)

        # FALLBACK FIRST (correct priority)
        plan = _fallback_plan(question, allowed=allowed, allowed_tables=allowed_tables, limit=req_limit)

        # Otherwise Aya plan
        if not plan:
            prompt = PROMPT_TEMPLATE_NL.format(schema_context=schema_context, question=question)
            plan = _ollama_chat_json(SYSTEM_PROMPT_CORE_PLAN, prompt)

        if not plan or not plan.get("tables") or not plan.get("select"):
            return JsonResponse(
                {
                    "ok": True,
                    "answer": "ডেটা অপর্যাপ্ত।",
                    "plan": None,
                    "sql": None,
                    "executed": False,
                    "debug": {"schema_context_preview": schema_context[:1200]} if CFG.DEBUG else None,
                }
            )

        plan["limit"] = req_limit
        sql = _plan_to_sql(plan, allowed=allowed, allowed_tables=allowed_tables, default_limit=req_limit, max_limit=CFG.MAX_LIMIT)

        if not execute:
            return JsonResponse(
                {
                    "ok": True,
                    "question": question,
                    "plan": plan,
                    "sql": sql,
                    "executed": False,
                    "schema_tables": sorted(list(allowed_tables)),
                    "schema_context_preview": schema_context[:1200],
                }
            )

        result = _nifi_exec(sql)
        return JsonResponse(
            {
                "ok": True,
                "question": question,
                "plan": plan,
                "sql": sql,
                "result": result,
                "executed": True,
                "schema_tables": sorted(list(allowed_tables)),
            }
        )

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Core backend error: {e}"}, status=502)
