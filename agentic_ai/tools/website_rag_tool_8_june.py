from __future__ import annotations

# Final complete version: Qdrant-first RAG + live source URL expansion, fixed language detection, and stable deterministic website answers.

import hashlib
import os
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


def _runtime_setting(name: str, default: str = "") -> str:
    """
    Read runtime setting with Kubernetes/env priority first, then dynamic settings.

    This is important because Django dynamic_config/get_setting can keep an
    older GEN_MODEL, while Kubernetes env is often what you patch during
    production troubleshooting.
    """
    env_value = os.getenv(name)
    if env_value is not None and str(env_value).strip():
        return str(env_value).strip()

    try:
        value = get_setting(name, default)
        return str(value).strip() if value is not None else default
    except Exception:
        return default


QDRANT_COLLECTION = _runtime_setting("QDRANT_COLLECTION_WEBSITE", "buro_website")
OLLAMA_URL = get_ollama_url()
EMBED_MODEL = _runtime_setting("EMBED_MODEL", _runtime_setting("EMBEDDING_MODEL", "nomic-embed-text"))

# Model split:
# - English website answers work better with qwen2.5-1.5b:local in your Ollama list.
# - Bangla answers can continue using aya-expanse:8b.
# You may override these with Kubernetes env vars.
GEN_MODEL = _runtime_setting(
    "WEBSITE_ANSWER_MODEL",
    _runtime_setting("GEN_MODEL", _runtime_setting("OLLAMA_MODEL", "aya-expanse:8b")),
)
GEN_MODEL_EN = _runtime_setting("WEBSITE_ANSWER_MODEL_EN", "qwen2.5-1.5b:local")
GEN_MODEL_BN = _runtime_setting("WEBSITE_ANSWER_MODEL_BN", GEN_MODEL or "aya-expanse:8b")

BURO_BASE_URL = "https://www.burobd.org"
TIMEOUT = 60
MAX_CONTEXT_CHARS = 9000


def _int_setting(name: str, default: int) -> int:
    try:
        return int(get_setting(name, default))
    except Exception:
        return default


# Keep answer generation bounded so the UI does not stay "Thinking..." too long.
GEN_TIMEOUT = _int_setting("OLLAMA_GENERATE_TIMEOUT", 180)
GEN_NUM_PREDICT = _int_setting("OLLAMA_NUM_PREDICT_WEBSITE", 450)

# Live source expansion: after Qdrant finds source URLs, optionally fetch
# the top matched website pages live and add their extracted text to context.
# This helps with profile/detail pages where the indexed chunk is too small.
WEBSITE_LIVE_EXPAND_ENABLED = _runtime_setting("WEBSITE_LIVE_EXPAND_ENABLED", "1").lower() in ("1", "true", "yes", "y", "on")
WEBSITE_LIVE_EXPAND_TOP_N = _int_setting("WEBSITE_LIVE_EXPAND_TOP_N", 3)
WEBSITE_LIVE_EXPAND_MAX_CHARS_PER_PAGE = _int_setting("WEBSITE_LIVE_EXPAND_MAX_CHARS_PER_PAGE", 7000)
WEBSITE_LIVE_EXPAND_TIMEOUT = _int_setting("WEBSITE_LIVE_EXPAND_TIMEOUT", 45)


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


BANGLA_REQUEST_KEYWORDS = [
    "bangla",
    "bengali",
    "in bangla",
    "answer in bangla",
    "respond in bangla",
    "reply in bangla",
    "convert into bangla",
    "translate into bangla",
    "bangla response",
    "বাংলা",
    "বাংলায়",
    "বাংলায়",
    "বাংলাতে",
    "বাংলা করে",
    "বাংলায় বলুন",
    "বাংলায় বলুন",
    "বাংলায় উত্তর",
    "বাংলায় উত্তর",
]


def contains_bangla_script(text: str) -> bool:
    return bool(re.search(r"[\u0980-\u09FF]", text or ""))


def wants_bangla(text: str) -> bool:
    """
    Detect whether the user explicitly requested Bangla/Bengali response.

    Critical fix:
    - Do NOT treat the word "Bangladesh" as Bangla.
    - Only return True for actual Bangla script or explicit language requests
      like "in Bangla", "answer in Bangla", "বাংলায়", etc.
    """
    raw = text or ""
    low = raw.lower()

    # Actual Bangla script means the user likely wants Bangla.
    if contains_bangla_script(raw):
        return True

    # Explicit English phrases requesting Bangla/Bengali.
    phrase_patterns = [
        r"\bin\s+bangla\b",
        r"\bin\s+bengali\b",
        r"\banswer\s+in\s+bangla\b",
        r"\banswer\s+in\s+bengali\b",
        r"\brespond\s+in\s+bangla\b",
        r"\brespond\s+in\s+bengali\b",
        r"\breply\s+in\s+bangla\b",
        r"\breply\s+in\s+bengali\b",
        r"\bconvert\s+(into|to)\s+bangla\b",
        r"\bconvert\s+(into|to)\s+bengali\b",
        r"\btranslate\s+(into|to)\s+bangla\b",
        r"\btranslate\s+(into|to)\s+bengali\b",
        r"\bbangla\s+(response|answer|version)\b",
        r"\bbengali\s+(response|answer|version)\b",
        r"\bbn(?:-bd)?\b",
    ]

    if any(re.search(pattern, low) for pattern in phrase_patterns):
        return True

    # Single word language request only. This prevents "Bangladesh" from
    # matching "bangla" by substring.
    if re.search(r"(^|\s)(bangla|bengali)(\s|$)", low):
        return True

    return False


def normalize_language(query: str = "", language: str = "en", params: Optional[Dict[str, Any]] = None) -> str:
    """
    Resolve output language.

    Priority:
    1. params['language'] / params['lang']
    2. explicit language argument
    3. query content only if it clearly asks Bangla/Bengali

    Important:
    - language='en' stays English for "BURO Bangladesh" questions.
    - "Bangladesh" must never trigger Bangla mode.
    """
    params = params or {}

    raw_lang = (
        params.get("language")
        or params.get("lang")
        or language
        or ""
    )

    lang = str(raw_lang).strip().lower()

    if lang in ["bn", "bn-bd", "bangla", "bengali", "বাংলা"]:
        return "bn"

    if lang in ["en", "en-us", "english"]:
        if wants_bangla(query):
            return "bn"
        return "en"

    if wants_bangla(query):
        return "bn"

    return "en"


def contains_devanagari_script(text: str) -> bool:
    return bool(re.search(r"[\u0900-\u097F]", text or ""))


def _needs_language_retry(answer: str, language: str) -> bool:
    """
    Detect bad language mixing in generated answer.

    - English answer must not contain Bangla or Devanagari script.
    - Bangla answer must not contain Devanagari script.
    """
    if not answer:
        return False

    if language == "en":
        return contains_bangla_script(answer) or contains_devanagari_script(answer)

    if language == "bn":
        return contains_devanagari_script(answer)

    return False


