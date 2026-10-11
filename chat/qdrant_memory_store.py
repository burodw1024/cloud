import os
import uuid
from typing import List

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from .ollama_embeddings import embed_text

QDRANT_URL = (os.getenv("QDRANT_URL", "http://192.168.1.144:6333") or "").rstrip("/")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")  # if you use one
LTM_COLLECTION = os.getenv("LTM_COLLECTION", "user_long_memory")

# Same vector size you use elsewhere (often 768 with nomic-embed-text)
LTM_VECTOR_SIZE = int(os.getenv("LTM_VECTOR_SIZE", "768"))
LTM_DISTANCE = os.getenv("LTM_DISTANCE", "Cosine")  # Cosine / Dot / Euclid

from typing import Optional, Dict
def _distance():
    d = (LTM_DISTANCE or "Cosine").lower()
    if d == "dot":
        return qmodels.Distance.DOT
    if d in ("euclid", "euclidean"):
        return qmodels.Distance.EUCLID
    return qmodels.Distance.COSINE

def get_client() -> QdrantClient:
    if QDRANT_API_KEY:
        return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    return QdrantClient(url=QDRANT_URL)

def ensure_collection() -> None:
    """
    Create the long memory collection if it does not exist.
    Call once on startup or lazily before first upsert/search.
    """
    client = get_client()
    existing = [c.name for c in client.get_collections().collections]
    if LTM_COLLECTION in existing:
        return

    client.create_collection(
        collection_name=LTM_COLLECTION,
        vectors_config=qmodels.VectorParams(
            size=LTM_VECTOR_SIZE,
            distance=_distance(),
        ),
    )
    # Optional: index user_id for faster filtering
    client.create_payload_index(
        collection_name=LTM_COLLECTION,
        field_name="user_id",
        field_schema=qmodels.PayloadSchemaType.INTEGER,
    )

def save_long_term_memory(user_id: int, text: str, meta: Optional[Dict] = None) -> str:
    """
    Save a memory string for a user into Qdrant.
    Returns point_id.
    """
    if not text or not text.strip():
        return ""

    ensure_collection()
    vec = embed_text(text)
    if not vec:
        return ""

    point_id = str(uuid.uuid4())
    payload = {"user_id": int(user_id), "text": text}
    if meta:
        payload.update(meta)

    client = get_client()
    client.upsert(
        collection_name=LTM_COLLECTION,
        points=[
            qmodels.PointStruct(
                id=point_id,
                vector=vec,
                payload=payload,
            )
        ],
    )
    return point_id

def search_long_memory(user_id: int, query: str, limit: int = 5) -> List[str]:
    """
    Semantic recall of user memories from Qdrant.
    Returns list of memory texts.
    """
    if not query or not query.strip():
        return []

    ensure_collection()
    vec = embed_text(query)
    if not vec:
        return []

    client = get_client()
    res = client.search(
        collection_name=LTM_COLLECTION,
        query_vector=vec,
        limit=int(limit),
        query_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="user_id",
                    match=qmodels.MatchValue(value=int(user_id)),
                )
            ]
        ),
    )
    out = []
    for r in res:
        p = r.payload or {}
        t = p.get("text")
        if t:
            out.append(t)
    return out
