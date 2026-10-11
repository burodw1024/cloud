# chat/views_upload_doc.py
# ------------------------------------------------------------
# STRICT production-ready uploaded document summarizer / Q&A
#
# Existing behavior preserved:
# ✅ PDF/DOC/TXT extracted_text based summarization
# ✅ XLS/XLSX/CSV structured analysis
# ✅ Column-wise numeric summary
# ✅ Missing value analysis
# ✅ Duplicate row warning
# ✅ Top values for text columns
# ✅ Correlation / relationship hints
# ✅ Management decision recommendation
# ✅ Bad Excel LLM-answer detection + deterministic fallback
# ✅ Deterministic extractive fallback for normal documents
#
# New extension added safely:
# ✅ Qdrant vector collection for uploaded document chunks
# ✅ One shared collection: uploaded_file_chunks
# ✅ Payload isolation by source_type + file_id + user_id
# ✅ First request indexes extracted_text if not already indexed
# ✅ Summary questions use ordered document chunks
# ✅ Normal questions use semantic vector search
# ✅ If vector indexing/search fails, existing full-text behavior still works
# ✅ Output cleanup prevents EOS/context leakage in vector Q&A
# ✅ Separate prompts for summary vs specific Q&A
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, List, Tuple

import requests
import pandas as pd

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import UploadedFile


# Optional Qdrant imports.
# The feature fails open: if qdrant-client is not installed or Qdrant is down,
# existing document summarization still works using extracted_text.
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance,
        VectorParams,
        PointStruct,
        Filter,
        FieldCondition,
        MatchValue,
    )

    QDRANT_IMPORT_OK = True
    QDRANT_IMPORT_ERROR = ""
except Exception as _qdrant_import_exc:  # pragma: no cover
    QdrantClient = None
    Distance = None
    VectorParams = None
    PointStruct = None
    Filter = None
    FieldCondition = None
    MatchValue = None
    QDRANT_IMPORT_OK = False
    QDRANT_IMPORT_ERROR = str(_qdrant_import_exc)


# -------------------------
# Config
# -------------------------
def _env(k: str, default: str) -> str:
    return (os.getenv(k, default) or "").strip()


def _env_int(k: str, default: str) -> int:
    try:
        return int(_env(k, default))
    except Exception:
        return int(default)


def _env_float(k: str, default: str) -> float:
    try:
        return float(_env(k, default))
    except Exception:
        return float(default)


def _env_bool(k: str, default: str = "0") -> bool:
    return _env(k, default).lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class Cfg:
    # Existing LLM config
    OLLAMA_URL: str = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    GEN_MODEL: str = _env("GEN_MODEL", "aya-23-8b:q5").strip() or "aya-23-8b:q5"
    GEN_TIMEOUT: float = _env_float("GEN_TIMEOUT", "6000")

    DOC_MAX_CHARS: int = _env_int("DOC_MAX_CHARS", "180000000")
    EXCEL_MAX_PROFILE_CHARS: int = _env_int("EXCEL_MAX_PROFILE_CHARS", "120000")
    EXCEL_SAMPLE_ROWS: int = _env_int("EXCEL_SAMPLE_ROWS", "20")

    DOC_REQUIRE_AUTH: bool = (_env("DOC_REQUIRE_AUTH", "1").lower() not in ("0", "false", "no"))
    DOC_ALLOWED_GROUPS_CSV: str = _env("DOC_ALLOWED_GROUPS", "")

    # New uploaded-file vector config
    UPLOAD_VECTOR_ENABLED: bool = _env_bool("UPLOAD_VECTOR_ENABLED", "1")
    UPLOAD_VECTOR_COLLECTION: str = _env("UPLOAD_VECTOR_COLLECTION", "uploaded_file_chunks")

    # Use QDRANT_URL if available, otherwise QDRANT_HOST/QDRANT_PORT.
    # Examples:
    #   QDRANT_URL=http://192.168.1.144:6333
    #   QDRANT_HOST=192.168.1.144
    #   QDRANT_PORT=6333
    QDRANT_URL: str = _env("QDRANT_URL", "").rstrip("/")
    QDRANT_HOST: str = _env("QDRANT_HOST", "192.168.1.144")
    QDRANT_PORT: int = _env_int("QDRANT_PORT", "6333")
    QDRANT_API_KEY: str = _env("QDRANT_API_KEY", "")
    QDRANT_TIMEOUT: float = _env_float("QDRANT_TIMEOUT", "30")

    UPLOAD_EMBED_MODEL: str = _env("UPLOAD_EMBED_MODEL", _env("EMBED_MODEL", "nomic-embed-text"))
    UPLOAD_VECTOR_SIZE: int = _env_int("UPLOAD_VECTOR_SIZE", "768")
    UPLOAD_CHUNK_SIZE: int = _env_int("UPLOAD_CHUNK_SIZE", "3000")
    UPLOAD_CHUNK_OVERLAP: int = _env_int("UPLOAD_CHUNK_OVERLAP", "300")
    UPLOAD_TOP_K: int = _env_int("UPLOAD_TOP_K", "5")
    UPLOAD_SUMMARY_CHUNK_LIMIT: int = _env_int("UPLOAD_SUMMARY_CHUNK_LIMIT", "20")
    UPLOAD_SCROLL_MAX: int = _env_int("UPLOAD_SCROLL_MAX", "1000")
    UPLOAD_UPSERT_BATCH_SIZE: int = _env_int("UPLOAD_UPSERT_BATCH_SIZE", "64")
    UPLOAD_REINDEX_ON_EACH_ASK: bool = _env_bool("UPLOAD_REINDEX_ON_EACH_ASK", "0")

    # Generation length controls for uploaded-file vector path
    UPLOAD_QA_NUM_PREDICT: int = _env_int("UPLOAD_QA_NUM_PREDICT", "500")
    UPLOAD_SUMMARY_NUM_PREDICT: int = _env_int("UPLOAD_SUMMARY_NUM_PREDICT", "1200")


CFG = Cfg()


# -------------------------
# Basic helpers
# -------------------------
def _json_body(request) -> Dict[str, Any]:
    try:
        raw = (request.body or b"{}").decode("utf-8", errors="replace")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _clean(v: Any) -> str:
    return ("" if v is None else str(v)).strip()


def _user_group_names(user) -> set:
    try:
        if not user or not getattr(user, "is_authenticated", False):
            return set()
        return set(user.groups.values_list("name", flat=True))
    except Exception:
        return set()


def _get_user_id_str(request) -> str:
    try:
        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False):
            return str(user.id)
    except Exception:
        pass
    return ""


def _authz_or_error(request) -> Optional[JsonResponse]:
    if not CFG.DOC_REQUIRE_AUTH:
        return None

    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

    if getattr(user, "is_superuser", False):
        return None

    allow_csv = (CFG.DOC_ALLOWED_GROUPS_CSV or "").strip()
    if not allow_csv:
        return None

    allowed = {x.strip() for x in allow_csv.split(",") if x.strip()}
    groups = _user_group_names(user)

    if not (groups & allowed):
        return JsonResponse(
            {
                "ok": False,
                "error": "Not allowed for document summary.",
                "hint": f"User must belong to one of groups: {sorted(list(allowed))}",
                "user_groups": sorted(list(groups)),
            },
            status=403,
        )

    return None


