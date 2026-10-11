# chat/upload.py
from __future__ import annotations

import io
import os
import re
import json
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from django.core.files.storage import default_storage
from django.http import JsonResponse
from django.shortcuts import render
from django.utils.timezone import now


# -------------------------
# Env helpers
# -------------------------
def _env(k: str, default: str) -> str:
    return os.getenv(k, default)


def _env_int(k: str, default: str) -> int:
    try:
        return int(os.getenv(k, default))
    except Exception:
        return int(default)


def _env_float(k: str, default: str) -> float:
    try:
        return float(os.getenv(k, default))
    except Exception:
        return float(default)


def _json_headers() -> Dict[str, str]:
    return {"Content-Type": "application/json"}


# -------------------------
# Auth helper (JSON response, no redirect)
# -------------------------
def require_staff_json(request) -> Optional[JsonResponse]:
    u = getattr(request, "user", None)
    if not u or not u.is_authenticated:
        return JsonResponse({"ok": False, "error": "Not logged in."}, status=401)
    if not u.is_staff:
        return JsonResponse({"ok": False, "error": "Staff only."}, status=403)
    return None


# -------------------------
# Qdrant/Ollama helpers
# -------------------------
def ensure_collection_named_vectors(
    qdrant_url: str,
    collection: str,
    vector_name: str,
    vector_size: int,
    distance: str,
    timeout: Tuple[int, int],
) -> None:
    """
    Creates collection if missing using NAMED vectors map:
      vectors: { "<vector_name>": {size, distance} }
    Your existing collection uses vector_name="default".
    """
    r = requests.get(f"{qdrant_url}/collections/{collection}", timeout=timeout)
    if r.status_code == 200:
        return

    body = {"vectors": {vector_name: {"size": vector_size, "distance": distance}}}
    rr = requests.put(
        f"{qdrant_url}/collections/{collection}",
        headers=_json_headers(),
        json=body,
        timeout=timeout,
    )
    rr.raise_for_status()


