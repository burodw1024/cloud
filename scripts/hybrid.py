

import os
import sys
import requests
from qdrant_client import QdrantClient
from qdrant_client.http import models

QDRANT_URL = os.getenv("QDRANT_URL", "http://192.168.1.176:31633")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.144:31134")
COLLECTION_NAME = os.getenv("BHUMIPEDIA_COLLECTION", "bhumipedia_land")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")


def embed_text(text: str):
    # Ollama old endpoint
    url1 = f"{OLLAMA_URL.rstrip('/')}/api/embeddings"
    payload1 = {
        "model": EMBED_MODEL,
        "prompt": text,
    }

    try:
        r = requests.post(url1, json=payload1, timeout=120)
        if r.status_code == 200:
            data = r.json()
            if data.get("embedding"):
                return data["embedding"]
    except Exception:
        pass

    # Ollama new endpoint
    url2 = f"{OLLAMA_URL.rstrip('/')}/api/embed"
    payload2 = {
        "model": EMBED_MODEL,
        "input": text,
    }

    r = requests.post(url2, json=payload2, timeout=120)
    r.raise_for_status()
    data = r.json()

    if data.get("embeddings"):
        return data["embeddings"][0]

    raise RuntimeError(f"Embedding failed: {data}")


def keyword_scroll(client, keyword, limit=5):
    try:
        points, _ = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="chunk_text",
                        match=models.MatchText(text=keyword),
                    )
                ]
            ),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return points
    except Exception as e:
        print(f"Keyword search failed for [{keyword}]: {e}")
        return []


def vector_search(client, question, limit=5):
    vector = embed_text(question)

    # Older qdrant-client
    if hasattr(client, "search"):
        return client.search(
            collection_name=COLLECTION_NAME,
            query_vector=vector,
            limit=limit,
            with_payload=True,
        )

    # Newer qdrant-client
    return client.query_points(
        collection_name=COLLECTION_NAME,
        query=vector,
        limit=limit,
        with_payload=True,
    ).points


def print_result(i, payload, score=None, mode=""):
    title = payload.get("title", "")
    url = payload.get("url", "")
    topic = payload.get("topic", "")
    content_type = payload.get("content_type", "")
    chunk_index = payload.get("chunk_index", "")
    chunk_text = payload.get("chunk_text", "")

    print("\n" + "#" * 90)
    print(f"RESULT {i} [{mode}]")
    if score is not None:
        print(f"Score       : {score}")
    print(f"Title       : {title}")
    print(f"Topic       : {topic}")
    print(f"Content Type: {content_type}")
    print(f"Chunk Index : {chunk_index}")
    print(f"URL         : {url}")
    print("-" * 90)
    print(chunk_text[:2000])
    print("#" * 90)


def extract_keywords(question: str):
    predefined = [
        "নামজারি",
        "ই-নামজারি",
        "মিউটেশন",
        "খতিয়ান",
        "খতিয়ান",
        "খাজনা",
        "ভূমি উন্নয়ন কর",
        "জরিপ",
        "মৌজা",
        "দাগ",
        "দলিল",
        "রেকর্ড",
        "ডিসিআর",
        "DCR",
        "QR",
        "mutation",
        "khatian",
        "survey",
        "record",
        "mouza",
    ]

    q_lower = question.lower()
    found = []

    for k in predefined:
        if k.lower() in q_lower:
            found.append(k)

    # If no known keyword found, use question words as weak keyword candidates.
    if not found:
        for w in question.split():
            w = w.strip("।,.?!:;()[]{}\"'")
            if len(w) >= 4:
                found.append(w)

    return found[:5]


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print('  python search_hybrid.py "নামজারি সম্পর্কে বলুন"')
        sys.exit(1)

    question = " ".join(sys.argv[1:]).strip()
    client = QdrantClient(url=QDRANT_URL)

    print("=" * 90)
    print(f"Question   : {question}")
    print(f"Qdrant     : {QDRANT_URL}")
    print(f"Collection : {COLLECTION_NAME}")
    print(f"Ollama     : {OLLAMA_URL}")
    print(f"Embed      : {EMBED_MODEL}")
    print("=" * 90)

    printed = 0
    seen = set()

    keywords = extract_keywords(question)

    print(f"Keyword candidates: {keywords}")

    # 1. Keyword search first
    for keyword in keywords:
        matches = keyword_scroll(client, keyword, limit=5)

        for p in matches:
            payload = p.payload or {}
            key = (payload.get("url"), payload.get("chunk_index"))

            if key in seen:
                continue

            seen.add(key)
            printed += 1
            print_result(
                printed,
                payload,
                mode=f"KEYWORD: {keyword}",
            )

    # 2. Vector search after keyword search
    print("\nRunning vector search also...")
    vector_results = vector_search(client, question, limit=5)

    for hit in vector_results:
        payload = hit.payload or {}
        key = (payload.get("url"), payload.get("chunk_index"))

        if key in seen:
            continue

        seen.add(key)
        printed += 1
        print_result(
            printed,
            payload,
            score=getattr(hit, "score", None),
            mode="VECTOR",
        )

    if printed == 0:
        print("No result found.")


if __name__ == "__main__":
    main()

