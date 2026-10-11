import os
import requests


OLLAMA_URL = os.getenv("HR_NOTICE_OLLAMA_URL", os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"))
EMBED_MODEL = os.getenv("HR_NOTICE_EMBED_MODEL", os.getenv("EMBED_MODEL", "nomic-embed-text"))


def embed_text(text: str):
    url = f"{OLLAMA_URL.rstrip('/')}/api/embeddings"
    resp = requests.post(url, json={"model": EMBED_MODEL, "prompt": text}, timeout=(10, 300))
    resp.raise_for_status()
    data = resp.json()
    emb = data.get("embedding")
    if not emb:
        raise RuntimeError("No embedding returned from Ollama")
    return emb


def embed_texts(texts):
    return [embed_text(t) for t in texts]