def _clean_pdf_artifacts(text: str) -> str:
    t = text or ""
    t = re.sub(r"\(cid:\d+\)", "•", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _get_uploaded_file(file_token: str) -> Optional[UploadedFile]:
    tok = _clean(file_token)
    if not tok:
        return None

    qs = UploadedFile.objects.all()

    try:
        if hasattr(UploadedFile, "file_id"):
            uf = qs.filter(file_id=tok).first()
            if uf:
                return uf
    except Exception:
        pass

    try:
        pk = int(tok)
        return qs.filter(id=pk).first()
    except Exception:
        return None


def _get_file_token(uf: UploadedFile) -> str:
    return _clean(getattr(uf, "file_id", None) or str(getattr(uf, "id", "")))


def _get_uploaded_file_path(uf: UploadedFile) -> Optional[str]:
    """
    Tries common FileField names.
    If your UploadedFile model uses another field, add it here.
    """

    possible_fields = [
        "file",
        "uploaded_file",
        "document",
        "attachment",
        "upload",
    ]

    for field_name in possible_fields:
        try:
            f = getattr(uf, field_name, None)
            if f:
                path = getattr(f, "path", None)
                if path and os.path.exists(path):
                    return path
        except Exception:
            pass

    try:
        path = getattr(uf, "path", None)
        if path and os.path.exists(path):
            return path
    except Exception:
        pass

    return None


def _get_uploaded_filename(uf: UploadedFile) -> str:
    for attr in ["filename", "original_name", "name"]:
        try:
            val = getattr(uf, attr, None)
            if val:
                return str(val)
        except Exception:
            pass

    for field_name in ["file", "uploaded_file", "document", "attachment", "upload"]:
        try:
            f = getattr(uf, field_name, None)
            if f:
                name = getattr(f, "name", "")
                if name:
                    return os.path.basename(name)
        except Exception:
            pass

    return f"uploaded_file_{getattr(uf, 'id', '')}"


# -------------------------
# Uploaded-file vector store helpers
# -------------------------
_QDRANT_CLIENT = None


def _vector_enabled() -> bool:
    return bool(CFG.UPLOAD_VECTOR_ENABLED and QDRANT_IMPORT_OK)


def _get_qdrant_client():
    global _QDRANT_CLIENT

    if not _vector_enabled():
        raise RuntimeError(f"Qdrant vector feature unavailable. import_error={QDRANT_IMPORT_ERROR}")

    if _QDRANT_CLIENT is not None:
        return _QDRANT_CLIENT

    kwargs = {"timeout": CFG.QDRANT_TIMEOUT}
    if CFG.QDRANT_API_KEY:
        kwargs["api_key"] = CFG.QDRANT_API_KEY

    if CFG.QDRANT_URL:
        _QDRANT_CLIENT = QdrantClient(url=CFG.QDRANT_URL, **kwargs)
    else:
        _QDRANT_CLIENT = QdrantClient(host=CFG.QDRANT_HOST, port=CFG.QDRANT_PORT, **kwargs)

    return _QDRANT_CLIENT


def _ensure_uploaded_file_collection() -> None:
    client = _get_qdrant_client()

    collections = client.get_collections().collections
    existing = {c.name for c in collections}

    if CFG.UPLOAD_VECTOR_COLLECTION not in existing:
        client.create_collection(
            collection_name=CFG.UPLOAD_VECTOR_COLLECTION,
            vectors_config=VectorParams(size=CFG.UPLOAD_VECTOR_SIZE, distance=Distance.COSINE),
        )


def _embed_text(text: str) -> List[float]:
    """
    Ollama embedding wrapper.
    Primary endpoint: /api/embeddings.
    Fallback endpoint: /api/embed for newer Ollama versions.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Cannot embed empty text.")

    # Legacy/stable endpoint
    r = requests.post(
        f"{CFG.OLLAMA_URL}/api/embeddings",
        json={"model": CFG.UPLOAD_EMBED_MODEL, "prompt": text},
        timeout=120,
        headers={"Accept": "application/json"},
    )

    if r.status_code < 400:
        data = r.json()
        emb = data.get("embedding")
        if emb:
            return emb

    # Newer endpoint fallback
    r2 = requests.post(
        f"{CFG.OLLAMA_URL}/api/embed",
        json={"model": CFG.UPLOAD_EMBED_MODEL, "input": text},
        timeout=120,
        headers={"Accept": "application/json"},
    )

    if r2.status_code >= 400:
        raise RuntimeError(f"Ollama embedding failed: {r.status_code} {r.text[:300]} | {r2.status_code} {r2.text[:300]}")

    data2 = r2.json()
    embeddings = data2.get("embeddings")
    if isinstance(embeddings, list) and embeddings:
        first = embeddings[0]
        if isinstance(first, list) and first:
            return first

    raise RuntimeError("No embedding returned from Ollama.")


def _chunk_text(text: str, chunk_size: int = 3000, overlap: int = 300) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []

    chunk_size = max(500, int(chunk_size or 3000))
    overlap = max(0, min(int(overlap or 0), chunk_size - 100))

    chunks: List[str] = []
    start = 0

    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start

    return chunks


def _uploaded_file_filter(file_id: str, user_id: str = ""):
    must = [
        FieldCondition(key="source_type", match=MatchValue(value="uploaded_file")),
        FieldCondition(key="file_id", match=MatchValue(value=str(file_id))),
    ]

    # Important security isolation. If your old anonymous uploads have empty user_id,
    # this keeps anonymous uploads searchable only as anonymous.
    must.append(FieldCondition(key="user_id", match=MatchValue(value=str(user_id or ""))))

    return Filter(must=must)


def _count_existing_file_chunks(file_id: str, user_id: str = "") -> int:
    client = _get_qdrant_client()
    _ensure_uploaded_file_collection()

    try:
        res = client.count(
            collection_name=CFG.UPLOAD_VECTOR_COLLECTION,
            count_filter=_uploaded_file_filter(file_id, user_id),
            exact=True,
        )
        return int(getattr(res, "count", 0) or 0)
    except Exception:
        # Fallback count using scroll limit 1.
        points, _ = client.scroll(
            collection_name=CFG.UPLOAD_VECTOR_COLLECTION,
            scroll_filter=_uploaded_file_filter(file_id, user_id),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        return len(points)


def _delete_existing_file_chunks(file_id: str, user_id: str = "") -> int:
    """
    Compatibility-safe delete: scroll IDs first, then delete by IDs.
    Used only when UPLOAD_REINDEX_ON_EACH_ASK=1 or forced reindex is requested.
    """
    client = _get_qdrant_client()
    _ensure_uploaded_file_collection()

    deleted = 0
    next_page = None

    while True:
        points, next_page = client.scroll(
            collection_name=CFG.UPLOAD_VECTOR_COLLECTION,
            scroll_filter=_uploaded_file_filter(file_id, user_id),
            limit=256,
            offset=next_page,
            with_payload=False,
            with_vectors=False,
        )

        ids = [p.id for p in points]
        if ids:
            client.delete(collection_name=CFG.UPLOAD_VECTOR_COLLECTION, points_selector=ids)
            deleted += len(ids)

        if not next_page:
            break

    return deleted


def _index_uploaded_file_text(
    *,
    file_id: str,
    user_id: str,
    filename: str,
    text: str,
    force_reindex: bool = False,
) -> Tuple[bool, int, str]:
    """
    Returns: (ok, chunk_count, warning)

    This never raises to caller for normal use. It returns warning if vector indexing fails,
    allowing the existing extracted_text summarization/Q&A to continue.
    """
    if not _vector_enabled():
        return False, 0, f"Vector disabled or qdrant-client missing. {QDRANT_IMPORT_ERROR}"

    try:
        _ensure_uploaded_file_collection()

        existing_count = _count_existing_file_chunks(file_id, user_id)
        if existing_count > 0 and not (force_reindex or CFG.UPLOAD_REINDEX_ON_EACH_ASK):
            return True, existing_count, ""

        if existing_count > 0:
            _delete_existing_file_chunks(file_id, user_id)

        chunks = _chunk_text(
            text,
            chunk_size=CFG.UPLOAD_CHUNK_SIZE,
            overlap=CFG.UPLOAD_CHUNK_OVERLAP,
        )

        if not chunks:
            return False, 0, "No chunks were produced from extracted_text."

        client = _get_qdrant_client()
        points: List[Any] = []
        total = 0

        for idx, chunk in enumerate(chunks, start=1):
            vector = _embed_text(chunk)

            points.append(
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=vector,
                    payload={
                        "source_type": "uploaded_file",
                        "file_id": str(file_id),
                        "user_id": str(user_id or ""),
                        "filename": filename,
                        "chunk_no": idx,
                        "text": chunk,
                    },
                )
            )

            if len(points) >= CFG.UPLOAD_UPSERT_BATCH_SIZE:
                client.upsert(collection_name=CFG.UPLOAD_VECTOR_COLLECTION, points=points)
                total += len(points)
                points = []

        if points:
            client.upsert(collection_name=CFG.UPLOAD_VECTOR_COLLECTION, points=points)
            total += len(points)

        return True, total, ""

    except Exception as e:
        return False, 0, str(e)[:800]


def _search_uploaded_file_chunks(
    *,
    file_id: str,
    user_id: str,
    question: str,
    top_k: int,
) -> Tuple[List[Dict[str, Any]], str]:
    if not _vector_enabled():
        return [], f"Vector disabled or qdrant-client missing. {QDRANT_IMPORT_ERROR}"

    try:
        _ensure_uploaded_file_collection()
        query_vector = _embed_text(question)
        client = _get_qdrant_client()

        results = client.search(
            collection_name=CFG.UPLOAD_VECTOR_COLLECTION,
            query_vector=query_vector,
            query_filter=_uploaded_file_filter(file_id, user_id),
            limit=max(1, int(top_k or 5)),
            with_payload=True,
        )

        rows = []
        for r in results:
            rows.append({"score": float(getattr(r, "score", 0.0) or 0.0), "payload": getattr(r, "payload", {}) or {}})

        return rows, ""

    except Exception as e:
        return [], str(e)[:800]


def _get_uploaded_file_chunks_ordered(
    *,
    file_id: str,
    user_id: str,
    limit: int,
) -> Tuple[List[Dict[str, Any]], str]:
    if not _vector_enabled():
        return [], f"Vector disabled or qdrant-client missing. {QDRANT_IMPORT_ERROR}"

    try:
        _ensure_uploaded_file_collection()
        client = _get_qdrant_client()

        # Qdrant scroll does not guarantee payload ordering, so fetch a safe window,
        # sort by chunk_no, then cut to requested limit.
        scroll_limit = min(max(int(limit or 20), int(limit or 20) * 5), CFG.UPLOAD_SCROLL_MAX)

        points, _ = client.scroll(
            collection_name=CFG.UPLOAD_VECTOR_COLLECTION,
            scroll_filter=_uploaded_file_filter(file_id, user_id),
            limit=scroll_limit,
            with_payload=True,
            with_vectors=False,
        )

        rows = [{"score": 1.0, "payload": p.payload or {}} for p in points]
        rows.sort(key=lambda x: int(x["payload"].get("chunk_no") or 0))
        return rows[: max(1, int(limit or 20))], ""

    except Exception as e:
        return [], str(e)[:800]


def _is_summary_question(question: str) -> bool:
    q = (question or "").lower().strip()
    return any(
        x in q
        for x in [
            "summarize",
            "summary",
            "what is this document about",
            "what is this file about",
            "tell me about this document",
            "tell me about this file",
            "সারাংশ",
            "সামারি",
            "সংক্ষেপে",
            "এই ডকুমেন্ট",
            "এই ফাইল",
            "আপলোডকৃত ডকুমেন্ট",
            "আপলোডকৃত ফাইল",
        ]
    )


def _build_context_from_vector_rows(rows: List[Dict[str, Any]]) -> str:
    parts = []
    for item in rows:
        payload = item.get("payload") or {}
        text = _clean(payload.get("text"))
        if not text:
            continue
        # Do NOT include Qdrant similarity score in the LLM context.
        # Some local models may mistake the score (for example 0.4602728) as the answer.
        parts.append(
            "SOURCE CHUNK:\n"
            f"Filename: {payload.get('filename', '')}\n"
            f"Chunk: {payload.get('chunk_no', '')}\n\n"
            f"{text}"
        )
    return "\n\n---\n\n".join(parts).strip()


# -------------------------
# Excel / CSV analysis helpers
# -------------------------
EXCEL_EXTENSIONS = {".xls", ".xlsx", ".csv"}


def _is_excel_file(filename: str) -> bool:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in EXCEL_EXTENSIONS


def _safe_float(v: Any):
    try:
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def _read_excel_or_csv(file_path: str) -> Dict[str, pd.DataFrame]:
    ext = os.path.splitext(file_path or "")[1].lower()

    if ext == ".csv":
        return {"CSV": pd.read_csv(file_path)}

    if ext == ".xlsx":
        return pd.read_excel(file_path, sheet_name=None, engine="openpyxl")

    if ext == ".xls":
        return pd.read_excel(file_path, sheet_name=None, engine="xlrd")

    raise ValueError(f"Unsupported Excel/CSV file type: {ext}")


def _normalize_excel_key(s: Any) -> str:
    """Normalize a column/question string for tolerant matching."""
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _excel_question_terms(question: str) -> set:
    return {
        t.strip().lower()
        for t in re.split(r"[^A-Za-z0-9]+", str(question or ""))
        if t.strip()
    }


def _find_requested_excel_column(question: str, columns: List[str]) -> Optional[str]:
    """
    Detect whether the user is asking about a specific Excel column.

    Examples:
      - "Tell about Network Team info" -> "Network Team"
      - "Database team details" -> "Database Team"
      - "Network security" -> "Network Security"
    """
    q = str(question or "")
    q_norm = _normalize_excel_key(q)
    q_terms = _excel_question_terms(q)

    if not q_norm or not columns:
        return None

    # 1) Best case: normalized full column name appears in the question.
    for col in columns:
        c_norm = _normalize_excel_key(col)
        if c_norm and c_norm in q_norm:
            return col

    # 2) Score columns by matching column words in the question.
    best_col = None
    best_score = 0

    for col in columns:
        col_words = [
            w.lower()
            for w in re.split(r"[^A-Za-z0-9]+", str(col or ""))
            if w.strip()
        ]
        if not col_words:
            continue

        score = sum(1 for w in col_words if w in q_terms)

        # Give priority to team/security/responsibility style columns.
        col_lower = str(col).lower()
        if score > 0 and any(x in col_lower for x in ["team", "security", "owner", "responsible", "department"]):
            score += 1

        if score > best_score:
            best_score = score
            best_col = col

    # Require at least one meaningful matched word.
    if best_score >= 1:
        return best_col

    return None


def _string_is_meaningful_cell(value: Any) -> bool:
    v = str(value or "").strip()
    return bool(v and v.lower() not in {"nan", "none", "null", "nat"})


def _answer_excel_column_question(uf: UploadedFile, question: str) -> Optional[JsonResponse]:
    """
    Focused Excel Q&A for questions about a specific column/team.

    Example:
      User: "Tell about Network Team info"
      Answer: rows containing Issue, Sub-task, and Network Team values.

    If no specific column is detected, return None so the existing generic
    Excel management-analysis path remains unchanged.
    """
    filename = _get_uploaded_filename(uf)
    file_path = _get_uploaded_file_path(uf)

    if not file_path or not os.path.exists(file_path):
        return None

    try:
        sheets = _read_excel_or_csv(file_path)
    except Exception:
        return None

    question_clean = _clean(question)

    for sheet_name, df in sheets.items():
        try:
            df = df.copy()
            df = df.dropna(how="all")
            df = df.dropna(axis=1, how="all")
            df.columns = [str(c).strip() for c in df.columns]
        except Exception:
            continue

        columns = df.columns.tolist()
        requested_col = _find_requested_excel_column(question_clean, columns)

        if not requested_col:
            continue

        # Include useful context columns when present.
        preferred_context_cols = [
            "Issue",
            "Sub-task",
            "Task",
            "Description",
            "Module",
            "Feature",
            requested_col,
            "Remarks",
            "Comment",
            "Status",
            "Priority",
            "Owner",
        ]

        related_cols = []
        normalized_to_actual = {_normalize_excel_key(c): c for c in columns}

        for candidate in preferred_context_cols:
            key = _normalize_excel_key(candidate)
            actual = normalized_to_actual.get(key)
            if actual and actual not in related_cols:
                related_cols.append(actual)

        if requested_col not in related_cols:
            related_cols.append(requested_col)

        # Keep output focused. If preferred columns are too few, add nearby team columns only when relevant.
        if len(related_cols) == 1:
            for col in columns:
                if col == requested_col:
                    continue
                col_lower = str(col).lower()
                if any(x in col_lower for x in ["issue", "task", "team", "security", "remarks", "status"]):
                    related_cols.append(col)
                if len(related_cols) >= 5:
                    break

        focused_df = df[related_cols].fillna("").astype(str)

        # Prefer rows where the requested column has a meaningful value.
        try:
            non_empty = focused_df[
                focused_df[requested_col].apply(_string_is_meaningful_cell)
            ]
            if not non_empty.empty:
                focused_df = non_empty
        except Exception:
            pass

        output_lines = []
        output_lines.append(f"## {requested_col} Information")
        output_lines.append("")
        output_lines.append(f"- File: `{filename}`")
        output_lines.append(f"- Sheet: `{sheet_name}`")
        output_lines.append(f"- Matched column: `{requested_col}`")
        output_lines.append(f"- Rows with available information: {len(focused_df)}")
        output_lines.append("")

        if focused_df.empty:
            output_lines.append(f"No clear information was found under `{requested_col}`.")
        else:
            output_lines.append("### Related Rows")
            output_lines.append("")

            for idx, row in focused_df.head(30).iterrows():
                output_lines.append(f"**Row {idx + 1}:**")
                for col in related_cols:
                    val = str(row.get(col, "")).strip()
                    if _string_is_meaningful_cell(val):
                        output_lines.append(f"- {col}: {val}")
                output_lines.append("")

            total_rows = len(focused_df)
            if total_rows > 30:
                output_lines.append(f"Only first 30 rows are shown out of {total_rows} matching rows.")
                output_lines.append("")

        output_lines.append("### Summary")
        output_lines.append(
            f"The `{requested_col}` column appears to describe the responsibility, involvement, "
            f"or input of the {str(requested_col).lower()} for the listed issue/sub-task records."
        )

        return JsonResponse(
            {
                "ok": True,
                "answer": "\n".join(output_lines).strip(),
                "file_id": _get_file_token(uf),
                "filename": filename,
                "file_type": "excel",
                "analysis_mode": "excel_column_specific_answer",
                "matched_column": requested_col,
                "sheet_name": str(sheet_name),
                "row_count": int(len(focused_df)),
            }
        )

    return None


def _detect_possible_id_columns(columns: List[str]) -> List[str]:
    keys = [
        "id", "code", "no", "number", "branch", "member", "customer",
        "client", "loan", "account", "loanee", "employee", "emp"
    ]

    out = []
    for col in columns:
        c = str(col).lower()
        if any(k in c for k in keys):
            out.append(col)
    return out


def _detect_possible_amount_columns(columns: List[str]) -> List[str]:
    keys = [
        "amount", "balance", "paid", "due", "loan", "savings", "cash",
        "total", "fee", "installment", "principal", "interest", "profit",
        "loss", "debit", "credit", "collection", "outstanding"
    ]

    out = []
    for col in columns:
        c = str(col).lower()
        if any(k in c for k in keys):
            out.append(col)
    return out


def _detect_possible_date_columns(columns: List[str]) -> List[str]:
    keys = [
        "date", "day", "month", "year", "created", "updated", "due",
        "start", "end", "opening", "closing", "deadline", "target"
    ]

    out = []
    for col in columns:
        c = str(col).lower()
        if any(k in c for k in keys):
            out.append(col)
    return out


def _try_convert_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for col in df.columns:
        try:
            if df[col].dtype == "object":
                cleaned = (
                    df[col]
                    .astype(str)
                    .str.replace(",", "", regex=False)
                    .str.replace("৳", "", regex=False)
                    .str.replace("BDT", "", regex=False)
                    .str.replace("Tk", "", regex=False)
                    .str.replace("TK", "", regex=False)
                    .str.strip()
                )

                converted = pd.to_numeric(cleaned, errors="coerce")

                non_null_original = df[col].notna().sum()
                converted_count = converted.notna().sum()

                if non_null_original > 0 and converted_count >= max(3, int(non_null_original * 0.50)):
                    df[col] = converted

        except Exception:
            continue

    return df


def _analyze_excel_file(file_path: str, max_sample_rows: int = 20) -> Dict[str, Any]:
    sheets = _read_excel_or_csv(file_path)

    workbook_summary: Dict[str, Any] = {
        "file_path": file_path,
        "sheet_count": len(sheets),
        "sheets": {},
    }

    for sheet_name, df in sheets.items():
        df = df.copy()

        df = df.dropna(how="all")
        df = df.dropna(axis=1, how="all")

        df.columns = [str(c).strip() for c in df.columns]
        df = _try_convert_numeric_columns(df)

        columns = df.columns.tolist()
        row_count = int(df.shape[0])
        column_count = int(df.shape[1])

        numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
        text_cols = df.select_dtypes(include=["object"]).columns.tolist()
        date_cols = df.select_dtypes(include=["datetime64[ns]", "datetime64[ns, UTC]"]).columns.tolist()

        missing_values: Dict[str, Any] = {}
        for col in columns:
            missing_count = int(df[col].isna().sum())
            missing_values[col] = {
                "missing_count": missing_count,
                "missing_percent": round((missing_count / row_count * 100), 2) if row_count else 0,
            }

        numeric_summary: Dict[str, Any] = {}
        for col in numeric_cols:
            try:
                numeric_summary[col] = {
                    "count": int(df[col].count()),
                    "sum": _safe_float(df[col].sum()),
                    "average": _safe_float(df[col].mean()),
                    "minimum": _safe_float(df[col].min()),
                    "maximum": _safe_float(df[col].max()),
                    "median": _safe_float(df[col].median()),
                    "zero_count": int((df[col] == 0).sum()),
                    "negative_count": int((df[col] < 0).sum()),
                }
            except Exception:
                numeric_summary[col] = {
                    "error": "Could not summarize this numeric column."
                }

        top_values: Dict[str, Any] = {}
        for col in text_cols[:20]:
            try:
                vc = (
                    df[col]
                    .astype(str)
                    .replace("nan", "")
                    .replace("None", "")
                    .value_counts()
                    .head(10)
                )
                top_values[col] = {
                    str(k): int(v)
                    for k, v in vc.items()
                    if str(k).strip()
                }
            except Exception:
                top_values[col] = {}

        correlations: Dict[str, Any] = {}
        relationship_hints: List[str] = []

        if len(numeric_cols) >= 2:
            try:
                corr = df[numeric_cols].corr(numeric_only=True).round(3)
                correlations = corr.fillna("").to_dict()

                for c1 in numeric_cols:
                    for c2 in numeric_cols:
                        if c1 == c2:
                            continue

                        try:
                            value = corr.loc[c1, c2]
                            if pd.notna(value) and abs(float(value)) >= 0.70:
                                direction = "positive" if float(value) > 0 else "negative"
                                relationship_hints.append(
                                    f"'{c1}' and '{c2}' have strong {direction} correlation ({value})."
                                )
                        except Exception:
                            pass

            except Exception:
                correlations = {}

        data_quality_flags: List[str] = []

        for col, info in missing_values.items():
            if info["missing_percent"] >= 30:
                data_quality_flags.append(
                    f"Column '{col}' has high missing values: {info['missing_percent']}%."
                )
            elif info["missing_percent"] >= 10:
                data_quality_flags.append(
                    f"Column '{col}' has moderate missing values: {info['missing_percent']}%."
                )

        try:
            duplicate_rows = int(df.duplicated().sum())
        except Exception:
            duplicate_rows = 0

        if duplicate_rows > 0:
            data_quality_flags.append(f"Found {duplicate_rows} duplicate rows.")

        possible_relationships = {
            "possible_id_columns": _detect_possible_id_columns(columns),
            "possible_amount_columns": _detect_possible_amount_columns(columns),
            "possible_date_columns": _detect_possible_date_columns(columns),
            "strong_numeric_relationships": relationship_hints[:30],
            "numeric_correlation_available": len(numeric_cols) >= 2,
        }

        sample_rows = []
        try:
            sample_rows = (
                df.head(max_sample_rows)
                .fillna("")
                .astype(str)
                .to_dict(orient="records")
            )
        except Exception:
            sample_rows = []

        workbook_summary["sheets"][str(sheet_name)] = {
            "row_count": row_count,
            "column_count": column_count,
            "columns": columns,
            "numeric_columns": numeric_cols,
            "text_columns": text_cols,
            "date_columns": date_cols,
            "missing_values": missing_values,
            "numeric_summary": numeric_summary,
            "top_values": top_values,
            "correlations": correlations,
            "possible_relationships": possible_relationships,
            "data_quality_flags": data_quality_flags,
            "sample_rows": sample_rows,
        }

    return workbook_summary


def _build_excel_context_for_llm(file_path: str) -> str:
    analysis = _analyze_excel_file(file_path, max_sample_rows=CFG.EXCEL_SAMPLE_ROWS)

    payload = {
        "role": "structured_excel_profile",
        "instruction": (
            "This is a structured profile of an uploaded Excel/CSV file. "
            "Use it to answer management decision questions. "
            "Explain column meaning, numeric summaries, missing values, relationships, "
            "data quality risks, and decision recommendations. "
            "Do not invent numbers."
        ),
        "analysis": analysis,
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return text[: CFG.EXCEL_MAX_PROFILE_CHARS]


def _excel_fallback_summary(excel_context_json: str) -> str:
    """
    Deterministic fallback summary.
    Used when Ollama fails or returns instruction-like/bad output.
    """
    try:
        obj = json.loads(excel_context_json)
        analysis = obj.get("analysis", {})
        sheets = analysis.get("sheets", {})

        if not sheets:
            return "Excel/CSV file analyzed, but no sheet data was found."

        lines = []
        lines.append("## 1. File Overview")

        for sheet_name, sheet in sheets.items():
            lines.append(
                f"- Sheet `{sheet_name}` has {sheet.get('row_count', 0)} rows and "
                f"{sheet.get('column_count', 0)} columns."
            )
            lines.append(
                "- Each row appears to represent a structured record based on the available columns and sample rows."
            )
            break

        lines.append("")
        lines.append("## 2. Important Columns")

        for sheet_name, sheet in sheets.items():
            columns = sheet.get("columns", [])
            if columns:
                lines.append("- Available columns:")
                for col in columns[:30]:
                    lines.append(f"  - {col}")
            else:
                lines.append("- No column names were detected.")
            break

        lines.append("")
        lines.append("## 3. Numeric Summary")

        for sheet_name, sheet in sheets.items():
            numeric_summary = sheet.get("numeric_summary", {})
            numeric_cols = sheet.get("numeric_columns", [])

            if not numeric_cols:
                lines.append("- No numeric columns were detected.")
            else:
                for col, stat in list(numeric_summary.items())[:15]:
                    if "error" in stat:
                        lines.append(f"- {col}: {stat.get('error')}")
                    else:
                        lines.append(
                            f"- {col}: count={stat.get('count')}, "
                            f"sum={stat.get('sum')}, average={stat.get('average')}, "
                            f"min={stat.get('minimum')}, max={stat.get('maximum')}, "
                            f"median={stat.get('median')}"
                        )
            break

        lines.append("")
        lines.append("## 4. Missing Value and Data Quality")

        for sheet_name, sheet in sheets.items():
            dq = sheet.get("data_quality_flags", [])
            missing_values = sheet.get("missing_values", {})

            if dq:
                for item in dq[:20]:
                    lines.append(f"- {item}")
            else:
                lines.append("- No major missing-value or duplicate-row warning was detected.")

            high_missing = []
            for col, info in missing_values.items():
                try:
                    if float(info.get("missing_percent", 0)) > 0:
                        high_missing.append(
                            f"{col}: {info.get('missing_count')} missing "
                            f"({info.get('missing_percent')}%)"
                        )
                except Exception:
                    pass

            if high_missing:
                lines.append("- Missing value details:")
                for item in high_missing[:20]:
                    lines.append(f"  - {item}")
            break

        lines.append("")
        lines.append("## 5. Possible Relationships Between Columns")

        for sheet_name, sheet in sheets.items():
            rel = sheet.get("possible_relationships", {})
            strong = rel.get("strong_numeric_relationships", [])
            id_cols = rel.get("possible_id_columns", [])
            amount_cols = rel.get("possible_amount_columns", [])
            date_cols = rel.get("possible_date_columns", [])

            if id_cols:
                lines.append(f"- Possible identifier/grouping columns: {', '.join(id_cols[:15])}.")
            if amount_cols:
                lines.append(f"- Possible amount/metric columns: {', '.join(amount_cols[:15])}.")
            if date_cols:
                lines.append(f"- Possible date/deadline columns: {', '.join(date_cols[:15])}.")

            if strong:
                lines.append("- Strong numeric relationships:")
                for item in strong[:10]:
                    lines.append(f"  - {item}")
            else:
                lines.append("- Numeric correlation is not applicable or no strong numeric relationship was detected.")
            break

        lines.append("")
        lines.append("## 6. Management Insights")

        for sheet_name, sheet in sheets.items():
            columns = [str(c).lower() for c in sheet.get("columns", [])]

            if any("team" in c for c in columns):
                lines.append("- The file can be used to understand team-wise responsibility distribution.")
            if any("issue" in c for c in columns):
                lines.append("- The file can help management track issues and related work areas.")
            if any("task" in c or "sub" in c for c in columns):
                lines.append("- Sub-task level tracking is available, which helps break large issues into actionable work.")
            if not sheet.get("date_columns"):
                lines.append("- No date columns were detected, so timeline, SLA, delay, and deadline analysis are limited.")
            if not sheet.get("numeric_columns"):
                lines.append("- No numeric columns were detected, so financial, volume, or quantitative trend analysis is not available.")
            break

        lines.append("")
        lines.append("## 7. Recommendations")

        lines.append("- Add a clear owner/accountable person or team for each row.")
        lines.append("- Add status values such as Pending, In Progress, Completed, Blocked, and Escalated.")
        lines.append("- Add priority values such as High, Medium, and Low.")
        lines.append("- Add target date, completion date, and SLA fields to measure delay and performance.")
        lines.append("- Add remarks or dependency fields to explain blockers between teams.")
        lines.append("- For RACI-style tracking, separate Responsible, Accountable, Consulted, and Informed columns.")

        lines.append("")
        lines.append("## 8. Risks / Warnings")

        lines.append("- Without deadline/date fields, management cannot measure delay or SLA breach.")
        lines.append("- Without status fields, it is difficult to know which tasks are completed or pending.")
        lines.append("- Without priority fields, critical issues may not receive proper attention.")
        lines.append("- Without a single accountable owner, responsibility may be unclear across teams.")

        return "\n".join(lines).strip()

    except Exception:
        return "Excel/CSV file analyzed, but deterministic summary could not be generated."


# -------------------------
# Excel answer validation
# -------------------------
BAD_EXCEL_ANSWER_PHRASES = [
    "please provide your insights",
    "please provide",
    "sample rows for better understanding",
    "you can use the excel sheet structure",
    "do not invent any numbers",
    "provide your insights, recommendations",
    "based on the structure and content",
    "there are no dates or numeric values in this file",
    "note: you can use",
    "please do not invent",
]


def _is_bad_excel_answer(answer: str) -> bool:
    text = (answer or "").strip().lower()

    if not text:
        return True

    if len(text) < 120:
        return True

    return any(p in text for p in BAD_EXCEL_ANSWER_PHRASES)


# -------------------------
# STRICT output enforcement for normal documents
# -------------------------
TASKY_PHRASES = [
    "summarize the", "extract the", "explain the", "users can", "after uploading",
    "test prompts", "designed to", "the system", "this feature", "this service",
]

FORBIDDEN_EXTERNAL_TERMS = [
    "chatbot", "chatbots", "translation", "speech recognition",
    "algorithms", "data analysis", "machine learning", "deep learning",
]


def _strip_markdown_headings(ans: str) -> str:
    t = (ans or "").strip()
    t = re.sub(r"^\s*\*\*.*?\*\*\s*:?\n*", "", t, flags=re.M)
    return t.strip()


def _first_block_only(ans: str) -> str:
    t = (ans or "").strip()
    i = t.find("BULLETS:")
    j = t.find("PARAGRAPH:")

    if i == -1 or j == -1 or j < i:
        return t.strip()

    after_para = t[j:]
    k = after_para.find("\nBULLETS:")

    if k != -1:
        return (t[: j + k]).strip()

    return t.strip()


def _contains_any(s: str, terms: list) -> bool:
    sl = (s or "").lower()
    return any(x in sl for x in terms)


def _enforce_format_or_empty(ans: str) -> str:
    t = _first_block_only(_strip_markdown_headings(ans))

    if "BULLETS:" not in t or "PARAGRAPH:" not in t:
        return ""

    parts = t.split("PARAGRAPH:", 1)
    bullets_part = parts[0]
    para_part = parts[1].strip()

    blines = []
    for line in bullets_part.splitlines():
        line = line.strip()
        if line.startswith("- "):
            blines.append(line[2:].strip())

    if len(blines) != 3:
        return ""

    if not para_part or "BULLETS:" in para_part:
        return ""

    combined = "\n".join(blines) + "\n" + para_part

    if _contains_any(combined, TASKY_PHRASES):
        return ""

    if _contains_any(combined, FORBIDDEN_EXTERNAL_TERMS):
        return ""

    out = "BULLETS:\n" + "\n".join([f"- {b}" for b in blines]) + "\n\nPARAGRAPH:\n" + para_part
    return out.strip()


def _strict_postcheck(answer: str) -> str:
    t = (answer or "").strip()
    return t if t else "Not found in the document."


def _clean_uploaded_file_answer(answer: str, question: str = "") -> str:
    """
    Cleans uploaded-file vector Q&A output.

    This prevents common local-model issues:
    - visible EOS tokens
    - repeated user question
    - raw retrieved context printed after the answer
    - separator/context leakage
    """
    t = _clean(answer)
    if not t:
        return ""

    eos_markers = [
        "<EOS_TOKEN>",
        "</s>",
        "<|endoftext|>",
        "<|eot_id|>",
    ]
    for marker in eos_markers:
        if marker in t:
            t = t.split(marker, 1)[0].strip()

    # Remove context leakage if the model starts copying prompt/context after the answer.
    leak_markers = [
        "\n---\n",
        "\n---",
        "\nDOCUMENT CONTEXT:",
        "\nDocument context:",
        "\nDOCUMENT CHUNKS:",
        "\nDocument chunks:",
        "\nRELEVANT DOCUMENT CHUNKS:",
        "\nRelevant document chunks:",
        "\nSOURCE CHUNK:",
        "\nSource chunk:",
        "\nCONTEXT:",
        "\nContext:",
    ]
    for marker in leak_markers:
        if marker in t:
            t = t.split(marker, 1)[0].strip()

    # Remove repeated question at the beginning.
    q = _clean(question)
    if q and t.lower().startswith(q.lower()):
        t = t[len(q):].strip()
        t = t.lstrip(":").strip()

    # Some local models prepend labels; remove only obvious answer labels.
    t = re.sub(r"^\s*(final answer|answer)\s*:\s*", "", t, flags=re.I).strip()

    # Avoid accidental duplicate table/context tail from the same response.
    t = re.sub(r"\n{3,}", "\n\n", t).strip()

    # If the model returned only a Qdrant score or a bare number, reject it.
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", t):
        return ""

    # Reject obviously incomplete / non-answer output.
    if len(t) < 20 and not any(ch.isalpha() for ch in t):
        return ""

    return t


def _is_bad_uploaded_file_answer(answer: str) -> bool:
    t = _clean(answer)
    if not t:
        return True
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", t):
        return True
    if "<EOS_TOKEN>" in t or "SOURCE CHUNK:" in t or "Document context:" in t:
        return True
    # A useful Q&A answer should normally contain letters and be more than a few characters.
    if len(t) < 20 and not any(ch.isalpha() for ch in t):
        return True
    return False


def _sentence_split_for_qa(text: str) -> List[str]:
    cleaned = re.sub(r"\s+", " ", text or " ").strip()
    if not cleaned:
        return []
    # Keep table-ish rows as searchable units, but split normal prose by sentence end.
    pieces = re.split(r"(?<=[.!?])\s+|\s*\|\s*", cleaned)
    return [p.strip() for p in pieces if p and p.strip()]


def _fallback_uploaded_qa(doc_context: str, question: str) -> str:
    """Deterministic fallback for uploaded-file vector Q&A.

    Used when the local LLM returns a bare score, EOS leakage, or empty output.
    It extracts the most relevant sentence/line from retrieved chunks without external knowledge.
    """
    q = (question or "").lower()
    ctx = doc_context or ""

    # Specific high-value policy terms.
    if "risk appetite" in q:
        # Prefer the direct prose definition over table rows.
        m = re.search(
            r"Buro Bangladesh IT 2025 has a moderate risk appetite\.?.{0,450}?core business operations\.?",
            ctx,
            flags=re.I | re.S,
        )
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()

        m = re.search(r"[^.\n]*moderate risk appetite[^.]*\.", ctx, flags=re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()

    # Generic keyword overlap extractive fallback.
    stopwords = {
        "what", "does", "the", "this", "that", "about", "from", "document", "file",
        "policy", "of", "is", "are", "a", "an", "to", "in", "for", "and", "or",
        "কী", "কি", "এই", "ডকুমেন্ট", "ফাইল", "সম্পর্কে", "বলুন"
    }
    q_terms = [w for w in re.findall(r"[A-Za-z0-9]+", q) if len(w) > 2 and w not in stopwords]

    candidates = _sentence_split_for_qa(ctx)
    scored = []
    for sent in candidates:
        sl = sent.lower()
        score = sum(1 for term in q_terms if term in sl)
        if score > 0:
            scored.append((score, len(sent), sent))

    if scored:
        scored.sort(key=lambda x: (-x[0], x[1]))
        best = scored[0][2]
        return re.sub(r"\s+", " ", best).strip()

    return "This information is not clearly found in the uploaded document."


def _build_uploaded_file_summary_prompt(question: str, doc_context: str, filename: str) -> Tuple[str, str]:
    system_prompt = (
        "You are a STRICT uploaded-document summarizer.\n"
        "Use ONLY the provided uploaded document chunks.\n"
        "Do not use outside knowledge.\n"
        "Do not print the full context.\n"
        "Do not repeat the user's question.\n"
        "Do not output EOS tokens or raw separators.\n"
        "Do not invent names, dates, figures, policy clauses, or conclusions.\n"
        "Write a clear summary in this exact format only:\n"
        "BULLETS:\n"
        "- <important fact 1 from document>\n"
        "- <important fact 2 from document>\n"
        "- <important fact 3 from document>\n"
        "PARAGRAPH:\n"
        "<2-4 sentence summary from the uploaded document>\n"
    )
    user_prompt = (
        f"Uploaded file: {filename}\n\n"
        f"User question:\n{question}\n\n"
        "Document chunks:\n"
        "--------------------\n"
        f"{doc_context}\n"
        "--------------------\n\n"
        "Final answer only:"
    )
    return system_prompt, user_prompt


def _build_uploaded_file_qa_prompt(question: str, doc_context: str, filename: str) -> Tuple[str, str]:
    system_prompt = (
        "You are an uploaded-document Q&A assistant.\n"
        "You must answer using ONLY the provided document context.\n"
        "Do not use outside knowledge.\n"
        "Do not print the full context.\n"
        "Do not repeat the user's question.\n"
        "Do not output EOS tokens or raw separators.\n"
        "Do not include raw table rows unless needed for the answer.\n"
        "Do not answer with a retrieval score or a bare number. Your answer must be complete sentence(s).\n"
        "If the question asks about risk appetite, do not confuse it with risk tolerance.\n"
        "If the question asks about risk tolerance, do not confuse it with risk appetite.\n"
        "If the answer is not clearly found in the context, say exactly:\n"
        "This information is not clearly found in the uploaded document.\n"
        "Answer clearly and briefly.\n"
    )
    user_prompt = (
        f"Uploaded file: {filename}\n\n"
        f"User question:\n{question}\n\n"
        "Document context:\n"
        "--------------------\n"
        f"{doc_context}\n"
        "--------------------\n\n"
        "Final answer only:"
    )
    return system_prompt, user_prompt


# -------------------------
# Deterministic fallback for normal documents
# -------------------------
def _is_tasky_line(s: str) -> bool:
    sl = (s or "").strip().lower()
    return any(p in sl for p in TASKY_PHRASES)


def _split_sentences(text: str) -> List[str]:
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t:
        return []
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", t) if x.strip()]


def _fallback_extractive(doc_text: str) -> str:
    raw_lines = [ln.strip() for ln in (doc_text or "").splitlines() if ln.strip()]

    bullets = []
    for ln in raw_lines:
        ln2 = ln

        if ln2.startswith("•"):
            ln2 = ln2.lstrip("•").strip()
        elif ln2.startswith("-"):
            ln2 = ln2.lstrip("-").strip()
        else:
            if ln2.startswith("• "):
                ln2 = ln2[2:].strip()

        if ln.startswith("•") or ln.startswith("-") or "(cid:" in ln:
            if ln2 and not _is_tasky_line(ln2):
                bullets.append(ln2)

        if len(bullets) == 3:
            break

    sents = _split_sentences(doc_text)
    para_sents = []

    for s in sents:
        if not _is_tasky_line(s):
            para_sents.append(s)

        if len(para_sents) == 3:
            break

    if len(bullets) != 3 or len(para_sents) < 2:
        return "Not found in the document."

    paragraph = " ".join(para_sents[:3])
    return "BULLETS:\n- " + "\n- ".join(bullets) + "\n\nPARAGRAPH:\n" + paragraph


# -------------------------
# Ollama call
# -------------------------
def _ollama_chat(system: str, user: str, timeout: float, num_predict: int = 700) -> str:
    url = f"{CFG.OLLAMA_URL}/api/chat"

    payload = {
        "model": CFG.GEN_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {
            "temperature": 0.0,
            "top_p": 0.9,
            "repeat_penalty": 1.15,
            "num_ctx": 8192,
            "num_predict": num_predict,
            # Prevent visible model stop markers and context spillover.
            "stop": [
                "<EOS_TOKEN>",
                "</s>",
                "<|endoftext|>",
                "<|eot_id|>",
            ],
        },
    }

    r = requests.post(url, json=payload, timeout=timeout, headers={"Accept": "application/json"})

    if r.status_code >= 400:
        raise RuntimeError(f"Ollama request failed: {r.status_code} {r.text[:500]}")

    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"Ollama returned non-JSON: {r.text[:500]}")

    return _clean(((data.get("message") or {}).get("content") or ""))


# -------------------------
# Excel answer path
# -------------------------
def _answer_excel_question(uf: UploadedFile, question: str) -> JsonResponse:
    filename = _get_uploaded_filename(uf)
    file_path = _get_uploaded_file_path(uf)

    # Specific Excel questions such as "Tell about Network Team info"
    # should return focused column/row details instead of the generic workbook profile.
    column_specific_response = _answer_excel_column_question(uf, question)
    if column_specific_response is not None:
        return column_specific_response

    if not file_path or not os.path.exists(file_path):
        return JsonResponse(
            {
                "ok": False,
                "error": "Excel file path not found.",
                "hint": (
                    "Check your UploadedFile model FileField name. "
                    "This code checks: file, uploaded_file, document, attachment, upload, path."
                ),
                "filename": filename,
            },
            status=400,
        )

    try:
        excel_context = _build_excel_context_for_llm(file_path)

        system_prompt = (
            "You are a senior enterprise data analyst.\n"
            "You must produce a final management analysis from the uploaded Excel/CSV profile.\n"
            "Use ONLY the provided structured Excel profile.\n"
            "Do NOT ask the user to provide insights.\n"
            "Do NOT repeat instructions.\n"
            "Do NOT say 'please provide'.\n"
            "Do NOT invent numbers.\n"
            "If numbers are not available, say 'No numeric columns were detected'.\n"
            "If dates are not available, say 'No date columns were detected'.\n"
            "Answer in the same language as the user's question when possible.\n"
            "Be direct, practical, and management-focused.\n"
        )

        user_prompt = (
            "USER QUESTION:\n"
            f"{question}\n\n"
            "STRUCTURED EXCEL PROFILE:\n"
            "-------------------------\n"
            f"{excel_context}\n"
            "-------------------------\n\n"
            "Now produce the final answer. Do not ask the user any question.\n\n"
            "Required output:\n"
            "## 1. File Overview\n"
            "- Mention sheet name, row count, column count, and overall purpose.\n\n"
            "## 2. Important Columns\n"
            "- Explain the key columns found in the sheet.\n\n"
            "## 3. Numeric Summary\n"
            "- If numeric columns exist, show sum, average, minimum, maximum.\n"
            "- If no numeric columns exist, clearly say no numeric columns were detected.\n\n"
            "## 4. Missing Value and Data Quality\n"
            "- Mention missing values, duplicate rows, or quality issues.\n\n"
            "## 5. Relationship Between Columns\n"
            "- Explain possible relationship between issue, sub-task, and responsible teams.\n"
            "- If numeric correlation is unavailable, say correlation is not applicable.\n\n"
            "## 6. Management Insights\n"
            "- Give practical observations from the file structure and sample rows.\n\n"
            "## 7. Recommendations\n"
            "- Give actionable recommendations for management.\n\n"
            "## 8. Risks / Warnings\n"
            "- Mention risks such as unclear ownership, missing deadline, no priority, no status, or no SLA if applicable.\n"
        )

        try:
            answer = _ollama_chat(system_prompt, user_prompt, CFG.GEN_TIMEOUT, num_predict=1400)
        except Exception as e:
            fallback = _excel_fallback_summary(excel_context)

            return JsonResponse(
                {
                    "ok": True,
                    "answer": fallback,
                    "file_id": _get_file_token(uf),
                    "filename": filename,
                    "file_type": "excel",
                    "analysis_mode": "structured_excel_summary",
                    "warning": "Ollama failed; returned deterministic Excel summary instead.",
                    "ollama_error": str(e)[:500],
                }
            )

        if _is_bad_excel_answer(answer):
            answer = _excel_fallback_summary(excel_context)

        return JsonResponse(
            {
                "ok": True,
                "answer": answer,
                "file_id": _get_file_token(uf),
                "filename": filename,
                "file_type": "excel",
                "analysis_mode": "structured_excel_summary",
            }
        )

    except Exception as e:
        return JsonResponse(
            {
                "ok": False,
                "error": str(e),
                "hint": "Excel analysis failed. Check pandas/openpyxl/xlrd installation and UploadedFile file path.",
                "filename": filename,
            },
            status=500,
        )


# -------------------------
# Normal document answer paths
# -------------------------
def _legacy_document_answer(*, doc_text: str, question: str, uf: UploadedFile, filename: str) -> JsonResponse:
    """
    Your original full extracted_text summarization behavior.
    Kept as fallback and compatibility path.
    """
    system_prompt = (
        "You are a STRICT document-only summarizer.\n"
        "RULES (must follow):\n"
        "1) Use ONLY the provided DOCUMENT TEXT.\n"
        "2) EXTRACTIVE ONLY: reuse the document's own words/phrases; do NOT add examples or explanations.\n"
        "3) Do NOT write what the user can do, do NOT describe the system; output only document facts.\n"
        "4) Do NOT add any external terms not present in the document.\n"
        "5) Output EXACTLY ONCE in this format:\n"
        "BULLETS:\n"
        "- <fact 1 from document>\n"
        "- <fact 2 from document>\n"
        "- <fact 3 from document>\n"
        "PARAGRAPH:\n"
        "<2–3 sentences using only document text>\n"
        "6) If you cannot comply, output exactly: Not found in the document.\n"
        "7) Plain text only. No markdown headings.\n"
        "8) IMPORTANT: Do NOT use lines about uploading/testing prompts.\n"
    )

    task = (
        f"QUESTION: {question}\n\n"
        "Create an EXTRACTIVE summary using ONLY phrases/sentences from the document.\n"
        "Bullets must be FACTS stated in the document, not instructions like summarize/extract.\n"
        "Output EXACTLY ONCE.\n"
    )

    user_prompt = (
        "DOCUMENT TEXT:\n"
        "--------------------\n"
        f"{doc_text}\n"
        "--------------------\n\n"
        f"{task}\n"
    )

    answer = ""

    try:
        raw1 = _ollama_chat(system_prompt, user_prompt, CFG.GEN_TIMEOUT, num_predict=260)
        norm1 = _enforce_format_or_empty(raw1)

        if not norm1:
            system_prompt2 = system_prompt + (
                "\nSTRICT MODE:\n"
                "If you output task instructions, system descriptions, or repeat templates, you FAIL.\n"
                "Return the required format EXACTLY ONCE.\n"
            )
            raw2 = _ollama_chat(system_prompt2, user_prompt, CFG.GEN_TIMEOUT, num_predict=260)
            norm2 = _enforce_format_or_empty(raw2)
            answer = norm2
        else:
            answer = norm1

        if not answer:
            answer = _fallback_extractive(doc_text)

        answer = _strict_postcheck(answer)

    except Exception as e:
        fb = _fallback_extractive(doc_text)

        if fb and fb != "Not found in the document.":
            return JsonResponse(
                {
                    "ok": True,
                    "answer": fb,
                    "file_id": _get_file_token(uf),
                    "filename": filename,
                    "text_len": len(doc_text),
                    "warning": "Ollama failed; returned deterministic extractive summary instead.",
                    "ollama_error": str(e)[:500],
                    "answer_mode": "legacy_full_text_fallback",
                }
            )

        return JsonResponse(
            {
                "ok": False,
                "error": str(e),
                "hint": "Check OLLAMA_URL and that your Ollama supports /api/chat and the model exists.",
                "ollama_url": CFG.OLLAMA_URL,
                "gen_model": CFG.GEN_MODEL,
                "filename": filename,
            },
            status=502,
        )

    return JsonResponse(
        {
            "ok": True,
            "answer": answer,
            "file_id": _get_file_token(uf),
            "filename": filename,
            "text_len": len(doc_text),
            "file_type": "document",
            "answer_mode": "legacy_full_text",
        }
    )


def _vector_document_answer(
    *,
    request,
    doc_text: str,
    question: str,
    uf: UploadedFile,
    filename: str,
) -> Optional[JsonResponse]:
    """
    New uploaded-file vector Q&A path.

    Returns JsonResponse if vector path is successful.
    Returns None if vector path cannot be used, so caller can fallback to legacy path.
    """
    if not _vector_enabled():
        return None

    file_token = _get_file_token(uf)
    user_id = _get_user_id_str(request)

    indexed_ok, indexed_chunks, vector_warning = _index_uploaded_file_text(
        file_id=file_token,
        user_id=user_id,
        filename=filename,
        text=doc_text,
        force_reindex=CFG.UPLOAD_REINDEX_ON_EACH_ASK,
    )

    if not indexed_ok:
        # Do not break existing feature. Let caller fallback to legacy full text behavior.
        return None

    summary_request = _is_summary_question(question)

    if summary_request:
        retrieved, search_warning = _get_uploaded_file_chunks_ordered(
            file_id=file_token,
            user_id=user_id,
            limit=CFG.UPLOAD_SUMMARY_CHUNK_LIMIT,
        )
    else:
        retrieved, search_warning = _search_uploaded_file_chunks(
            file_id=file_token,
            user_id=user_id,
            question=question,
            top_k=CFG.UPLOAD_TOP_K,
        )

    if not retrieved:
        # If retrieval fails unexpectedly, fallback to legacy full-text behavior.
        return None

    doc_context = _build_context_from_vector_rows(retrieved)
    if not doc_context:
        return None

    if summary_request:
        system_prompt, user_prompt = _build_uploaded_file_summary_prompt(
            question=question,
            doc_context=doc_context,
            filename=filename,
        )
        num_predict = CFG.UPLOAD_SUMMARY_NUM_PREDICT
    else:
        system_prompt, user_prompt = _build_uploaded_file_qa_prompt(
            question=question,
            doc_context=doc_context,
            filename=filename,
        )
        num_predict = CFG.UPLOAD_QA_NUM_PREDICT

    try:
        answer = _ollama_chat(system_prompt, user_prompt, CFG.GEN_TIMEOUT, num_predict=num_predict)
        answer = _clean_uploaded_file_answer(answer, question)

        # If the model returns only a Qdrant similarity score or any non-answer,
        # produce a deterministic extractive answer from retrieved chunks.
        if (not summary_request) and _is_bad_uploaded_file_answer(answer):
            answer = _fallback_uploaded_qa(doc_context, question)

        if summary_request:
            # Preserve strict summary quality where possible, but do not allow context/EOS leakage.
            normalized = _enforce_format_or_empty(answer)
            if normalized:
                answer = normalized
            elif not answer:
                answer = _fallback_extractive(doc_context)
            else:
                answer = _clean_uploaded_file_answer(answer, question)
        else:
            if not answer:
                answer = "This information is not clearly found in the uploaded document."

        sources = []
        for item in retrieved:
            payload = item.get("payload") or {}
            sources.append(
                {
                    "filename": payload.get("filename"),
                    "chunk_no": payload.get("chunk_no"),
                    "score": item.get("score"),
                }
            )

        response_payload = {
            "ok": True,
            "answer": answer,
            "file_id": file_token,
            "filename": filename,
            "text_len": len(doc_text),
            "file_type": "document",
            "answer_mode": "vector_summary" if summary_request else "vector_qa",
            "vector_collection": CFG.UPLOAD_VECTOR_COLLECTION,
            "indexed_chunks": indexed_chunks,
            "retrieved_chunks": len(retrieved),
            "sources": sources,
        }

        warnings = []
        if vector_warning:
            warnings.append(vector_warning)
        if search_warning:
            warnings.append(search_warning)
        if warnings:
            response_payload["vector_warning"] = " | ".join(warnings)[:1000]

        return JsonResponse(response_payload)

    except Exception:
        # Do not break existing feature. Let caller fallback to legacy full text behavior.
        return None


# -------------------------
# Main endpoint
# -------------------------
@csrf_exempt
@require_POST
def upload_doc_ask(request):
    """
    POST JSON:
      {
        "file_id": "<uuid or pk>",
        "question": "Summarize this document"
      }

    Behavior:
    - Excel/CSV: existing structured deterministic + LLM analysis path.
    - Normal document:
        1) Read UploadedFile.extracted_text as before.
        2) Try vector indexing/search in collection uploaded_file_chunks.
        3) If vector works, answer from current file chunks only.
        4) If vector fails, fallback to original full extracted_text behavior.
    """

    deny = _authz_or_error(request)
    if deny:
        return deny

    body = _json_body(request)
    file_id = _clean(body.get("file_id"))
    question = _clean(body.get("question") or body.get("message") or "Summarize this document")

    if not file_id:
        return JsonResponse({"ok": False, "error": "file_id is required"}, status=400)

    uf = _get_uploaded_file(file_id)
    if not uf:
        return JsonResponse({"ok": False, "error": f"File not found for file_id={file_id}"}, status=404)

    filename = _get_uploaded_filename(uf)

    # -------------------------
    # Excel / CSV structured analysis path
    # -------------------------
    if _is_excel_file(filename):
        return _answer_excel_question(uf, question)

    # -------------------------
    # Normal document text path
    # -------------------------
    doc_text = (getattr(uf, "extracted_text", "") or "").strip()

    if not doc_text:
        return JsonResponse(
            {
                "ok": False,
                "error": "Uploaded file text is empty (extracted_text is blank).",
                "hint": "For PDF/DOC/TXT, check text extraction. For Excel, make sure filename ends with .xls, .xlsx, or .csv.",
                "filename": filename,
            },
            status=400,
        )

    doc_text = _clean_pdf_artifacts(doc_text)[: CFG.DOC_MAX_CHARS]

    if len(doc_text) < 80:
        return JsonResponse(
            {
                "ok": False,
                "error": "Document text too small; PDF extraction may have failed.",
                "text_len": len(doc_text),
                "filename": filename,
            },
            status=400,
        )

    # Try new vector path first. If it fails, keep old behavior.
    vector_response = _vector_document_answer(
        request=request,
        doc_text=doc_text,
        question=question,
        uf=uf,
        filename=filename,
    )
    if vector_response is not None:
        return vector_response

    # Existing behavior fallback.
    return _legacy_document_answer(doc_text=doc_text, question=question, uf=uf, filename=filename)
