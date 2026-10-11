import json
import requests


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


def search(query: str):
    vector = get_embedding(query)

    payload = {
        "vector": vector,
        "limit": 5,
        "with_payload": True,
        "filter": {
            "must": [
                {
                    "key": "category",
                    "match": {
                        "value": "loan"
                    }
                }
            ]
        }
    }

    print("Sending payload filter:")
    print(json.dumps(payload["filter"], ensure_ascii=False, indent=2))

    response = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
        json=payload,
        timeout=60
    )

    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    query = "আমার কিস্তি জমা হয়েছে কিন্তু আপডেট হয়নি"
    result = search(query)
    print(json.dumps(result, ensure_ascii=False, indent=2))
