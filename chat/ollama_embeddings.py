import os
import requests
from typing import List

OLLAMA_URL = (os.getenv("OLLAMA_URL", "http://192.168.1.144:31134") or "").rstrip("/")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "60"))

def embed_text(text: str) -> List[float]:
    """
    Returns embedding vector for the given text using Ollama embeddings API.
    """
    if not text or not text.strip():
        return []

    r = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=EMBED_TIMEOUT,
    )

    r.raise_for_status()
    data = r.json()
    return data.get("embedding") or []
