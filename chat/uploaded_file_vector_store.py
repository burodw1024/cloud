# chat/uploaded_file_vector_store.py

from __future__ import annotations

import os
import uuid
from typing import List, Dict, Any, Optional

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
)


def _env(k: str, default: str) -> str:
    return (os.getenv(k, default) or "").strip()


QDRANT_HOST = _env("QDRANT_HOST", "192.168.1.144")
QDRANT_PORT = int(_env("QDRANT_PORT", "6333"))
COLLECTION = _env("UPLOAD_VECTOR_COLLECTION", "uploaded_file_chunks")

OLLAMA_URL = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
EMBED_MODEL = _env("UPLOAD_EMBED_MODEL", "nomic-embed-text")
VECTOR_SIZE = int(_env("UPLOAD_VECTOR_SIZE", "768"))

client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)


def ensure_uploaded_file_collection() -> None:
    collections = client.get_collections().collections
    existing = {c.name for c in collections}

    if COLLECTION not in existing:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE,
            ),
        )


def embed_text(text: str) -> List[float]:
    response = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={
            "model": EMBED_MODEL,
            "prompt": text,
        },
        timeout=120,
    )
    response.raise_for_status()

    data = response.json()
    embedding = data.get("embedding")

    if not embedding:
        raise RuntimeError("No embedding returned from Ollama.")

    return embedding


def chunk_text(text: str, chunk_size: int = 3000, overlap: int = 300) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []

    chunks = []
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


def delete_existing_file_chunks(file_id: str, user_id: Optional[str] = None) -> None:
    must = [
        FieldCondition(
            key="source_type",
            match=MatchValue(value="uploaded_file"),
        ),
        FieldCondition(
            key="file_id",
            match=MatchValue(value=str(file_id)),
        ),
    ]

    if user_id:
        must.append(
            FieldCondition(
                key="user_id",
                match=MatchValue(value=str(user_id)),
            )
        )

    client.delete(
        collection_name=COLLECTION,
        points_selector=Filter(must=must),
    )


def index_uploaded_file_text(
    *,
    file_id: str,
    user_id: Optional[str],
    filename: str,
    text: str,
) -> int:
    ensure_uploaded_file_collection()

    chunks = chunk_text(
        text,
        chunk_size=int(_env("UPLOAD_CHUNK_SIZE", "3000")),
        overlap=int(_env("UPLOAD_CHUNK_OVERLAP", "300")),
    )

    if not chunks:
        return 0

    # Prevent duplicate chunks if the same file is indexed again.
    delete_existing_file_chunks(file_id=file_id, user_id=user_id)

    points = []

    for idx, chunk in enumerate(chunks, start=1):
        vector = embed_text(chunk)

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

    client.upsert(
        collection_name=COLLECTION,
        points=points,
    )

    return len(points)


def search_uploaded_file_chunks(
    *,
    file_id: str,
    user_id: Optional[str],
    question: str,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    ensure_uploaded_file_collection()

    query_vector = embed_text(question)

    must = [
        FieldCondition(
            key="source_type",
            match=MatchValue(value="uploaded_file"),
        ),
        FieldCondition(
            key="file_id",
            match=MatchValue(value=str(file_id)),
        ),
    ]

    if user_id:
        must.append(
            FieldCondition(
                key="user_id",
                match=MatchValue(value=str(user_id)),
            )
        )

    results = client.search(
        collection_name=COLLECTION,
        query_vector=query_vector,
        query_filter=Filter(must=must),
        limit=top_k,
        with_payload=True,
    )

    return [
        {
            "score": float(r.score),
            "payload": r.payload or {},
        }
        for r in results
    ]


def get_uploaded_file_chunks_ordered(
    *,
    file_id: str,
    user_id: Optional[str],
    limit: int = 20,
) -> List[Dict[str, Any]]:
    ensure_uploaded_file_collection()

    must = [
        FieldCondition(
            key="source_type",
            match=MatchValue(value="uploaded_file"),
        ),
        FieldCondition(
            key="file_id",
            match=MatchValue(value=str(file_id)),
        ),
    ]

    if user_id:
        must.append(
            FieldCondition(
                key="user_id",
                match=MatchValue(value=str(user_id)),
            )
        )

    points, _ = client.scroll(
        collection_name=COLLECTION,
        scroll_filter=Filter(must=must),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )

    rows = [
        {
            "score": 1.0,
            "payload": p.payload or {},
        }
        for p in points
    ]

    rows.sort(key=lambda x: int(x["payload"].get("chunk_no") or 0))
    return rows
