from hr_notice_rag.services.embedder import embed_text
from hr_notice_rag.services.qdrant_indexer import search


def retrieve_notice_context(query: str, top_k: int = 5):
    vector = embed_text(query)
    results = search(vector, top_k=top_k)

    formatted = []
    for r in results:
        payload = r.get("payload", {}) or {}
        formatted.append({
            "score": r.get("score"),
            "document_id": payload.get("document_id"),
            "title": payload.get("title"),
            "notice_no": payload.get("notice_no"),
            "department": payload.get("department"),
            "publish_date": payload.get("publish_date"),
            "section_name": payload.get("section_name"),
            "file_name": payload.get("file_name"),
            "text": payload.get("text"),
        })
    return formatted
