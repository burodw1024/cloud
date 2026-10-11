from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .compat import APIRouter, File, Form, Header, HTTPException, UploadFile
from .compat import OpenAI


router = APIRouter(prefix="/api/admin", tags=["Policy RAG Admin"])

OPENAI_VECTOR_STORE_ID = os.environ.get("OPENAI_VECTOR_STORE_ID", "").strip()
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
ADMIN_MAX_UPLOAD_MB = int(os.environ.get("ADMIN_MAX_UPLOAD_MB", "50"))

# Conservative set of formats commonly used for policy/RAG documents.
ALLOWED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".md",
    ".txt",
    ".html",
    ".htm",
    ".rtf",
}

openai_client = OpenAI()


def _require_admin(x_admin_token: str | None) -> None:
    """Require an admin token when ADMIN_TOKEN is configured."""
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")


def _get_store_id() -> str:
    if not OPENAI_VECTOR_STORE_ID:
        raise HTTPException(
            status_code=500,
            detail="OPENAI_VECTOR_STORE_ID is not configured.",
        )
    return OPENAI_VECTOR_STORE_ID


def _safe_token(value: str, fallback: str = "policy") -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._")
    return value[:80] or fallback


def _clean_version(value: str) -> str:
    version = (value or "").strip()
    if not version:
        raise HTTPException(status_code=400, detail="Version is required.")
    if len(version) > 80:
        raise HTTPException(status_code=400, detail="Version is too long.")
    return version


def _clean_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="effective_date must use YYYY-MM-DD format.",
        ) from exc
    return value


def _iter_vector_files(vector_store_id: str) -> list[Any]:
    """Return all files attached to the configured vector store."""
    page = openai_client.vector_stores.files.list(
        vector_store_id,
        limit=100,
        order="desc",
    )
    items = list(getattr(page, "data", []) or [])

    # The OpenAI cursor-page helper supports these methods in current SDKs.
    while getattr(page, "has_next_page", lambda: False)():
        page = page.get_next_page()
        items.extend(list(getattr(page, "data", []) or []))

    return items


def _file_metadata(file_id: str) -> dict[str, Any]:
    try:
        file_obj = openai_client.files.retrieve(file_id)
        return {
            "filename": str(getattr(file_obj, "filename", "") or ""),
            "bytes": int(getattr(file_obj, "bytes", 0) or 0),
            "purpose": str(getattr(file_obj, "purpose", "") or ""),
        }
    except Exception:
        return {"filename": "", "bytes": 0, "purpose": ""}


