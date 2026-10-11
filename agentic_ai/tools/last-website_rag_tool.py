from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from chat.dynamic_config import (
    get_setting,
    get_qdrant_url,
    get_ollama_url,
    qdrant_headers,
)


QDRANT_COLLECTION = get_setting("QDRANT_COLLECTION_WEBSITE", "buro_website")
OLLAMA_URL = get_ollama_url()
EMBED_MODEL = get_setting("EMBED_MODEL", get_setting("EMBEDDING_MODEL", "nomic-embed-text"))
GEN_MODEL = get_setting("GEN_MODEL", "aya-23-8b:q5")

BURO_BASE_URL = "https://www.burobd.org"
TIMEOUT = 60
MAX_CONTEXT_CHARS = 9000


BAD_PHRASES = [
    "Increase font size",
    "Decrease font size",
    "Highlight links",
    "Underline links",
    "Invert images",
    "Greyscale images",
    "Go to standard site",
    "Download screen reader",
    "Quick Assistance",
    "Show in Google Maps",
    "Contact us",
    "About buro",
    "Our Coverage",
    "Our Impact",
    "Our Awards",
    "Core Values",
    "Publications",
    "Tender",
    "Career",
    "Privacy policy",
    "All BURO locations",
    "Programs & Services",
    "Development Project",
    "Copyright 2025",
    "Need a professional Webflow Designer",
    "House No:",
    "Road No.",
    "buro@burobd.org",
    "88-02-55059860",
    "88-02-55059861",
    "88-02-55059862",
]


def get_embedding(text: str) -> List[float]:
    resp = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("embedding", [])


def generate_llm(prompt: str) -> str:
    resp = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": GEN_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "top_p": 0.8,
            },
        },
        timeout=1800,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def qdrant_url() -> str:
    return get_qdrant_url(check_health=True).rstrip("/")


def clean_buro_text(text: str) -> str:
    lines = []
    seen = set()

    for raw in text.splitlines():
        line = " ".join(raw.strip().split())

        if not line:
            continue

        low = line.lower()

        if len(line) < 3:
            continue

        if any(p.lower() in low for p in BAD_PHRASES):
            continue

        # Remove large repeated menu/footer blocks
        if len(line) > 220 and low.count("buro") >= 4:
            continue

        if len(line) > 220 and ("programs" in low or "copyright" in low):
            continue

        if low in seen:
            continue

        seen.add(low)
        lines.append(line)

    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def extract_page_text(html: str) -> Tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "form", "header", "footer", "nav"]):
        tag.decompose()

    title = ""
    if soup.title:
        title = soup.title.get_text(" ", strip=True)

    candidates = []
    for selector in ["main", "article", ".w-richtext", ".rich-text", ".page-content", ".content", "body"]:
        for item in soup.select(selector):
            txt = item.get_text("\n", strip=True)
            if txt and len(txt) > 80:
                candidates.append(txt)

    text = max(candidates, key=len) if candidates else soup.get_text("\n", strip=True)
    return title, clean_buro_text(text)


def normalize_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")


def allowed_url(url: str) -> bool:
    p = urlparse(url)

    if p.netloc not in ["www.burobd.org", "burobd.org"]:
        return False

    blocked_ext = [
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        ".zip", ".rar", ".mp4", ".mp3",
    ]

    return not any(p.path.lower().endswith(x) for x in blocked_ext)