def embed_text(
    ollama_url: str,
    embed_model: str,
    text: str,
    vector_size: int,
    timeout: Tuple[int, int],
) -> List[float]:
    r = requests.post(
        f"{ollama_url}/api/embeddings",
        headers=_json_headers(),
        json={"model": embed_model, "prompt": text},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Ollama embedding failed {r.status_code}: {r.text}")

    vec = r.json().get("embedding")
    if not isinstance(vec, list) or len(vec) != vector_size:
        got = 0 if not isinstance(vec, list) else len(vec)
        raise RuntimeError(f"Embedding size mismatch: got {got} expected {vector_size}")
    return vec


def upsert_points_named_vector(
    qdrant_url: str,
    collection: str,
    vector_name: str,
    ids: List[Union[int, str]],
    vectors: List[List[float]],
    payloads: List[Dict[str, Any]],
    timeout: Tuple[int, int],
) -> None:
    """
    Universal Qdrant upsert:
      PUT /collections/<col>/points?wait=true
      body: {"points":[{"id":.., "vector": {"default":[...]}, "payload": {...}}, ...]}
    """
    points = []
    for pid, vec, pay in zip(ids, vectors, payloads):
        points.append(
            {
                "id": pid,
                "vector": {vector_name: vec},  # ✅ named vector
                "payload": pay,
            }
        )

    body = {"points": points}

    r = requests.put(
        f"{qdrant_url}/collections/{collection}/points?wait=true",
        headers=_json_headers(),
        json=body,
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Qdrant upsert failed {r.status_code}: {r.text}")

    resp = r.json()
    if resp.get("status") != "ok":
        raise RuntimeError(f"Unexpected Qdrant response: {resp}")


def ingest_saved_jsonl(
    *,
    abs_path: str,
    collection: str,
    ollama_url: str,
    qdrant_url: str,
    embed_model: str,
    vector_name: str,
    batch_size: int,
    vector_size: int,
    embed_timeout: Tuple[int, int],
    qdrant_timeout: Tuple[int, int],
    sleep_between: float,
) -> Dict[str, Any]:
    inserted = 0
    checked = 0
    skipped_empty = 0
    bad = 0
    bad_examples: List[Dict[str, Any]] = []

    ids: List[Union[int, str]] = []
    vecs: List[List[float]] = []
    pays: List[Dict[str, Any]] = []
    fallback_id = 0

    def flush():
        nonlocal inserted, ids, vecs, pays
        if not ids:
            return
        upsert_points_named_vector(
            qdrant_url=qdrant_url,
            collection=collection,
            vector_name=vector_name,
            ids=ids,
            vectors=vecs,
            payloads=pays,
            timeout=qdrant_timeout,
        )
        inserted += len(ids)
        ids, vecs, pays = [], [], []
        if sleep_between > 0:
            time.sleep(sleep_between)

    with open(abs_path, "rb") as bf:
        stream = io.TextIOWrapper(bf, encoding="utf-8-sig", errors="replace", newline="")
        for line_no, line in enumerate(stream, start=1):
            line = line.strip()
            if not line:
                skipped_empty += 1
                continue

            checked += 1

            # Parse JSON object per line
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("Not a JSON object")
            except Exception as e:
                bad += 1
                if len(bad_examples) < 10:
                    bad_examples.append({"line": line_no, "error": f"JSON parse error: {e}"})
                continue

            text = (obj.get("text") or "").strip()
            if not text:
                bad += 1
                if len(bad_examples) < 10:
                    bad_examples.append({"line": line_no, "error": "Missing/empty field: text"})
                continue

            # ID strategy: prefer chunk_index; else fallback incremental
            pid = obj.get("chunk_index", None)
            if pid is None:
                fallback_id += 1
                pid = fallback_id
            if isinstance(pid, str) and pid.isdigit():
                pid = int(pid)

            # Embed
            try:
                v = embed_text(ollama_url, embed_model, text, vector_size, embed_timeout)
            except Exception as e:
                bad += 1
                if len(bad_examples) < 10:
                    bad_examples.append({"line": line_no, "error": f"Embedding failed: {e}"})
                continue

            payload = {
                "title": obj.get("title"),
                "source_file": obj.get("source_file"),
                "section": obj.get("section"),
                "chunk_index": obj.get("chunk_index"),
                "language": obj.get("language", "bn"),
                "text": obj.get("text"),
                "metadata": obj.get("metadata", {}),
            }

            ids.append(pid)
            vecs.append(v)
            pays.append(payload)

            if len(ids) >= batch_size:
                flush()

        flush()

    return {
        "checked_lines": checked,
        "skipped_empty_lines": skipped_empty,
        "inserted": inserted,
        "bad_lines": bad,
        "bad_examples": bad_examples,
    }


# -------------------------
# Main handler (GET UI / POST save+ingest)
# -------------------------
def handle_upload_request(request):
    """
    GET  -> render upload UI
    POST -> save JSONL + ingest into Qdrant collection (collection REQUIRED from UI)
    """
    if request.method == "GET":
        return render(request, "chat/upload.html")

    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Use POST."}, status=405)

    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "Missing file field 'file'."}, status=400)

    # Collection MUST come from UI
    collection = (request.POST.get("collection") or "").strip()
    if not collection:
        return JsonResponse({"ok": False, "error": "Missing required field: collection"}, status=400)

    # Validate collection name
    if not re.fullmatch(r"[a-zA-Z0-9_-]{3,64}", collection):
        return JsonResponse(
            {"ok": False, "error": "Invalid collection name. Use a–z, 0–9, _ or - (3–64 chars)."},
            status=400,
        )

    # Embedding model can be from UI, else env default
    embed_model = (request.POST.get("embed_model") or "").strip() or _env("EMBED_MODEL", "nomic-embed-text:latest")

    # Infra from env (your real endpoints)
    ollama_url = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    qdrant_url = _env("QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")

    # IMPORTANT: your collection uses named vector "default"
    vector_name = _env("QDRANT_VECTOR_NAME", "default").strip() or "default"

    # Tunables
    vector_size = 768
    distance = "Cosine"
    batch_size = _env_int("INGEST_BATCH_SIZE", "8")
    sleep_between = _env_float("INGEST_SLEEP", "0.10")

    embed_timeout_s = int(_env_float("EMBED_TIMEOUT", "300"))
    qdrant_timeout_s = int(_env_float("QDRANT_TIMEOUT", "300"))
    timeout_embed = (5, max(10, embed_timeout_s))
    timeout_qdrant = (5, max(10, qdrant_timeout_s))

    # Save file
    ts = now().strftime("%Y%m%d_%H%M%S")
    safe_name = os.path.basename(getattr(f, "name", "upload.jsonl")).replace(" ", "_")
    rel_path = f"uploads/jsonl/{ts}_{safe_name}"
    saved_path = default_storage.save(rel_path, f)

    try:
        abs_path = default_storage.path(saved_path)
    except Exception:
        return JsonResponse(
            {"ok": False, "error": "Storage backend does not support path(); use local filesystem storage."},
            status=500,
        )

    # Prechecks + ensure collection
    try:
        requests.get(f"{ollama_url}/api/tags", timeout=timeout_embed).raise_for_status()
        requests.get(f"{qdrant_url}/healthz", timeout=timeout_qdrant).raise_for_status()

        # ensure named-vectors collection
        ensure_collection_named_vectors(
            qdrant_url=qdrant_url,
            collection=collection,
            vector_name=vector_name,
            vector_size=vector_size,
            distance=distance,
            timeout=timeout_qdrant,
        )
    except Exception as e:
        return JsonResponse(
            {
                "ok": False,
                "saved_relative_path": saved_path,
                "saved_abs_path": abs_path,
                "error": f"Precheck/collection failed: {e}",
            },
            status=502,
        )

    # Ingest
    try:
        stats = ingest_saved_jsonl(
            abs_path=abs_path,
            collection=collection,
            ollama_url=ollama_url,
            qdrant_url=qdrant_url,
            embed_model=embed_model,
            vector_name=vector_name,
            batch_size=batch_size,
            vector_size=vector_size,
            embed_timeout=timeout_embed,
            qdrant_timeout=timeout_qdrant,
            sleep_between=sleep_between,
        )
    except Exception as e:
        return JsonResponse(
            {
                "ok": False,
                "saved_relative_path": saved_path,
                "saved_abs_path": abs_path,
                "collection": collection,
                "error": f"Ingestion failed: {e}",
            },
            status=500,
        )

    return JsonResponse(
        {
            "ok": True,
            "saved_relative_path": saved_path,
            "saved_abs_path": abs_path,
            "collection": collection,
            "embed_model": embed_model,
            "ollama_url": ollama_url,
            "qdrant_url": qdrant_url,
            "vector_name": vector_name,
            "batch_size": batch_size,
            **stats,
        }
    )
