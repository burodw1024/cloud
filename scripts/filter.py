
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import requests
from qdrant_client import QdrantClient
from qdrant_client.http import models

QDRANT_URL = os.getenv("QDRANT_URL", "http://192.168.1.176:31633")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.144:31134")
COLLECTION_NAME = os.getenv("BHUMIPEDIA_COLLECTION", "bhumipedia_land")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")


def embed_text(text):
    url1 = f"{OLLAMA_URL.rstrip('/')}/api/embeddings"
    try:
        r = requests.post(url1, json={"model": EMBED_MODEL, "prompt": text}, timeout=120)
        if r.status_code == 200 and r.json().get("embedding"):
            return r.json()["embedding"]
    except Exception:
        pass

    url2 = f"{OLLAMA_URL.rstrip('/')}/api/embed"
    r = requests.post(url2, json={"model": EMBED_MODEL, "input": text}, timeout=120)
    r.raise_for_status()
    return r.json()["embeddings"][0]


def main():
    if len(sys.argv) < 2:
        print('Usage: python search_land_filtered.py "নামজারি সম্পর্কে বলুন"')
        sys.exit(1)

    question = " ".join(sys.argv[1:])
    client = QdrantClient(url=QDRANT_URL)

    vector = embed_text(question)

    search_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="topic",
                match=models.MatchAny(any=["circulars", "guidelines"])
            )
        ]
    )

    if hasattr(client, "search"):
        results = client.search(
            collection_name=COLLECTION_NAME,
            query_vector=vector,
            query_filter=search_filter,
            limit=10,
            with_payload=True,
        )
    else:
        results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=vector,
            query_filter=search_filter,
            limit=10,
            with_payload=True,
        ).points

    print("=" * 90)
    print("QUESTION:", question)
    print("=" * 90)

    for i, hit in enumerate(results, 1):
        payload = hit.payload or {}
        print("\n" + "#" * 90)
        print("RESULT:", i)
        print("SCORE :", getattr(hit, "score", None))
        print("TITLE :", payload.get("title"))
        print("TOPIC :", payload.get("topic"))
        print("URL   :", payload.get("url"))
        print("-" * 90)
        print((payload.get("chunk_text") or "")[:2500])
        print("#" * 90)


if __name__ == "__main__":
    main()
