import os
import requests
from hr_notice_rag.utils.ids import make_point_id

QDRANT_URL = os.getenv("HR_NOTICE_QDRANT_URL", os.getenv("QDRANT_URL", "http://127.0.0.1:6333"))
QDRANT_COLLECTION = os.getenv("HR_NOTICE_QDRANT_COLLECTION", "hr_notices")


def _qdrant(endpoint: str) -> str:
    return f"{QDRANT_URL.rstrip('/')}{endpoint}"


def _raise_with_body(resp):
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"Qdrant error {resp.status_code}: {resp.text}") from e


def ensure_collection(vector_size: int):
    url = _qdrant(f"/collections/{QDRANT_COLLECTION}")
    resp = requests.get(url, timeout=(10, 60))

    if resp.status_code == 200:
        data = resp.json()
        try:
            existing_size = data["result"]["config"]["params"]["vectors"]["size"]
        except Exception:
            existing_size = None

        if existing_size is not None and existing_size != vector_size:
            raise RuntimeError(
                f"Qdrant collection '{QDRANT_COLLECTION}' vector size mismatch: "
                f"existing={existing_size}, new={vector_size}"
            )
        return

    create_resp = requests.put(
        url,
        json={
            "vectors": {
                "size": vector_size,
                "distance": "Cosine",
            }
        },
        timeout=(10, 60),
    )
    _raise_with_body(create_resp)


def delete_document_chunks(document_id: int):
    url = _qdrant(f"/collections/{QDRANT_COLLECTION}/points/delete")
    payload = {
        "filter": {
            "must": [
                {
                    "key": "document_id",
                    "match": {"value": int(document_id)},
                }
            ]
        }
    }
    resp = requests.post(url, json=payload, timeout=(10, 120))
    _raise_with_body(resp)


def upsert_document_chunks(document, chunks, vectors):
    if not vectors:
        return []

    ensure_collection(len(vectors[0]))

    points = []
    point_ids = []

    for chunk, vector in zip(chunks, vectors):
        point_id = make_point_id(document.id, chunk["chunk_index"], chunk["chunk_text"])
        point_ids.append(point_id)

        points.append(
            {
                "id": point_id,
                "vector": vector,
                "payload": {
                    "document_id": int(document.id),
                    "title": document.title or "",
                    "notice_no": document.notice_no or "",
                    "department": document.department or "",
                    "publish_date": document.publish_date.isoformat() if document.publish_date else "",
                    "section_name": chunk.get("section_name", ""),
                    "chunk_index": int(chunk["chunk_index"]),
                    "file_name": document.file_name or "",
                    "source_type": "docx",
                    "text": chunk["chunk_text"],
                },
            }
        )

    url = _qdrant(f"/collections/{QDRANT_COLLECTION}/points")
    resp = requests.put(url, json={"points": points}, timeout=(10, 300))
    _raise_with_body(resp)

    return point_ids


def search(query_vector, top_k=5):
    url = _qdrant(f"/collections/{QDRANT_COLLECTION}/points/search")
    resp = requests.post(
        url,
        json={
            "vector": query_vector,
            "limit": top_k,
            "with_payload": True,
        },
        timeout=(10, 120),
    )
    _raise_with_body(resp)
    return resp.json().get("result", [])