def discover_links(html: str, current_url: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()

        if not href:
            continue

        if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue

        full = normalize_url(urljoin(current_url, href))

        if allowed_url(full):
            links.append(full)

    return list(dict.fromkeys(links))


def chunk_text(text: str, size: int = 1100, overlap: int = 150) -> List[str]:
    text = text.strip()

    if not text:
        return []

    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
    current = ""

    for para in paras:
        if len(current) + len(para) + 2 <= size:
            current = f"{current}\n\n{para}".strip()
        else:
            if current:
                chunks.append(current)
            current = para

    if current:
        chunks.append(current)

    final = []
    for ch in chunks:
        if len(ch) <= size:
            final.append(ch)
        else:
            start = 0
            while start < len(ch):
                part = ch[start:start + size].strip()
                if part:
                    final.append(part)
                start += size - overlap

    return final


def ensure_collection(vector_size: int, recreate: bool = False) -> None:
    url = qdrant_url()
    headers = qdrant_headers()

    if recreate:
        try:
            requests.delete(
                f"{url}/collections/{QDRANT_COLLECTION}",
                headers=headers,
                timeout=TIMEOUT,
            )
        except Exception:
            pass

    check = requests.get(
        f"{url}/collections/{QDRANT_COLLECTION}",
        headers=headers,
        timeout=TIMEOUT,
    )

    if check.status_code == 200:
        return

    create = requests.put(
        f"{url}/collections/{QDRANT_COLLECTION}",
        json={
            "vectors": {
                "size": vector_size,
                "distance": "Cosine",
            }
        },
        headers=headers,
        timeout=TIMEOUT,
    )
    create.raise_for_status()


def point_id(url: str, idx: int, text: str) -> int:
    raw = f"{url}|{idx}|{hashlib.sha1(text.encode('utf-8')).hexdigest()}"
    return int(hashlib.sha1(raw.encode("utf-8")).hexdigest()[:15], 16)


def page_priority(url: str, title: str, text: str) -> int:
    data = f"{url}\n{title}\n{text}".lower()

    if "/governance" in data:
        return 100

    if "founder executive director" in data:
        return 95

    if "management team" in data or "governing body" in data:
        return 90

    if "/development-projects/buro-healthcare-foundation" in data:
        return 85

    if "/buro-updates" in data:
        return 50

    return 60


def upsert_points(points: List[Dict[str, Any]]) -> None:
    if not points:
        return

    resp = requests.put(
        f"{qdrant_url()}/collections/{QDRANT_COLLECTION}/points?wait=true",
        json={"points": points},
        headers=qdrant_headers(),
        timeout=180,
    )
    resp.raise_for_status()


def sync_buro_website(max_pages: int = 80, recreate_collection: bool = True) -> Dict[str, Any]:
    seeds = [
        "https://www.burobd.org/",
        "https://www.burobd.org/governance",
        "https://www.burobd.org/about-buro",
        "https://www.burobd.org/development-projects/buro-healthcare-foundation",
        "https://www.burobd.org/buro-updates",
    ]

    queue = [normalize_url(x) for x in seeds]
    visited = set()
    indexed_pages = 0
    indexed_chunks = 0
    errors = []
    vector_size = None

    session = requests.Session()
    session.headers.update({"User-Agent": "BURO-RAG-Clean-Sync/1.0"})

    while queue and len(visited) < max_pages:
        url = queue.pop(0)

        if url in visited:
            continue

        visited.add(url)

        try:
            resp = session.get(url, timeout=TIMEOUT)

            if resp.status_code != 200:
                errors.append(f"{url} -> HTTP {resp.status_code}")
                continue

            html = resp.text
            title, text = extract_page_text(html)

            for link in discover_links(html, url):
                if link not in visited and link not in queue and len(queue) < max_pages * 3:
                    queue.append(link)

            if len(text) < 80:
                continue

            chunks = chunk_text(text)

            if not chunks:
                continue

            priority = page_priority(url, title, text)
            points = []

            for idx, chunk in enumerate(chunks):
                vector = get_embedding(chunk)

                if not vector:
                    raise RuntimeError("Embedding model returned empty vector.")

                if vector_size is None:
                    vector_size = len(vector)
                    ensure_collection(vector_size, recreate=recreate_collection)
                    recreate_collection = False

                points.append({
                    "id": point_id(url, idx, chunk),
                    "vector": vector,
                    "payload": {
                        "source": "buro_website",
                        "title": title,
                        "url": url,
                        "source_url": url,
                        "text": chunk,
                        "content": chunk,
                        "chunk_index": idx,
                        "priority": priority,
                        "indexed_at": int(time.time()),
                    },
                })

            upsert_points(points)
            indexed_pages += 1
            indexed_chunks += len(points)

        except Exception as exc:
            errors.append(f"{url} -> {type(exc).__name__}: {exc}")

    return {
        "status": "ok",
        "collection": QDRANT_COLLECTION,
        "visited_pages": len(visited),
        "indexed_pages": indexed_pages,
        "indexed_chunks": indexed_chunks,
        "errors": errors[:20],
    }


def search_qdrant(query: str, limit: int = 8) -> Dict[str, Any]:
    vector = get_embedding(query)

    payload = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
        "with_vector": False,
    }

    resp = requests.post(
        f"{qdrant_url()}/collections/{QDRANT_COLLECTION}/points/search",
        json=payload,
        headers=qdrant_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def normalize_hits(query: str, result: Dict[str, Any]) -> List[Dict[str, Any]]:
    hits = []

    for item in result.get("result", []):
        payload = item.get("payload", {}) or {}
        url = payload.get("source_url") or payload.get("url") or ""

        hits.append({
            "score": item.get("score", 0),
            "title": payload.get("title") or "BURO Bangladesh Website",
            "url": url,
            "source_url": url,
            "text": payload.get("text") or payload.get("content") or "",
            "priority": payload.get("priority", 0),
        })

    return rerank_hits(query, hits)


def rerank_hits(query: str, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    q = query.lower()

    def score(hit: Dict[str, Any]) -> float:
        data = f"{hit.get('url','')} {hit.get('title','')} {hit.get('text','')}".lower()
        s = float(hit.get("score") or 0)
        s += float(hit.get("priority") or 0) / 1000

        if any(x in q for x in ["executive director", "founder executive director", "management", "governance"]):
            if "/governance" in data:
                s += 0.50
            if "founder executive director" in data:
                s += 0.40
            if "zakir hossain" in data:
                s += 0.30

        if "healthcare" in q or "health care" in q:
            if "/development-projects/buro-healthcare-foundation" in data:
                s += 0.50

        return s

    return sorted(hits, key=score, reverse=True)


def build_context(chunks: List[Dict[str, Any]]) -> str:
    lines = []
    total = 0

    for i, item in enumerate(chunks, start=1):
        title = item.get("title") or "Untitled"
        url = item.get("url") or item.get("source_url") or ""
        text = (item.get("text") or "").strip()

        if not text:
            continue

        if len(text) > 1400:
            text = text[:1400]

        block = (
            f"[Source {i}]\n"
            f"Title: {title}\n"
            f"URL: {url}\n"
            f"Content: {text}\n"
        )

        if total + len(block) > MAX_CONTEXT_CHARS:
            break

        lines.append(block)
        total += len(block)

    return "\n\n".join(lines)


def build_sources(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sources = []
    seen = set()

    for row in chunks:
        url = row.get("url") or row.get("source_url") or ""

        if not url or url in seen:
            continue

        seen.add(url)
        sources.append({
            "title": row.get("title"),
            "url": url,
            "score": row.get("score"),
        })

    return sources[:5]


def executive_director_guard(query: str, context: str, sources: List[Dict[str, Any]], language: str = "en") -> Optional[str]:
    q = query.lower()
    c = context.lower()

    if any(x in q for x in ["executive director", "founder executive director", "who is executive director"]):
        if "zakir hossain" in c and "founder executive director" in c:
            if language == "bn":
                return "BURO Bangladesh-এর Governance page অনুযায়ী, Zakir Hossain হলেন BURO Bangladesh-এর Founder Executive Director।"
            return "According to BURO Bangladesh Governance page, Zakir Hossain is the Founder Executive Director of BURO Bangladesh."

    return None


def healthcare_founder_guard(query: str, context: str, sources: List[Dict[str, Any]], language: str = "en") -> Optional[str]:
    q = query.lower()
    c = context.lower()

    asks_founder = any(x in q for x in ["founder", "founded", "who founded", "established by"])
    asks_healthcare = "healthcare" in q or "health care" in q

    if asks_founder and asks_healthcare:
        direct_evidence = any(x in c for x in ["founded by", "established by", "founder of buro healthcare"])

        if not direct_evidence:
            if language == "bn":
                return "প্রদত্ত BURO website তথ্যের মধ্যে BURO Healthcare Foundation কে প্রতিষ্ঠা করেছেন—এই নির্দিষ্ট তথ্য পাওয়া যায়নি। Healthcare Foundation page-এ শুধু এর স্বাস্থ্যসেবা কার্যক্রমের বিবরণ আছে।"
            return "I could not find this information in the indexed BURO website content. The BURO Healthcare Foundation page describes its healthcare services, but it does not clearly mention who founded it."

    return None


def generate_answer(query: str, chunks: List[Dict[str, Any]], language: str = "en") -> str:
    context = build_context(chunks)
    sources = build_sources(chunks)

    guarded = executive_director_guard(query, context, sources, language)
    if guarded:
        return guarded

    guarded = healthcare_founder_guard(query, context, sources, language)
    if guarded:
        return guarded

    if language == "bn":
        prompt = f"""
আপনি BURO Bangladesh-এর অফিসিয়াল ওয়েবসাইট RAG সহকারী।

নিয়ম:
১. শুধু নিচের Context থেকে উত্তর দিন।
২. Context-এ তথ্য না থাকলে বলুন: "প্রদত্ত ওয়েবসাইট তথ্যের মধ্যে এই প্রশ্নের নির্দিষ্ট উত্তর পাওয়া যায়নি।"
৩. Founder, Executive Director, Chairman, ownership বা establishment সম্পর্কিত তথ্য context-এ স্পষ্ট না থাকলে অনুমান করবেন না।
৪. menu, footer, address, contact number, copyright ও repeated navigation text উপেক্ষা করুন।
৫. উত্তর সহজ, পেশাদার এবং বাংলায় দিন।
৬. শেষে উৎস URL দিন।

প্রশ্ন:
{query}

Context:
{context}

বাংলা উত্তর:
""".strip()
    else:
        prompt = f"""
You are the official BURO Bangladesh website RAG assistant.

Rules:
1. Answer only from the provided context.
2. If the answer is not available in the context, say the specific answer was not found in the provided website information.
3. Do not infer Founder, Executive Director, Chairman, ownership, or establishment information unless the context explicitly says it.
4. Ignore menu, footer, address, contact number, copyright, and repeated navigation text.
5. Keep the answer professional and concise.
6. Add source URLs at the end.

Question:
{query}

Context:
{context}

Answer:
""".strip()

    return generate_llm(prompt)


def run(user=None, query: str = "", params=None, language: str = "en", **kwargs) -> Dict[str, Any]:
    try:
        params = params or {}

        # Optional: allow privileged caller to run sync through tool layer
        if params.get("action") == "sync":
            max_pages = int(params.get("max_pages", 80))
            recreate = bool(params.get("recreate", True))
            result = sync_buro_website(max_pages=max_pages, recreate_collection=recreate)
            return {
                "ok": True,
                "source": QDRANT_COLLECTION,
                "action": "sync",
                "result": result,
                "answer": "BURO website clean sync completed.",
            }

        limit = int(params.get("limit", 8))
        result = search_qdrant(query, limit=limit)
        chunks = normalize_hits(query, result)
        sources = build_sources(chunks)

        if not chunks:
            final_answer = (
                "BURO ওয়েবসাইট RAG collection-এ এই প্রশ্নের প্রাসঙ্গিক তথ্য পাওয়া যায়নি।"
                if language == "bn"
                else "No relevant information was found in the BURO website RAG collection."
            )

            return {
                "ok": True,
                "source": QDRANT_COLLECTION,
                "answer": final_answer,
                "sources": [],
                "matches": [],
            }

        final_answer = generate_answer(query, chunks, language=language)

        return {
            "ok": True,
            "source": QDRANT_COLLECTION,
            "answer": final_answer,
            "sources": sources,
            "matches_count": len(chunks),
        }

    except Exception as exc:
        return {
            "ok": False,
            "source": QDRANT_COLLECTION,
            "error": str(exc),
            "answer": f"Website RAG tool error: {exc}",
        }