def _serialize_vector_file(item: Any) -> dict[str, Any]:
    file_id = str(getattr(item, "id", "") or "")
    meta = _file_metadata(file_id)
    attrs = dict(getattr(item, "attributes", None) or {})
    created_at = int(getattr(item, "created_at", 0) or 0)
    created_iso = ""
    if created_at:
        created_iso = datetime.fromtimestamp(
            created_at, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")

    last_error = getattr(item, "last_error", None)
    error_message = ""
    if last_error:
        error_message = str(getattr(last_error, "message", "") or last_error)

    return {
        "file_id": file_id,
        "filename": meta["filename"],
        "bytes": meta["bytes"],
        "status": str(getattr(item, "status", "") or ""),
        "created_at": created_iso,
        "usage_bytes": int(getattr(item, "usage_bytes", 0) or 0),
        "attributes": attrs,
        "last_error": error_message,
    }


def _delete_vector_file(vector_store_id: str, file_id: str, delete_file: bool) -> None:
    openai_client.vector_stores.files.delete(
        file_id,
        vector_store_id=vector_store_id,
    )
    if delete_file:
        try:
            openai_client.files.delete(file_id)
        except Exception:
            # Vector-store detachment is the critical operation. A failure to delete
            # the underlying File should not leave the RAG index in a duplicated state.
            pass


def _find_upsert_matches(
    vector_store_id: str,
    *,
    document_key: str,
    document_type: str,
    version: str,
) -> list[Any]:
    matches: list[Any] = []
    for item in _iter_vector_files(vector_store_id):
        attrs = dict(getattr(item, "attributes", None) or {})
        if (
            str(attrs.get("document_key", "")) == document_key
            and str(attrs.get("document_type", "")) == document_type
            and str(attrs.get("version", "")) == version
        ):
            matches.append(item)
    return matches


@router.get("/config")
def admin_config(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _require_admin(x_admin_token)
    vector_store_id = _get_store_id()

    try:
        store = openai_client.vector_stores.retrieve(vector_store_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unable to retrieve OpenAI vector store: {exc}",
        ) from exc

    return {
        "vector_store_id": vector_store_id,
        "vector_store_name": str(getattr(store, "name", "") or ""),
        "status": str(getattr(store, "status", "") or ""),
        "admin_token_required": bool(ADMIN_TOKEN),
        "max_upload_mb": ADMIN_MAX_UPLOAD_MB,
        "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
    }


@router.get("/files")
def list_policy_files(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _require_admin(x_admin_token)
    vector_store_id = _get_store_id()

    try:
        items = [_serialize_vector_file(item) for item in _iter_vector_files(vector_store_id)]
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unable to list vector-store files: {exc}",
        ) from exc

    return {
        "vector_store_id": vector_store_id,
        "count": len(items),
        "files": items,
    }


@router.post("/upsert")
def upsert_policy_file(
    file: UploadFile = File(...),
    document_key: str = Form(...),
    document_type: str = Form(...),
    version: str = Form(...),
    effective_date: str = Form(""),
    title: str = Form(""),
    replace_existing: bool = Form(True),
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Upload/index a base policy or amendment into the existing vector store.

    Upsert identity = document_key + document_type + version.
    When replace_existing=true, existing matches are detached before the new
    document is attached, preventing duplicate retrieval for the same version.
    """
    _require_admin(x_admin_token)
    vector_store_id = _get_store_id()

    original_filename = Path(file.filename or "policy-document").name
    suffix = Path(original_filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '{suffix or '(none)'}'. "
                f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            ),
        )

    document_type = (document_type or "").strip().lower()
    if document_type not in {"base", "amendment"}:
        raise HTTPException(
            status_code=400,
            detail="document_type must be 'base' or 'amendment'.",
        )

    normalized_key = _safe_token(document_key, "organization-policy")
    version = _clean_version(version)
    effective_date = _clean_date(effective_date)
    title = (title or "").strip()[:200]

    data = file.file.read()
    max_bytes = ADMIN_MAX_UPLOAD_MB * 1024 * 1024
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {ADMIN_MAX_UPLOAD_MB} MB admin upload limit.",
        )

    generated_filename = "__".join(
        part
        for part in [
            normalized_key,
            document_type,
            _safe_token(version, "v1"),
            effective_date or "no-effective-date",
        ]
        if part
    ) + suffix

    attributes: dict[str, str | int | bool | float] = {
        "document_key": normalized_key,
        "document_type": document_type,
        "version": version,
        "effective_date": effective_date or "unspecified",
        "original_filename": original_filename[:512],
        "title": title or original_filename[:512],
        "active": True,
    }

    removed: list[str] = []
    cleanup_warnings: list[str] = []
    uploaded_file_id = ""
    attached_file_id = ""

    # Resolve matches before upload, but intentionally keep them attached until
    # the replacement has finished indexing. This prevents an interrupted
    # upsert from removing the only working copy of a policy version.
    matches = (
        _find_upsert_matches(
            vector_store_id,
            document_key=normalized_key,
            document_type=document_type,
            version=version,
        )
        if replace_existing
        else []
    )

    try:
        # Upload with a deterministic, policy-aware filename so File Search
        # citations themselves carry version/type context.
        uploaded = openai_client.files.create(
            file=(generated_filename, data, file.content_type or "application/octet-stream"),
            purpose="assistants",
        )
        uploaded_file_id = str(uploaded.id)

        attached = openai_client.vector_stores.files.create(
            vector_store_id,
            file_id=uploaded.id,
            attributes=attributes,
        )
        attached_file_id = str(attached.id)
        indexed = openai_client.vector_stores.files.poll(
            attached.id,
            vector_store_id=vector_store_id,
        )

        status = str(getattr(indexed, "status", "") or "")
        if status != "completed":
            last_error = getattr(indexed, "last_error", None)
            message = str(getattr(last_error, "message", "") or last_error or status)
            # Clean up only the failed new copy; the previous version remains intact.
            if attached_file_id:
                try:
                    _delete_vector_file(vector_store_id, attached_file_id, delete_file=True)
                except Exception:
                    pass
            raise HTTPException(
                status_code=502,
                detail=f"Vector-store indexing did not complete successfully: {message}",
            )

        # Replacement is now healthy. Remove prior matching copies.
        for item in matches:
            old_file_id = str(getattr(item, "id", "") or "")
            if not old_file_id or old_file_id == attached_file_id:
                continue
            try:
                _delete_vector_file(vector_store_id, old_file_id, delete_file=True)
                removed.append(old_file_id)
            except Exception as exc:
                cleanup_warnings.append(f"Could not remove old file {old_file_id}: {exc}")

    except HTTPException:
        raise
    except Exception as exc:
        # If upload succeeded but attachment/indexing failed before a completed
        # replacement existed, remove the new partial copy and leave old matches.
        if attached_file_id:
            try:
                _delete_vector_file(vector_store_id, attached_file_id, delete_file=True)
            except Exception:
                pass
        elif uploaded_file_id:
            try:
                openai_client.files.delete(uploaded_file_id)
            except Exception:
                pass
        raise HTTPException(
            status_code=502,
            detail=f"OpenAI upsert failed: {exc}",
        ) from exc

    return {
        "ok": True,
        "action": "replaced" if removed else "inserted",
        "removed_file_ids": removed,
        "warnings": cleanup_warnings,
        "file": _serialize_vector_file(indexed),
    }


@router.delete("/files/{file_id}")
def delete_policy_file(
    file_id: str,
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_admin_token)
    vector_store_id = _get_store_id()

    try:
        _delete_vector_file(vector_store_id, file_id, delete_file=True)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unable to delete vector-store file: {exc}",
        ) from exc

    return {"ok": True, "file_id": file_id}


@router.post("/search-test")
def search_test(
    query: str = Form(...),
    max_results: int = Form(5),
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_admin_token)
    vector_store_id = _get_store_id()
    query = (query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Search query is required.")

    max_results = max(1, min(int(max_results), 10))

    try:
        page = openai_client.vector_stores.search(
            vector_store_id,
            query=query,
            max_num_results=max_results,
            rewrite_query=True,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Vector-store search test failed: {exc}",
        ) from exc

    results: list[dict[str, Any]] = []
    for item in list(getattr(page, "data", []) or []):
        content_parts = []
        for content in getattr(item, "content", []) or []:
            text = str(getattr(content, "text", "") or "").strip()
            if text:
                content_parts.append(text)
        results.append(
            {
                "file_id": str(getattr(item, "file_id", "") or ""),
                "filename": str(getattr(item, "filename", "") or ""),
                "score": float(getattr(item, "score", 0.0) or 0.0),
                "attributes": dict(getattr(item, "attributes", None) or {}),
                "text": "\n".join(content_parts)[:4000],
            }
        )

    return {"query": query, "results": results}
