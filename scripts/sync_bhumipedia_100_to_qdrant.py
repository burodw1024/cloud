

"""
Standalone Bhumipedia -> Qdrant sync script.

Purpose:
- Crawl first N pages/PDFs from https://bhumipedia.land.gov.bd/
- Extract text from HTML and PDF
- Chunk text
- Generate embeddings using local Ollama nomic-embed-text
- Upsert into Qdrant collection: bhumipedia_land

Run:
  python import/sync_bhumipedia_100_to_qdrant.py --max-pages 100 --max-depth 3

Recommended first test:
  python import/sync_bhumipedia_100_to_qdrant.py --max-pages 20 --max-depth 2
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from io import BytesIO
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urldefrag

import fitz  # PyMuPDF
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from qdrant_client import QdrantClient
from qdrant_client.http import models


# ============================================================
# Config
# ============================================================

BASE_URL = os.getenv("BHUMIPEDIA_BASE_URL", "https://bhumipedia.land.gov.bd/")
COLLECTION_NAME = os.getenv("BHUMIPEDIA_COLLECTION", "bhumipedia_land")

QDRANT_URL = os.getenv("QDRANT_URL", "http://192.168.1.176:31633")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.144:31134")

EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
VECTOR_SIZE = int(os.getenv("EMBED_VECTOR_SIZE", "768"))

RAW_DIR = os.getenv(
    "BHUMIPEDIA_RAW_DIR",
    "/home/mamun/finalapp/app/uploads/bhumipedia_raw"
)

USER_AGENT = os.getenv(
    "BHUMIPEDIA_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) BhumipediaRAGBot/1.0"
)

REQUEST_TIMEOUT = int(os.getenv("BHUMIPEDIA_REQUEST_TIMEOUT", "45"))
PAGE_TIMEOUT_MS = int(os.getenv("BHUMIPEDIA_PAGE_TIMEOUT_MS", "45000"))

CHUNK_WORDS = int(os.getenv("BHUMIPEDIA_CHUNK_WORDS", "850"))
CHUNK_OVERLAP_WORDS = int(os.getenv("BHUMIPEDIA_CHUNK_OVERLAP_WORDS", "120"))

UPSERT_BATCH_SIZE = int(os.getenv("BHUMIPEDIA_UPSERT_BATCH_SIZE", "32"))
CRAWL_DELAY_SECONDS = float(os.getenv("BHUMIPEDIA_CRAWL_DELAY_SECONDS", "0.3"))


# ============================================================
# Utility
# ============================================================

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def normalize_url(url: str) -> str:
    url, _frag = urldefrag(url)
    url = url.strip()
    return url


def is_same_domain(url: str, base_url: str = BASE_URL) -> bool:
    try:
        base_host = urlparse(base_url).netloc.lower()
        host = urlparse(url).netloc.lower()
        return host == base_host
    except Exception:
        return False


def is_probably_file(url: str) -> bool:
    lower = url.lower()
    return any(
        lower.endswith(ext)
        for ext in [
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".jpg", ".jpeg", ".png", ".gif", ".zip", ".rar"
        ]
    )


def is_pdf_url(url: str) -> bool:
    return url.lower().split("?")[0].endswith(".pdf")


def safe_filename_from_url(url: str, suffix: str = ".txt") -> str:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return f"{h}{suffix}"


def clean_text(text: str) -> str:
    if not text:
        return ""

    text = text.replace("\x00", " ")
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\t+", " ", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            lines.append("")
            continue

        # Remove very noisy repeated UI words if needed.
        if line.lower() in {"javascript", "enable javascript"}:
            continue

        lines.append(line)

    return "\n".join(lines).strip()


def detect_language(text: str) -> str:
    if not text:
        return "unknown"

    bangla_chars = len(re.findall(r"[\u0980-\u09FF]", text))
    latin_chars = len(re.findall(r"[A-Za-z]", text))

    if bangla_chars > latin_chars:
        return "bn"
    if latin_chars > 0:
        return "en"
    return "unknown"


def infer_topic_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return "home"

    parts = [p for p in path.split("/") if p]
    if not parts:
        return "home"

    if "PDF" in parts or "pdf" in [p.lower() for p in parts]:
        return "pdf"

    return parts[0][:100]


def stable_point_id(url: str, chunk_index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{url}#chunk={chunk_index}"))


# ============================================================
# Extraction
# ============================================================

def extract_title_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)

    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)

    h2 = soup.find("h2")
    if h2:
        return h2.get_text(" ", strip=True)

    return ""


def html_to_text_and_links(html: str, url: str) -> Tuple[str, str, List[str]]:
    soup = BeautifulSoup(html, "html.parser")

    title = extract_title_from_html(html)

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    links = []
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not href:
            continue

        full = normalize_url(urljoin(url, href))
        if not full:
            continue

        if not is_same_domain(full):
            continue

        # Avoid useless assets except PDF.
        if is_probably_file(full) and not is_pdf_url(full):
            continue

        links.append(full)

    main_candidates = [
        soup.find("main"),
        soup.find("article"),
        soup.find("div", class_=re.compile("content|main|body", re.I)),
        soup.body,
        soup,
    ]

    text = ""
    for candidate in main_candidates:
        if candidate:
            text = candidate.get_text("\n", strip=True)
            if len(text) > 300:
                break

    return title, clean_text(text), sorted(set(links))


def extract_pdf_text(url: str) -> Tuple[str, str]:
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    pdf_bytes = resp.content
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    all_text = []
    title = ""

    try:
        meta = doc.metadata or {}
        title = meta.get("title") or ""
    except Exception:
        title = ""

    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        txt = page.get_text("text")
        if txt:
            all_text.append(f"\n\n[PDF Page {page_num + 1}]\n{txt}")

    text = clean_text("\n".join(all_text))

    if not title:
        parsed = urlparse(url)
        title = os.path.basename(parsed.path)

    return title, text


async def fetch_html_with_playwright(page, url: str) -> Optional[str]:
    try:
        await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(1000)
        html = await page.content()
        return html
    except PlaywrightTimeoutError:
        log(f"TIMEOUT: {url}")
        return None
    except Exception as e:
        log(f"HTML FETCH ERROR: {url} -> {e}")
        return None


# ============================================================
# Chunking
# ============================================================

def split_text_into_chunks(text: str, max_words: int = CHUNK_WORDS, overlap_words: int = CHUNK_OVERLAP_WORDS) -> List[str]:
    text = clean_text(text)
    if not text:
        return []

    # Paragraph-aware first.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks = []
    current_words = []

    for para in paragraphs:
        words = para.split()
        if not words:
            continue

        if len(current_words) + len(words) <= max_words:
            current_words.extend(words)
        else:
            if current_words:
                chunks.append(" ".join(current_words).strip())

            if overlap_words > 0 and current_words:
                current_words = current_words[-overlap_words:]
            else:
                current_words = []

            # Very large paragraph split.
            if len(words) > max_words:
                start = 0
                while start < len(words):
                    end = start + max_words
                    part = words[start:end]
                    if part:
                        chunks.append(" ".join(part).strip())
                    start = max(0, end - overlap_words)
                    if start >= len(words):
                        break
                current_words = []
            else:
                current_words.extend(words)

    if current_words:
        chunks.append(" ".join(current_words).strip())

    # Filter weak chunks.
    final_chunks = []
    for c in chunks:
        if len(c) < 100:
            continue
        final_chunks.append(c)

    return final_chunks


# ============================================================
# Ollama Embedding
# ============================================================

def ollama_embed(text: str) -> List[float]:
    """
    Supports both:
    - /api/embeddings old response: {"embedding": [...]}
    - /api/embed newer response: {"embeddings": [[...]]}
    """
    text = text.strip()
    if not text:
        raise ValueError("Empty text cannot be embedded")

    # Try old endpoint first.
    url1 = f"{OLLAMA_URL.rstrip('/')}/api/embeddings"
    payload1 = {
        "model": EMBED_MODEL,
        "prompt": text,
    }

    try:
        r = requests.post(url1, json=payload1, timeout=120)
        if r.status_code == 200:
            data = r.json()
            emb = data.get("embedding")
            if emb:
                return emb
    except Exception:
        pass

    # Try newer endpoint.
    url2 = f"{OLLAMA_URL.rstrip('/')}/api/embed"
    payload2 = {
        "model": EMBED_MODEL,
        "input": text,
    }

    r = requests.post(url2, json=payload2, timeout=120)
    r.raise_for_status()
    data = r.json()

    embeddings = data.get("embeddings")
    if embeddings and isinstance(embeddings, list):
        return embeddings[0]

    raise RuntimeError(f"Could not get embedding from Ollama. Response: {data}")


# ============================================================
# Qdrant
# ============================================================

def get_qdrant_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL)


def ensure_collection(client: QdrantClient) -> None:
    existing = [c.name for c in client.get_collections().collections]

    if COLLECTION_NAME in existing:
        log(f"Qdrant collection exists: {COLLECTION_NAME}")
        return

    log(f"Creating Qdrant collection: {COLLECTION_NAME}")

    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=models.VectorParams(
            size=VECTOR_SIZE,
            distance=models.Distance.COSINE,
        ),
    )


def upsert_points(client: QdrantClient, points: List[models.PointStruct]) -> None:
    if not points:
        return

    client.upsert(
        collection_name=COLLECTION_NAME,
        points=points,
        wait=True,
    )


# ============================================================
# Storage
# ============================================================

def save_raw_doc(url: str, title: str, text: str, metadata: Dict) -> None:
    os.makedirs(RAW_DIR, exist_ok=True)

    base = safe_filename_from_url(url, suffix=".json")
    path = os.path.join(RAW_DIR, base)

    data = {
        "url": url,
        "title": title,
        "text": text,
        "metadata": metadata,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# Crawl + Ingest
# ============================================================

async def crawl_and_ingest(max_pages: int, max_depth: int, start_url: str) -> None:
    client = get_qdrant_client()
    ensure_collection(client)

    visited: Set[str] = set()
    queued: Set[str] = set()

    queue = deque()
    start_url = normalize_url(start_url)
    queue.append((start_url, 0))
    queued.add(start_url)

    total_pages_processed = 0
    total_chunks_upserted = 0
    total_failed = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        page = await browser.new_page(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
        )

        while queue and total_pages_processed < max_pages:
            url, depth = queue.popleft()

            if url in visited:
                continue
            visited.add(url)

            if depth > max_depth:
                continue

            log(f"Processing [{total_pages_processed + 1}/{max_pages}] depth={depth}: {url}")

            title = ""
            text = ""
            links: List[str] = []
            content_type = "html"

            try:
                if is_pdf_url(url):
                    content_type = "pdf"
                    title, text = extract_pdf_text(url)
                else:
                    html = await fetch_html_with_playwright(page, url)
                    if not html:
                        total_failed += 1
                        continue

                    title, text, links = html_to_text_and_links(html, url)

                text = clean_text(text)

                if len(text) < 150:
                    log(f"SKIP weak text: {url} len={len(text)}")
                else:
                    topic = infer_topic_from_url(url)
                    language = detect_language(text)
                    chunks = split_text_into_chunks(text)

                    if not chunks:
                        log(f"SKIP no chunks: {url}")
                    else:
                        metadata_base = {
                            "source": "bhumipedia",
                            "collection": COLLECTION_NAME,
                            "url": url,
                            "title": title or url,
                            "topic": topic,
                            "content_type": content_type,
                            "language": language,
                            "crawl_depth": depth,
                            "last_crawled": datetime.now(timezone.utc).isoformat(),
                        }

                        save_raw_doc(url, title, text, metadata_base)

                        batch: List[models.PointStruct] = []

                        for idx, chunk in enumerate(chunks):
                            try:
                                vector = ollama_embed(chunk)

                                if len(vector) != VECTOR_SIZE:
                                    raise RuntimeError(
                                        f"Embedding vector size mismatch. Got {len(vector)}, expected {VECTOR_SIZE}"
                                    )

                                payload = dict(metadata_base)
                                payload.update({
                                    "chunk_index": idx,
                                    "chunk_count": len(chunks),
                                    "chunk_text": chunk,
                                })

                                point = models.PointStruct(
                                    id=stable_point_id(url, idx),
                                    vector=vector,
                                    payload=payload,
                                )

                                batch.append(point)

                                if len(batch) >= UPSERT_BATCH_SIZE:
                                    upsert_points(client, batch)
                                    total_chunks_upserted += len(batch)
                                    log(f"Upserted batch: {len(batch)} chunks")
                                    batch = []

                            except Exception as e:
                                total_failed += 1
                                log(f"CHUNK ERROR: {url} chunk={idx} -> {e}")

                        if batch:
                            upsert_points(client, batch)
                            total_chunks_upserted += len(batch)
                            log(f"Upserted batch: {len(batch)} chunks")

                # Enqueue links only from HTML pages.
                if depth < max_depth:
                    for link in links:
                        link = normalize_url(link)

                        if not link:
                            continue
                        if link in visited or link in queued:
                            continue
                        if not is_same_domain(link):
                            continue

                        # Keep PDFs, skip other files.
                        if is_probably_file(link) and not is_pdf_url(link):
                            continue

                        queue.append((link, depth + 1))
                        queued.add(link)

                total_pages_processed += 1
                time.sleep(CRAWL_DELAY_SECONDS)

            except Exception as e:
                total_failed += 1
                log(f"PAGE ERROR: {url} -> {e}")

        await browser.close()

    log("=" * 70)
    log("SYNC COMPLETE")
    log(f"Pages processed     : {total_pages_processed}")
    log(f"Chunks upserted     : {total_chunks_upserted}")
    log(f"Failed items        : {total_failed}")
    log(f"Collection          : {COLLECTION_NAME}")
    log(f"Qdrant URL          : {QDRANT_URL}")
    log(f"Raw saved path      : {RAW_DIR}")
    log("=" * 70)


# ============================================================
# CLI
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(description="Sync Bhumipedia website data to Qdrant")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=100,
        help="Maximum number of pages/PDFs to process"
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="Maximum crawl depth"
    )
    parser.add_argument(
        "--start-url",
        type=str,
        default=BASE_URL,
        help="Start URL"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    log("Starting Bhumipedia -> Qdrant sync")
    log(f"Start URL       : {args.start_url}")
    log(f"Max pages       : {args.max_pages}")
    log(f"Max depth       : {args.max_depth}")
    log(f"Qdrant URL      : {QDRANT_URL}")
    log(f"Collection      : {COLLECTION_NAME}")
    log(f"Ollama URL      : {OLLAMA_URL}")
    log(f"Embedding model : {EMBED_MODEL}")
    log(f"Vector size     : {VECTOR_SIZE}")

    asyncio.run(
        crawl_and_ingest(
            max_pages=args.max_pages,
            max_depth=args.max_depth,
            start_url=args.start_url,
        )
    )


if __name__ == "__main__":
    main()