def _language_instruction(language: str) -> str:
    if language == "bn":
        return (
            "উত্তর অবশ্যই পরিষ্কার, প্রমিত বাংলায় দিন। "
            "বাংলা-হিন্দি মিশ্রিত শব্দ ব্যবহার করবেন না। "
            "দেবনাগরী/হিন্দি অক্ষর ব্যবহার করবেন না। "
            "BURO Bangladesh, URL, official title বা product/service name প্রয়োজন হলে ইংরেজিতে রাখা যাবে।"
        )

    return (
        "Answer strictly in English. "
        "Do not answer in Bangla. "
        "Do not use Bangla script. "
        "Do not use Hindi or Devanagari script. "
        "Use clear professional English only."
    )


def _empty_context_answer(language: str) -> str:
    if language == "bn":
        return "প্রদত্ত BURO website তথ্যের মধ্যে এই প্রশ্নের নির্দিষ্ট উত্তর পাওয়া যায়নি।"
    return "The specific answer was not found in the provided BURO website information."


def select_generation_model(language: str = "en") -> str:
    """Choose the safest model for the requested output language."""
    if language == "en":
        return GEN_MODEL_EN or GEN_MODEL or "qwen2.5-1.5b:local"
    if language == "bn":
        return GEN_MODEL_BN or GEN_MODEL or "aya-expanse:8b"
    return GEN_MODEL or "aya-expanse:8b"


def get_embedding(text: str) -> List[float]:
    resp = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("embedding", [])


