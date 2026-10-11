import json
import uuid
import requests
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
JSONL_FILE = BASE_DIR / "data" / "customer_support_faq.jsonl"

QDRANT_URL = "http://10.50.1.29:31635"
COLLECTION = "customer_support"

OLLAMA_URL = "http://192.168.1.144:31134"
EMBED_MODEL = "nomic-embed-text"


def get_embedding(text: str):
    response = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={
            "model": EMBED_MODEL,
            "prompt": text
        },
        timeout=60
    )
    response.raise_for_status()
    return response.json()["embedding"]


def upsert_point(payload: dict):
    vector = get_embedding(payload["text"])

    point = {
        "id": str(uuid.uuid4()),
        "vector": vector,
        "payload": payload
    }

    response = requests.put(
        f"{QDRANT_URL}/collections/{COLLECTION}/points",
        json={
            "points": [point]
        },
        timeout=60
    )
    response.raise_for_status()
    return response.json()


def main():
    if not JSONL_FILE.exists():
        raise FileNotFoundError(f"JSONL file not found: {JSONL_FILE}")

    with open(JSONL_FILE, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue

            payload = json.loads(line)
            result = upsert_point(payload)

            print(
                f"Line {line_no}: Inserted {payload.get('topic_id')} "
                f"status={result.get('status')}"
            )


if __name__ == "__main__":
    main()
