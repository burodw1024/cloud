

import os
import sys
import requests
from qdrant_client import QdrantClient

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.144:31134")
COLLECTION_NAME = os.getenv("BHUMIPEDIA_COLLECTION", "bhumipedia_land")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")


def embed_text(text: str):
    # Old Ollama embedding endpoint
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

    # New Ollama embedding endpoint
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


def main():
    if len(sys.argv) < 2:
        print('Usage:')
        print('  python scripts/test_bhumipedia_qdrant_search.py "নামজারি সম্পর্কে বলুন"')
        sys.exit(1)

    question = " ".join(sys.argv[1:]).strip()

    print("=" * 80)
    print(f"Question: {question}")
    print(f"Qdrant  : {QDRANT_URL}")
    print(f"Collection: {COLLECTION_NAME}")
    print(f"Ollama  : {OLLAMA_URL}")
    print(f"Embed   : {EMBED_MODEL}")
    print("=" * 80)

    vector = embed_text(question)

    client = QdrantClient(url=QDRANT_URL)

    results = client.search(
        collection_name=COLLECTION_NAME,
        query_vector=vector,
        limit=5,
        with_payload=True,
    )

    if not results:
        print("No result found.")
        return

    for i, hit in enumerate(results, start=1):
        payload = hit.payload or {}

        title = payload.get("title", "")
        url = payload.get("url", "")
        topic = payload.get("topic", "")
        content_type = payload.get("content_type", "")
        chunk_text = payload.get("chunk_text", "")

        print("\n" + "#" * 80)
        print(f"RESULT {i}")
        print(f"Score       : {hit.score}")
        print(f"Title       : {title}")
        print(f"Topic       : {topic}")
        print(f"Content Type: {content_type}")
        print(f"URL         : {url}")
        print("-" * 80)
        print(chunk_text[:1500])
        print("#" * 80)


if __name__ == "__main__":
    main()