def generate_llm(
    prompt: str,
    language: str = "en",
    *,
    num_predict: Optional[int] = None,
    retry_prompt: Optional[str] = None,
) -> str:
    """
    Generate the final answer through Ollama.

    Important production settings:
    - keep_alive keeps the model warm after first load.
    - num_predict is bounded but configurable per request.
    - low temperature reduces hallucination and language mixing.
    """
    try:
        predict = int(num_predict or GEN_NUM_PREDICT)
    except Exception:
        predict = GEN_NUM_PREDICT

    predict = max(80, min(predict, 900))

    options = {
        "temperature": 0.08,
        "top_p": 0.80,
        "repeat_penalty": 1.15,
        "num_ctx": 4096,
        "num_predict": predict,
    }

    if language == "bn":
        options.update({
            "temperature": 0.05,
            "top_p": 0.70,
            "repeat_penalty": 1.18,
        })

    model = select_generation_model(language)

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30m",
        "options": options,
    }

    resp = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json=payload,
        timeout=GEN_TIMEOUT,
    )
    resp.raise_for_status()
    answer = resp.json().get("response", "").strip()

    # One retry for language mixing. This fixes cases where an English
    # question returns Bangla/Hindi-mixed text, or Bangla contains Devanagari.
    if retry_prompt and _needs_language_retry(answer, language):
        retry_payload = {
            "model": model,
            "prompt": retry_prompt,
            "stream": False,
            "keep_alive": "30m",
            "options": {
                **options,
                "temperature": 0.03,
                "top_p": 0.65,
                "num_predict": predict,
            },
        }
        retry_resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=retry_payload,
            timeout=GEN_TIMEOUT,
        )
        retry_resp.raise_for_status()
        retry_answer = retry_resp.json().get("response", "").strip()
        if retry_answer:
            answer = retry_answer

    return answer


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
    """
    Static priority used as a small secondary ranking signal.
    Higher value = slightly more important during reranking.
    """
    data = f"{url}\n{title}\n{text}".lower()

    if "/programs" in data:
        return 120

    if any(path in data for path in [
        "/general-loans", "/agricultural-loans", "/sme-loans",
        "/emergency-loans", "/disaster-loans", "/water-sanitation-loans",
        "/consumer-loans",
    ]):
        return 115

    if any(path in data for path in [
        "/general-savings", "/contractual-savings", "/voluntary-savings",
    ]):
        return 110

    if "/remittance" in data or "/buro-remittance-service" in data:
        return 108

    if "/about-buro" in data:
        return 105

    if "/governance" in data:
        return 100

    if "founder executive director" in data:
        return 95

    if "management team" in data or "governing body" in data:
        return 90

    if "/development-projects/buro-healthcare-foundation" in data:
        return 85

    if "/development-projects/digital-healthcare-services" in data:
        return 83

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
    # Keep explicit seed URLs for high-value service/program pages.
    # The crawler still discovers links, but explicit seeds prevent important
    # BURO service pages from being missed or ranked too weakly.
    seeds = [
        "https://www.burobd.org/",
        "https://www.burobd.org/about-buro",
        "https://www.burobd.org/programs",
        "https://www.burobd.org/general-loans",
        "https://www.burobd.org/agricultural-loans",
        "https://www.burobd.org/sme-loans",
        "https://www.burobd.org/emergency-loans",
        "https://www.burobd.org/disaster-loans",
        "https://www.burobd.org/water-sanitation-loans",
        "https://www.burobd.org/consumer-loans",
        "https://www.burobd.org/general-savings",
        "https://www.burobd.org/contractual-savings",
        "https://www.burobd.org/voluntary-savings",
        "https://www.burobd.org/remittance",
        "https://www.burobd.org/buro-remittance-service",
        "https://www.burobd.org/governance",
        "https://www.burobd.org/development-projects/digital-healthcare-services",
        "https://www.burobd.org/development-projects/buro-healthcare-foundation",
        "https://www.burobd.org/buro-reports",
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


def _query_has_any(query: str, phrases: List[str]) -> bool:
    q = (query or "").lower()
    return any(p in q for p in phrases)


def _intent_flags(query: str) -> Dict[str, bool]:
    q = (query or "").lower()

    loan = _query_has_any(q, [
        "loan", "loans", "loan service", "loan services", "loan product", "loan products",
        "buro loan", "agricultural loan", "sme loan", "general loan", "emergency loan",
        "disaster loan", "water sanitation loan", "water & sanitation loan", "consumer loan",
        "ঋণ", "লোন", "ঋণ সেবা", "লোন সেবা",
    ])

    savings = _query_has_any(q, [
        "saving", "savings", "general savings", "contractual savings", "voluntary savings",
        "সঞ্চয়", "সঞ্চয়", "সেভিংস",
    ])

    remittance = _query_has_any(q, [
        "remittance", "buro remittance", "foreign earnings", "রেমিট্যান্স",
    ])

    healthcare = _query_has_any(q, [
        "healthcare", "health care", "health", "medical", "স্বাস্থ্য", "হেলথ",
    ])

    governance = _query_has_any(q, [
        "executive director", "founder executive director", "director", "director profile",
        "leadership", "leadership profile", "management", "management team", "governance",
        "chairman", "board", "governing body", "zakir hossain",
        "পরিচালনা", "গভর্নেন্স", "নির্বাহী পরিচালক", "পরিচালক", "ডিরেক্টর",
        "ডিরেক্টর প্রোফাইল", "প্রোফাইল", "নেতৃত্ব", "ম্যানেজমেন্ট",
    ])

    reports = _query_has_any(q, [
        "report", "reports", "annual report", "publication", "publications", "রিপোর্ট", "প্রতিবেদন",
    ])

    about = _query_has_any(q, [
        "about buro", "about buro bangladesh", "tell me about", "tell about", "what is buro",
        "what is buro bangladesh", "who is buro", "buro bangladesh", "বুরো সম্পর্কে",
        "বুরো বাংলাদেশ", "বুরো কী", "বুরো কি",
    ])

    services = _query_has_any(q, [
        "service", "services", "program", "programs", "product", "products", "what can buro offer",
        "সেবা", "প্রোগ্রাম", "কর্মসূচি",
    ]) or loan or savings or remittance or healthcare

    return {
        "about": about,
        "services": services,
        "loan": loan,
        "savings": savings,
        "remittance": remittance,
        "healthcare": healthcare,
        "governance": governance,
        "reports": reports,
    }


def expand_search_query(query: str) -> str:
    """
    Expand short user questions into terms that are known to exist in the
    indexed BURO website pages. This improves Qdrant retrieval without changing
    the user's original question for answer generation.
    """
    q = query or ""
    flags = _intent_flags(q)

    extra: List[str] = []

    if flags["loan"]:
        extra.append(
            "BURO Bangladesh loan services Programs Agricultural Loans SME Loans General Loans "
            "Emergency Loans Disaster Loans Water Sanitation Loans Consumer Loans accessible financial solutions"
        )

    if flags["savings"]:
        extra.append(
            "BURO Bangladesh savings services General Savings Contractual Savings Voluntary Savings secure savings stable future clients"
        )

    if flags["remittance"]:
        extra.append(
            "BURO Bangladesh remittance service international remittance foreign earnings expatriates branch network beneficiaries"
        )

    if flags["services"] and not (flags["loan"] or flags["savings"] or flags["remittance"]):
        extra.append(
            "BURO Bangladesh Our Services Programs Loan Savings Individual Loan Remittance Digital Healthcare Services "
            "Microfinance Agricultural Financing SME Financing WaSH Financing"
        )

    if flags["healthcare"]:
        extra.append(
            "BURO Bangladesh healthcare foundation digital healthcare services health education community awareness"
        )

    if flags["governance"]:
        extra.append(
            "BURO Bangladesh Governance Director Profile Founder Executive Director Zakir Hossain "
            "governing body management team leadership chairman board"
        )

    if flags["reports"]:
        extra.append("BURO Bangladesh reports annual reports publications download PDF")

    if flags["about"] and not (flags["services"] or flags["governance"]):
        extra.append("BURO Bangladesh About BURO microfinance institution vision mission philosophy values")

    if not extra:
        return q

    return (q + " " + " ".join(extra)).strip()


def rerank_hits(query: str, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    flags = _intent_flags(query)

    def score(hit: Dict[str, Any]) -> float:
        url = (hit.get("url") or hit.get("source_url") or "").lower()
        title = (hit.get("title") or "").lower()
        text = (hit.get("text") or "").lower()
        data = f"{url} {title} {text}"

        s = float(hit.get("score") or 0)
        s += float(hit.get("priority") or 0) / 1000

        # General services/programs should prefer /programs and concrete product pages.
        if flags["services"]:
            if "/programs" in url:
                s += 1.00
            if "our services" in text:
                s += 0.50
            if "programs" in title or "programs" in url:
                s += 0.45
            if any(x in text for x in ["loan", "savings", "remittance", "individual loan"]):
                s += 0.25
            if "/about-buro" in url and not flags["about"]:
                s -= 0.35
            if "/governance" in url and not flags["governance"]:
                s -= 0.50

        # Loan questions must prefer real loan/program pages over About/Remittance/installment pages.
        if flags["loan"]:
            if "/programs" in url:
                s += 1.00
            if any(path in url for path in [
                "/general-loans", "/agricultural-loans", "/sme-loans", "/emergency-loans",
                "/disaster-loans", "/water-sanitation-loans", "/consumer-loans",
            ]):
                s += 1.40
            if "loan" in title:
                s += 0.45
            if "provides tailored loans" in text:
                s += 0.55
            if any(x in text for x in [
                "agricultural loans", "sme loans", "general loans", "emergency loans",
                "disaster loans", "water& sanitation loans", "water & sanitation loans",
                "consumer loans",
            ]):
                s += 0.45
            if "/about-buro" in url:
                s -= 0.60
            if "/governance" in url:
                s -= 0.60
            if "/remittance" in url or "remittance" in title:
                s -= 0.45
            if "/buro-loan-installment" in url:
                s -= 0.35

        if flags["savings"]:
            if any(path in url for path in ["/general-savings", "/contractual-savings", "/voluntary-savings"]):
                s += 1.30
            if "/programs" in url:
                s += 0.75
            if "savings" in title:
                s += 0.45
            if "savings" in text:
                s += 0.35
            if "/about-buro" in url or "/governance" in url:
                s -= 0.40

        if flags["remittance"]:
            if "/remittance" in url or "/buro-remittance-service" in url:
                s += 1.30
            if "remittance" in title:
                s += 0.50
            if "remittance" in text:
                s += 0.40
            if "/about-buro" in url or "/governance" in url:
                s -= 0.40

        if flags["about"] and not (flags["loan"] or flags["savings"] or flags["remittance"] or flags["governance"] or flags["healthcare"]):
            if "/about-buro" in url:
                s += 0.90
            if url.rstrip("/") == "https://www.burobd.org" or "home | buro" in title:
                s += 0.25
            if "/governance" in url and not flags["governance"]:
                s -= 0.15

        if flags["governance"]:
            if "/governance" in url:
                s += 0.85
            if "founder executive director" in text:
                s += 0.55
            if "zakir hossain" in text:
                s += 0.45

        if flags["healthcare"]:
            if "/development-projects/buro-healthcare-foundation" in url:
                s += 0.80
            if "/development-projects/digital-healthcare-services" in url:
                s += 0.75
            if "healthcare" in text or "health care" in text:
                s += 0.35

        if flags["reports"]:
            if "/buro-reports" in url:
                s += 1.10
            if "annual report" in text or "reports" in title:
                s += 0.40

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



def _unique_source_urls(sources: List[Dict[str, Any]], limit: int = 3) -> List[Tuple[str, str]]:
    urls: List[Tuple[str, str]] = []
    seen = set()

    for src in sources:
        url = (src.get("url") or "").strip()
        title = (src.get("title") or "BURO website").strip()
        if not url or url in seen:
            continue
        if not allowed_url(url):
            continue
        seen.add(url)
        urls.append((url, title))
        if len(urls) >= limit:
            break

    return urls


def _forced_live_urls_for_query(query: str) -> List[Tuple[str, str]]:
    """
    Add known high-value BURO pages for categories where vector search may
    return a related chunk but not the best detail page.
    """
    flags = _intent_flags(query)
    urls: List[Tuple[str, str]] = []

    if flags.get("governance"):
        urls.append(("https://www.burobd.org/governance", "Governance"))

    if flags.get("services"):
        urls.append(("https://www.burobd.org/programs", "Programs"))

    if flags.get("loan"):
        urls.extend([
            ("https://www.burobd.org/programs", "Programs"),
            ("https://www.burobd.org/general-loans", "General Loans"),
            ("https://www.burobd.org/agricultural-loans", "Agricultural Loans"),
            ("https://www.burobd.org/sme-loans", "SME Loans"),
            ("https://www.burobd.org/emergency-loans", "Emergency Loans"),
            ("https://www.burobd.org/disaster-loans", "Disaster Loans"),
            ("https://www.burobd.org/water-sanitation-loans", "Water & Sanitation Loans"),
            ("https://www.burobd.org/consumer-loans", "Consumer Loans"),
        ])

    if flags.get("savings"):
        urls.extend([
            ("https://www.burobd.org/programs", "Programs"),
            ("https://www.burobd.org/general-savings", "General Savings"),
            ("https://www.burobd.org/contractual-savings", "Contractual Savings"),
            ("https://www.burobd.org/voluntary-savings", "Voluntary Savings"),
        ])

    if flags.get("remittance"):
        urls.extend([
            ("https://www.burobd.org/remittance", "Remittance"),
            ("https://www.burobd.org/buro-remittance-service", "BURO Remittance Service"),
        ])

    if flags.get("healthcare"):
        urls.extend([
            ("https://www.burobd.org/development-projects/digital-healthcare-services", "Digital Healthcare Services"),
            ("https://www.burobd.org/development-projects/buro-healthcare-foundation", "BURO Healthcare Foundation"),
        ])

    if flags.get("about") and not (flags.get("services") or flags.get("governance")):
        urls.append(("https://www.burobd.org/about-buro", "About BURO"))

    # Deduplicate preserving order.
    unique: List[Tuple[str, str]] = []
    seen = set()
    for url, title in urls:
        if url in seen:
            continue
        if allowed_url(url):
            unique.append((url, title))
            seen.add(url)
    return unique


def should_live_expand(query: str, params: Optional[Dict[str, Any]] = None) -> bool:
    """
    Decide whether to fetch the source URLs live after vector search.

    Default: enabled for detailed/profile/service categories, because those
    answers often need more than the single Qdrant chunk.
    """
    params = params or {}

    if str(params.get("live_expand", "")).lower() in ("0", "false", "no", "off"):
        return False

    if not WEBSITE_LIVE_EXPAND_ENABLED:
        return False

    flags = _intent_flags(query)

    if any(flags.get(k) for k in ["governance", "services", "loan", "savings", "remittance", "healthcare", "reports"]):
        return True

    q = (query or "").lower()
    return any(x in q for x in ["details", "profile", "full", "more info", "more information", "বিস্তারিত", "প্রোফাইল"])


def fetch_live_page_text(url: str) -> Tuple[str, str]:
    """Fetch one BURO website page live and extract readable text."""
    if not allowed_url(url):
        return "", ""

    session = requests.Session()
    session.headers.update({"User-Agent": "BURO-RAG-Live-Expand/1.0"})
    resp = session.get(url, timeout=WEBSITE_LIVE_EXPAND_TIMEOUT)
    resp.raise_for_status()
    title, text = extract_page_text(resp.text)
    text = (text or "").strip()
    if len(text) > WEBSITE_LIVE_EXPAND_MAX_CHARS_PER_PAGE:
        text = text[:WEBSITE_LIVE_EXPAND_MAX_CHARS_PER_PAGE]
    return title, text


def _score_live_chunk(query: str, title: str, url: str, text: str) -> float:
    flags = _intent_flags(query)
    q = (query or "").lower()
    data = f"{title} {url} {text}".lower()
    score = 0.0

    # Generic query-term overlap.
    for token in re.findall(r"[a-zA-Z0-9]+|[\u0980-\u09FF]+", q):
        token = token.lower().strip()
        if len(token) >= 3 and token in data:
            score += 0.05

    if flags.get("governance"):
        if "governance" in data:
            score += 1.00
        if "founder executive director" in data:
            score += 1.00
        if "zakir hossain" in data:
            score += 0.80
        if "management team" in data or "governing body" in data:
            score += 0.45

    if flags.get("services"):
        if "programs" in data or "our services" in data:
            score += 0.70
        if "loan" in data or "savings" in data or "remittance" in data:
            score += 0.45

    if flags.get("loan"):
        for term in ["agricultural loans", "sme loans", "general loans", "emergency loans", "disaster loans", "water", "sanitation", "consumer loans"]:
            if term in data:
                score += 0.25

    if flags.get("savings"):
        for term in ["general savings", "contractual savings", "voluntary savings", "secure savings"]:
            if term in data:
                score += 0.30

    if flags.get("remittance") and "remittance" in data:
        score += 0.80

    if flags.get("healthcare") and ("healthcare" in data or "health care" in data):
        score += 0.70

    return score


def _select_live_chunks(query: str, url: str, title: str, text: str, max_chunks: int = 3) -> List[Dict[str, Any]]:
    if not text.strip():
        return []

    raw_chunks = chunk_text(text, size=1400, overlap=180)
    scored: List[Tuple[float, int, str]] = []
    for idx, chunk in enumerate(raw_chunks):
        if not chunk.strip():
            continue
        scored.append((_score_live_chunk(query, title, url, chunk), idx, chunk))

    if not scored:
        return []

    # Highest score first; keep original order as a tie-breaker.
    scored.sort(key=lambda x: (-x[0], x[1]))
    selected = scored[:max_chunks]
    selected.sort(key=lambda x: x[1])

    rows: List[Dict[str, Any]] = []
    for rank, (score, idx, chunk) in enumerate(selected, start=1):
        rows.append({
            "score": 1.5 + float(score),
            "title": title or "BURO Bangladesh Website",
            "url": url,
            "source_url": url,
            "text": chunk,
            "priority": 300,
            "live_fetched": True,
            "chunk_index": idx,
            "live_rank": rank,
        })
    return rows


def _dedupe_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    final: List[Dict[str, Any]] = []
    for c in chunks:
        url = c.get("url") or c.get("source_url") or ""
        text = re.sub(r"\s+", " ", (c.get("text") or "")).strip()
        key = (url, text[:220])
        if not text or key in seen:
            continue
        seen.add(key)
        final.append(c)
    return final


def expand_chunks_from_live_sources(
    query: str,
    chunks: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Qdrant-first, live-link expansion second.

    It fetches the top source URLs and known high-value URLs for the query,
    extracts page text live, and adds the best live chunks to the context.
    """
    params = params or {}
    if not should_live_expand(query, params=params):
        return chunks, []

    sources = build_sources(chunks)
    top_n = max(1, min(int(params.get("live_top_n") or WEBSITE_LIVE_EXPAND_TOP_N), 5))

    candidate_urls = _forced_live_urls_for_query(query) + _unique_source_urls(sources, limit=top_n)

    # Deduplicate URL list.
    deduped_urls: List[Tuple[str, str]] = []
    seen_urls = set()
    for url, title in candidate_urls:
        if not url or url in seen_urls:
            continue
        if not allowed_url(url):
            continue
        seen_urls.add(url)
        deduped_urls.append((url, title))
        if len(deduped_urls) >= max(top_n, len(_forced_live_urls_for_query(query))):
            # Forced URLs may be more than top_n for loan details; keep them all up to a safe cap.
            if len(deduped_urls) >= 10:
                break

    live_chunks: List[Dict[str, Any]] = []
    live_sources: List[Dict[str, Any]] = []

    for url, fallback_title in deduped_urls[:10]:
        try:
            title, text = fetch_live_page_text(url)
            title = title or fallback_title or "BURO Bangladesh Website"
            if not text or len(text) < 80:
                continue
            selected = _select_live_chunks(query, url, title, text, max_chunks=3)
            if selected:
                live_chunks.extend(selected)
                live_sources.append({"title": title, "url": url, "live": True})
        except Exception as exc:
            live_sources.append({"title": fallback_title or "BURO website", "url": url, "live": False, "error": str(exc)[:160]})
            continue

    if not live_chunks:
        return chunks, live_sources

    combined = _dedupe_chunks(live_chunks + chunks)
    combined = rerank_hits(query, combined)
    return combined[:18], live_sources

def executive_director_guard(query: str, context: str, sources: List[Dict[str, Any]], language: str = "en") -> Optional[str]:
    q = query.lower()
    c = context.lower()

    if any(x in q for x in [
        "executive director", "founder executive director", "who is executive director",
        "director profile", "director", "leadership", "zakir hossain",
        "নির্বাহী পরিচালক", "পরিচালক", "ডিরেক্টর", "ডিরেক্টর প্রোফাইল",
    ]):
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


def _clean_context_for_language(text: str, language: str) -> str:
    """Remove script-mixed lines from fallback extractive answers."""
    lines = []
    for raw in (text or "").splitlines():
        line = " ".join(raw.strip().split())
        if not line:
            continue
        if language == "en" and (contains_bangla_script(line) or contains_devanagari_script(line)):
            continue
        if language == "bn" and contains_devanagari_script(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _short_sentences(text: str, max_sentences: int = 5, max_chars: int = 900) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return ""

    pieces = re.split(r"(?<=[.!?।])\s+", text)
    selected = []
    total = 0

    for piece in pieces:
        piece = piece.strip()
        if len(piece) < 20:
            continue
        if total + len(piece) > max_chars:
            break
        selected.append(piece)
        total += len(piece)
        if len(selected) >= max_sentences:
            break

    if not selected:
        return text[:max_chars].strip()

    return " ".join(selected).strip()


def _looks_like_about_buro_query(query: str) -> bool:
    flags = _intent_flags(query)
    return flags["about"] and not (flags["loan"] or flags["savings"] or flags["remittance"] or flags["healthcare"])


def _looks_like_empty_or_not_found(answer: str) -> bool:
    a = (answer or "").strip().lower()
    if not a:
        return True

    not_found_markers = [
        "not found",
        "not available",
        "could not find",
        "couldn't find",
        "no relevant information",
        "specific answer was not found",
        "প্রদত্ত",
        "পাওয়া যায়নি",
        "পাওয়া যায়নি",
        "খুঁজে পাওয়া যায়নি",
    ]
    return any(x in a for x in not_found_markers)


def _remove_non_english_scripts(text: str) -> str:
    """Keep English-readable text and remove Bangla/Devanagari script fragments."""
    text = re.sub(r"[\u0980-\u09FF]+", " ", text or "")
    text = re.sub(r"[\u0900-\u097F]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _english_context_sentences(chunks: List[Dict[str, Any]], max_sentences: int = 7) -> List[str]:
    """Extract English-looking sentences from retrieved chunks."""
    candidates: List[str] = []

    for item in chunks[:8]:
        text = item.get("text") or ""
        text = _remove_non_english_scripts(text)
        text = re.sub(r"\s+", " ", text).strip()

        if not text:
            continue

        for sentence in re.split(r"(?<=[.!?])\s+", text):
            s = sentence.strip(" -•\t\n")
            if len(s) < 35:
                continue
            if contains_bangla_script(s) or contains_devanagari_script(s):
                continue

            low = s.lower()
            if any(bad.lower() in low for bad in BAD_PHRASES):
                continue

            if any(key in low for key in [
                "buro", "microfinance", "financial", "organization", "mission", "vision",
                "development", "services", "program", "loan", "savings", "remittance",
                "bangladesh", "clients", "communities",
            ]):
                candidates.append(s)

            if len(candidates) >= max_sentences:
                return candidates

    return candidates


def _source_lines(sources: List[Dict[str, Any]], max_sources: int = 5, language: str = "en") -> str:
    lines = []
    seen = set()
    for src in sources[:max_sources]:
        url = src.get("url") or ""
        title = src.get("title") or "BURO website"
        if not url or url in seen:
            continue
        seen.add(url)
        lines.append(f"- {title}: {url}")

    if not lines:
        return ""

    return ("উৎস:\n" if language == "bn" else "Sources:\n") + "\n".join(lines)


def _evidence_text(chunks: List[Dict[str, Any]]) -> str:
    return "\n".join((c.get("text") or "") for c in chunks)


def _find_terms(evidence: str, known_terms: List[Tuple[str, List[str]]]) -> List[str]:
    e = (evidence or "").lower()
    found: List[str] = []
    for display, keys in known_terms:
        if any(k.lower() in e for k in keys):
            if display not in found:
                found.append(display)
    return found


KNOWN_LOANS: List[Tuple[str, List[str]]] = [
    ("Agricultural Loans", ["agricultural loans", "agricultural loan"]),
    ("SME Loans", ["sme loans", "sme loan"]),
    ("General Loans", ["general loans", "general loan"]),
    ("Emergency Loans", ["emergency loans", "emergency loan"]),
    ("Disaster Loans", ["disaster loans", "disaster loan"]),
    ("Water & Sanitation Loans", ["water& sanitation loans", "water & sanitation loans", "water sanitation loans", "wash financing"]),
    ("Consumer Loans", ["consumer loans", "consumer loan"]),
]

KNOWN_SAVINGS: List[Tuple[str, List[str]]] = [
    ("General Savings", ["general savings", "general saving"]),
    ("Contractual Savings", ["contractual savings", "contractual saving"]),
    ("Voluntary Savings", ["voluntary savings", "voluntary saving"]),
]

KNOWN_OTHER_SERVICES: List[Tuple[str, List[str]]] = [
    ("Remittance", ["remittance"]),
    ("Individual Loan", ["individual loan", "personal loan"]),
    ("Digital Healthcare Services", ["digital healthcare services"]),
    ("Basic Health Education Program", ["basic health education program", "bhep"]),
    ("Health Education and Community Awareness", ["health education and community awareness"]),
    ("BURO Healthcare Foundation", ["buro healthcare foundation"]),
    ("Digital Financial Services", ["digital financial services"]),
]


def _terms_from_titles_and_urls(chunks: List[Dict[str, Any]], existing: List[str]) -> List[str]:
    found = list(existing)
    url_map = [
        ("Agricultural Loans", ["agricultural"]),
        ("SME Loans", ["sme"]),
        ("General Loans", ["general-loans"]),
        ("Emergency Loans", ["emergency"]),
        ("Disaster Loans", ["disaster"]),
        ("Water & Sanitation Loans", ["water", "sanitation"]),
        ("Consumer Loans", ["consumer"]),
        ("General Savings", ["general-savings"]),
        ("Contractual Savings", ["contractual-savings"]),
        ("Voluntary Savings", ["voluntary-savings"]),
        ("Remittance", ["remittance"]),
        ("Digital Healthcare Services", ["digital-healthcare-services"]),
    ]

    for c in chunks:
        url = (c.get("url") or c.get("source_url") or "").lower()
        title = (c.get("title") or "").lower()
        data = f"{url} {title}"
        for display, keys in url_map:
            if display in found:
                continue
            if all(k in data for k in keys):
                found.append(display)

    return found


def deterministic_services_answer(
    query: str,
    chunks: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    language: str = "en",
) -> str:
    """
    Deterministic answer for BURO service/program questions using only retrieved chunks.
    This prevents wrong or weak LLM summaries when Qdrant already has the facts.
    """
    flags = _intent_flags(query)
    evidence = _evidence_text(chunks)

    loans = _terms_from_titles_and_urls(chunks, _find_terms(evidence, KNOWN_LOANS))
    savings = _terms_from_titles_and_urls(chunks, _find_terms(evidence, KNOWN_SAVINGS))
    other_services = _terms_from_titles_and_urls(chunks, _find_terms(evidence, KNOWN_OTHER_SERVICES))

    source_block = _source_lines(sources, language=language)

    if language == "bn":
        parts = []
        if flags["loan"] or (flags["services"] and loans):
            if loans:
                parts.append("ঋণসেবা: " + ", ".join(loans) + "।")
            else:
                parts.append("ঋণসেবা: BURO Bangladesh গ্রাহক ও কমিউনিটির জন্য বিভিন্ন ঋণসেবা প্রদান করে।")
        if flags["savings"] or (flags["services"] and savings):
            if savings:
                parts.append("সঞ্চয় সেবা: " + ", ".join(savings) + "।")
        if flags["remittance"] or (flags["services"] and "Remittance" in other_services):
            parts.append("রেমিট্যান্স: BURO Bangladesh প্রবাসীদের পাঠানো অর্থ স্থানীয় সুবিধাভোগীদের কাছে পৌঁছে দিতে remittance service প্রদান করে।")
        if flags["healthcare"] or (flags["services"] and any("Healthcare" in s or "Health" in s for s in other_services)):
            health_terms = [s for s in other_services if "Health" in s or "Healthcare" in s]
            if health_terms:
                parts.append("স্বাস্থ্য/উন্নয়নমূলক সেবা: " + ", ".join(health_terms) + "।")
        if not parts:
            parts.append("প্রদত্ত BURO website context অনুযায়ী, BURO Bangladesh ঋণ, সঞ্চয়, রেমিট্যান্স এবং উন্নয়নমূলক বিভিন্ন সেবা প্রদান করে।")
        answer = "প্রদত্ত BURO website context অনুযায়ী, " + " ".join(parts)
        if source_block:
            answer += "\n\n" + source_block
        return answer.strip()

    # English answer
    if flags["loan"] and not (flags["savings"] or flags["remittance"]):
        if loans:
            answer = (
                "Based on the retrieved BURO website context, BURO Bangladesh provides several loan services, including "
                + ", ".join(loans)
                + ". These loan services are described as accessible financial solutions for clients and communities."
            )
        else:
            answer = (
                "Based on the retrieved BURO website context, BURO Bangladesh provides loan services designed to support "
                "clients and communities through accessible financial solutions."
            )
    elif flags["savings"] and not flags["loan"]:
        if savings:
            answer = (
                "Based on the retrieved BURO website context, BURO Bangladesh provides savings services including "
                + ", ".join(savings)
                + ". These services are presented as secure savings options for clients' future stability."
            )
        else:
            answer = "Based on the retrieved BURO website context, BURO Bangladesh provides savings services for its clients."
    elif flags["remittance"] and not flags["loan"]:
        answer = (
            "Based on the retrieved BURO website context, BURO Bangladesh provides remittance services that help deliver "
            "money sent by Bangladeshi expatriates abroad to local beneficiaries through its branch network."
        )
    else:
        sections = []
        if loans:
            sections.append("Loan services: " + ", ".join(loans) + ".")
        if savings:
            sections.append("Savings services: " + ", ".join(savings) + ".")
        if "Remittance" in other_services:
            sections.append("Remittance services for transferring foreign earnings to local beneficiaries.")
        other = [s for s in other_services if s != "Remittance"]
        if other:
            sections.append("Other programs/services include: " + ", ".join(other) + ".")
        if not sections:
            sections.append(
                "BURO Bangladesh provides services and programs including loans, savings, remittance, and development-oriented services."
            )
        answer = "Based on the retrieved BURO website context, " + " ".join(sections)

    if source_block:
        answer += "\n\n" + source_block
    return answer.strip()


def deterministic_about_answer(
    query: str,
    chunks: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    language: str = "en",
) -> str:
    evidence = _evidence_text(chunks)
    source_block = _source_lines(sources, language=language)

    if language == "bn":
        answer = (
            "প্রদত্ত BURO website context অনুযায়ী, BURO Bangladesh বাংলাদেশের একটি microfinance institution। "
            "এটি inclusive এবং sustainable financial services-এর মাধ্যমে ব্যক্তি ও কমিউনিটিকে ক্ষমতায়নের ওপর গুরুত্ব দেয়। "
            "ওয়েবসাইটে BURO Bangladesh-এর vision হিসেবে বাংলাদেশের মানুষের মৌলিক চাহিদা পূরণকারী সুখী, সমৃদ্ধ ও pluralistic democratic society-এর কথা বলা হয়েছে।"
        )
        if source_block:
            answer += "\n\n" + source_block
        return answer.strip()

    # Prefer extractive English sentences if available.
    sentences = _english_context_sentences(chunks, max_sentences=5)
    if sentences:
        summary = " ".join(sentences[:4])
    else:
        summary = (
            "BURO Bangladesh is described on its website as one of the first microfinance institutions to commit to "
            "financial sustainability through commercial capital. Its website highlights inclusive and sustainable "
            "financial services, dignity, respect, trust, transparency, and community-focused development."
        )

    answer = "Based on the retrieved BURO website context:\n\n" + summary
    if source_block:
        answer += "\n\n" + source_block
    return answer.strip()



def _governance_profile_evidence(chunks: List[Dict[str, Any]], max_items: int = 5) -> List[str]:
    """Extract concise public leadership/profile lines from retrieved/live governance context."""
    evidence = _evidence_text(chunks)
    lines: List[str] = []

    # Work line-by-line first because the governance page often uses short profile rows.
    raw_lines = []
    for raw in evidence.splitlines():
        line = " ".join(raw.strip().split())
        if len(line) < 3:
            continue
        raw_lines.append(line)

    keywords = [
        "zakir hossain", "founder executive director", "executive director",
        "governing body", "management team", "chairman", "director",
        "নির্বাহী পরিচালক", "পরিচালক", "চেয়ারম্যান", "চেয়ারম্যান",
    ]

    for idx, line in enumerate(raw_lines):
        low = line.lower()
        if any(k in low for k in keywords):
            # Include nearby short lines if they look like a name/title pair.
            window = raw_lines[max(0, idx - 1): min(len(raw_lines), idx + 2)]
            combined = " | ".join(window)
            if combined not in lines:
                lines.append(combined)
        if len(lines) >= max_items:
            break

    if lines:
        return lines[:max_items]

    # Fallback sentence extraction.
    for sentence in re.split(r"(?<=[.!?।])\s+", re.sub(r"\s+", " ", evidence)):
        s = sentence.strip()
        low = s.lower()
        if len(s) >= 20 and any(k in low for k in keywords):
            if s not in lines:
                lines.append(s)
        if len(lines) >= max_items:
            break

    return lines[:max_items]


def deterministic_governance_answer(
    query: str,
    chunks: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    language: str = "en",
) -> str:
    """
    Deterministic answer for director/profile/governance questions.
    Uses retrieved Qdrant chunks plus optional live-expanded governance context.
    Does not invent private profile details.
    """
    evidence = _evidence_text(chunks)
    e = (evidence or "").lower()
    source_block = _source_lines(sources, language=language)
    profile_lines = _governance_profile_evidence(chunks, max_items=5)

    has_zakir = "zakir hossain" in e
    has_founder_ed = "founder executive director" in e
    has_governance = "governance" in e or any("governance" in (src.get("url") or "").lower() for src in sources)
    has_management = "management team" in e or "governing body" in e

    if language == "bn":
        if profile_lines:
            answer = "প্রদত্ত BURO website context অনুযায়ী director/profile সম্পর্কিত পাওয়া তথ্য:\n"
            for line in profile_lines:
                answer += f"- {line}\n"
        elif has_zakir and has_founder_ed:
            answer = (
                "প্রদত্ত BURO website context অনুযায়ী, BURO Bangladesh-এর Governance page-এ "
                "Zakir Hossain-কে BURO Bangladesh-এর Founder Executive Director হিসেবে উল্লেখ করা হয়েছে।"
            )
        elif has_governance:
            answer = (
                "প্রদত্ত BURO website context অনুযায়ী, director/profile বা governance সম্পর্কিত তথ্যের জন্য "
                "BURO Bangladesh-এর Governance page প্রাসঙ্গিক উৎস হিসেবে পাওয়া গেছে।"
            )
        else:
            answer = _empty_context_answer("bn")

        if has_management:
            answer += "\nএকই context-এ governing body/management team সম্পর্কিত তথ্যও পাওয়া যায়।"

        answer += "\nব্যক্তিগত তথ্য যেমন পরিবার, ব্যক্তিগত ফোন, ঠিকানা বা জন্মতারিখ context-এ স্পষ্ট না থাকলে আমি তা অনুমান করব না।"
        if source_block:
            answer += "\n\n" + source_block
        return answer.strip()

    if profile_lines:
        answer = "Based on the retrieved BURO website context, the director/profile information found is:\n"
        for line in profile_lines:
            answer += f"- {line}\n"
    elif has_zakir and has_founder_ed:
        answer = (
            "According to the retrieved BURO Bangladesh Governance context, Zakir Hossain is listed as "
            "the Founder Executive Director of BURO Bangladesh."
        )
    elif has_governance:
        answer = (
            "The retrieved BURO website context points to the Governance page for director/profile or "
            "leadership-related information."
        )
    else:
        answer = _empty_context_answer("en")

    if has_management:
        answer += "\nThe same context also refers to governing body or management team information."

    answer += "\nI will not infer private personal details unless they are explicitly available in the official website context."

    if source_block:
        answer += "\n\n" + source_block
    return answer.strip()

def extractive_fallback_answer(
    query: str,
    chunks: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    language: str = "en",
) -> str:
    """
    Last-resort grounded answer when the LLM violates language instructions
    or wrongly says information was not found despite Qdrant hits.
    """
    flags = _intent_flags(query)

    if flags["services"] or flags["loan"] or flags["savings"] or flags["remittance"]:
        return deterministic_services_answer(query, chunks, sources, language)

    if flags["governance"]:
        return deterministic_governance_answer(query, chunks, sources, language)

    if _looks_like_about_buro_query(query):
        return deterministic_about_answer(query, chunks, sources, language)

    if language == "bn":
        return _empty_context_answer("bn")

    sentences = _english_context_sentences(chunks, max_sentences=6)

    if sentences:
        summary_text = " ".join(sentences[:5])
    else:
        titles = []
        for src in sources[:3]:
            title = src.get("title") or "BURO website"
            if title and title not in titles:
                titles.append(title)

        if titles:
            summary_text = (
                "Relevant BURO Bangladesh website pages were found, including "
                + ", ".join(titles)
                + ". Please check the listed source URLs for the full official context."
            )
        else:
            return _empty_context_answer("en")

    answer = "Based on the retrieved BURO website context:\n\n" + summary_text
    source_block = _source_lines(sources, language=language)
    if source_block:
        answer += "\n\n" + source_block
    return answer.strip()


def generate_answer(
    query: str,
    chunks: List[Dict[str, Any]],
    language: str = "en",
    *,
    num_predict: Optional[int] = None,
) -> str:
    language = normalize_language(query=query, language=language)
    context = build_context(chunks)
    sources = build_sources(chunks)
    flags = _intent_flags(query)

    guarded = executive_director_guard(query, context, sources, language)
    if guarded:
        return guarded

    guarded = healthcare_founder_guard(query, context, sources, language)
    if guarded:
        return guarded

    if not context.strip():
        return _empty_context_answer(language)

    # Deterministic answers for common website categories. These are safer and
    # faster than LLM generation because the facts are directly in Qdrant chunks.
    if flags["services"] or flags["loan"] or flags["savings"] or flags["remittance"]:
        return deterministic_services_answer(query, chunks, sources, language)

    if flags["governance"]:
        return deterministic_governance_answer(query, chunks, sources, language)

    if _looks_like_about_buro_query(query):
        return deterministic_about_answer(query, chunks, sources, language)

    language_rule = _language_instruction(language)

    if language == "bn":
        prompt = f"""
আপনি BURO Bangladesh-এর অফিসিয়াল ওয়েবসাইট RAG সহকারী।

অত্যন্ত গুরুত্বপূর্ণ ভাষা নির্দেশনা:
- {language_rule}
- সম্পূর্ণ উত্তর প্রমিত বাংলায় দিন।
- কোনো হিন্দি/দেবনাগরী অক্ষর ব্যবহার করবেন না।
- BURO Bangladesh-এর মতো অফিসিয়াল নাম ইংরেজিতে রাখা যাবে।

নিয়ম:
১. শুধু নিচের Context থেকে উত্তর দিন।
২. Context-এ তথ্য না থাকলে ঠিক এই বাক্যটি বলুন: "{_empty_context_answer("bn")}"
৩. Founder, Executive Director, Chairman, ownership বা establishment সম্পর্কিত তথ্য context-এ স্পষ্ট না থাকলে অনুমান করবেন না।
৪. menu, footer, address, contact number, copyright ও repeated navigation text উপেক্ষা করুন।
৫. উত্তর সংক্ষিপ্ত, পেশাদার এবং তথ্যভিত্তিক রাখুন।
৬. শেষে "উৎস:" শিরোনামে প্রাসঙ্গিক URL দিন।

প্রশ্ন:
{query}

Context:
{context}

বাংলা উত্তর:
""".strip()

        retry_prompt = f"""
নিচের উত্তরটি ভুল ভাষা/হিন্দি/দেবনাগরী মিশ্রিত হতে পারে। এটিকে শুধুমাত্র প্রমিত বাংলায় পুনরায় লিখুন।
শুধু Context-এর তথ্য ব্যবহার করুন। কোনো নতুন তথ্য যোগ করবেন না।
BURO Bangladesh নাম ইংরেজিতে রাখুন।

প্রশ্ন:
{query}

Context:
{context}

শুদ্ধ বাংলা উত্তর:
""".strip()

    else:
        prompt = f"""
You are the official BURO Bangladesh website RAG assistant.

Critical language instruction:
- {language_rule}
- The user asked in English, so the entire answer must be English only.
- Never use Bangla script.
- Never use Hindi or Devanagari script.
- Keep official names such as "BURO Bangladesh" unchanged.

Rules:
1. Answer only from the provided Context.
2. If the answer is not available in the Context, say: "{_empty_context_answer("en")}"
3. Do not infer Founder, Executive Director, Chairman, ownership, or establishment information unless the Context explicitly says it.
4. Ignore menu, footer, address, contact number, copyright, and repeated navigation text.
5. Keep the answer professional, concise, and factual.
6. Add "Sources:" at the end with relevant URLs.

Question:
{query}

Context:
{context}

English answer:
""".strip()

        retry_prompt = f"""
Rewrite the answer using English only.

Strict rules:
- English only.
- No Bangla script.
- No Hindi.
- No Devanagari script.
- Use only the Context.
- Do not add new facts.
- Keep BURO Bangladesh unchanged.

Question:
{query}

Context:
{context}

Final English answer:
""".strip()

    answer = generate_llm(
        prompt,
        language=language,
        num_predict=num_predict,
        retry_prompt=retry_prompt,
    )

    if _needs_language_retry(answer, language) or (_looks_like_empty_or_not_found(answer) and chunks):
        return extractive_fallback_answer(query, chunks, sources, language)

    return answer

def run(user=None, query: str = "", params=None, language: str = "en", **kwargs) -> Dict[str, Any]:
    try:
        params = params or {}
        language = normalize_language(query=query, language=language, params=params)

        # Optional: allow privileged caller to run sync through tool layer
        if params.get("action") == "sync":
            max_pages = int(params.get("max_pages", 80))
            recreate = bool(params.get("recreate", True))
            result = sync_buro_website(max_pages=max_pages, recreate_collection=recreate)
            return {
                "ok": True,
                "source": QDRANT_COLLECTION,
                "source_collection": QDRANT_COLLECTION,
                "action": "sync",
                "result": result,
                "answer": (
                    "BURO ওয়েবসাইট clean sync সম্পন্ন হয়েছে।"
                    if language == "bn"
                    else "BURO website clean sync completed."
                ),
                "sources": [],
                "matches_count": 0,
                "language": language,
                "model": select_generation_model(language),
            }

        try:
            limit = int(params.get("limit", 8))
        except Exception:
            limit = 8

        limit = max(1, min(limit, 10))

        try:
            num_predict = int(params.get("num_predict", GEN_NUM_PREDICT))
        except Exception:
            num_predict = GEN_NUM_PREDICT

        # If caller asks fast mode, keep output shorter but still grounded.
        if bool(params.get("fast", False)):
            num_predict = min(num_predict, 300)

        search_query = expand_search_query(query)

        # For broad service/product questions, use a slightly larger retrieval window.
        # This helps bring Programs, Loan, Savings, and Remittance pages into context.
        flags = _intent_flags(query)
        effective_limit = limit
        if flags.get("services") or flags.get("loan") or flags.get("savings") or flags.get("remittance") or flags.get("governance"):
            effective_limit = max(limit, 8)

        result = search_qdrant(search_query, limit=effective_limit)
        chunks = normalize_hits(query, result)

        live_sources: List[Dict[str, Any]] = []
        if chunks:
            chunks, live_sources = expand_chunks_from_live_sources(query, chunks, params=params)

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
                "source_collection": QDRANT_COLLECTION,
                "answer": final_answer,
                "sources": [],
                "matches": [],
                "matches_count": 0,
                "language": language,
                "model": select_generation_model(language),
            }

        final_answer = generate_answer(
            query,
            chunks,
            language=language,
            num_predict=num_predict,
        )

        return {
            "ok": True,
            "source": QDRANT_COLLECTION,
            "source_collection": QDRANT_COLLECTION,
            "answer": final_answer,
            "sources": sources,
            "matches_count": len(chunks),
            "language": language,
            "model": select_generation_model(language),
            "search_query": search_query,
            "live_expanded": bool(live_sources),
            "live_sources": live_sources,
        }

    except Exception as exc:
        language = normalize_language(query=query, language=language, params=params or {})
        return {
            "ok": False,
            "source": QDRANT_COLLECTION,
            "source_collection": QDRANT_COLLECTION,
            "error": str(exc),
            "answer": (
                f"Website RAG tool error: {exc}"
                if language != "bn"
                else f"Website RAG tool error: {exc}"
            ),
            "sources": [],
            "matches_count": 0,
            "language": language,
            "model": select_generation_model(language),
        }
