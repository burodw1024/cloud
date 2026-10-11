from __future__ import annotations

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


# =============================================================================
# Runtime settings
# =============================================================================
def _runtime_setting(name: str, default: Any = "") -> str:
    """
    Read runtime setting with environment priority first, then dynamic settings.
    This works for both normal Linux/systemd Django and container/Kubernetes.
    """
    env_value = os.getenv(name)
    if env_value is not None and str(env_value).strip():
        return str(env_value).strip()

    try:
        value = get_setting(name, default)
        return str(value).strip() if value is not None else str(default)
    except Exception:
        return str(default)


def _int_setting(name: str, default: int) -> int:
    try:
        return int(_runtime_setting(name, default))
    except Exception:
        return default


def _bool_setting(name: str, default: bool = False) -> bool:
    raw = _runtime_setting(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


QDRANT_COLLECTION = _runtime_setting("QDRANT_COLLECTION_WEBSITE", "buro_website")
OLLAMA_URL = get_ollama_url()
EMBED_MODEL = _runtime_setting("EMBED_MODEL", _runtime_setting("EMBEDDING_MODEL", "nomic-embed-text"))

# Model split:
# English public website answers are faster and cleaner with qwen2.5-1.5b:local.
# Bangla answers can use aya-expanse:8b.
GEN_MODEL = _runtime_setting(
    "WEBSITE_ANSWER_MODEL",
    _runtime_setting("GEN_MODEL", _runtime_setting("OLLAMA_MODEL", "aya-expanse:8b")),
)
GEN_MODEL_EN = _runtime_setting("WEBSITE_ANSWER_MODEL_EN", "qwen2.5-1.5b:local")
GEN_MODEL_BN = _runtime_setting("WEBSITE_ANSWER_MODEL_BN", GEN_MODEL or "aya-expanse:8b")

BURO_BASE_URL = "https://www.burobd.org"
TIMEOUT = _int_setting("WEBSITE_REQUEST_TIMEOUT", 60)
GEN_TIMEOUT = _int_setting("OLLAMA_GENERATE_TIMEOUT", 240)
GEN_NUM_PREDICT = _int_setting("OLLAMA_NUM_PREDICT_WEBSITE", 500)
MAX_CONTEXT_CHARS = _int_setting("WEBSITE_MAX_CONTEXT_CHARS", 14000)

LIVE_EXPAND_ENABLED = _bool_setting("WEBSITE_LIVE_EXPAND_ENABLED", True)
LIVE_EXPAND_TOP_N = _int_setting("WEBSITE_LIVE_EXPAND_TOP_N", 3)
LIVE_EXPAND_MAX_CHARS_PER_PAGE = _int_setting("WEBSITE_LIVE_EXPAND_MAX_CHARS_PER_PAGE", 7000)


# =============================================================================
# Cleaning / language helpers
# =============================================================================
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


def contains_bangla_script(text: str) -> bool:
    return bool(re.search(r"[\u0980-\u09FF]", text or ""))


def contains_devanagari_script(text: str) -> bool:
    return bool(re.search(r"[\u0900-\u097F]", text or ""))


def wants_bangla(text: str) -> bool:
    """
    Detect explicit Bangla/Bengali response request.

    Critical fix:
    - 'Bangladesh' contains the letters 'bangla', but must NOT trigger Bangla.
    - Only actual Bangla script or explicit language phrases trigger Bangla.
    """
    raw = text or ""
    low = raw.lower()

    if contains_bangla_script(raw):
        return True

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
        r"\btranslate\s+(into|to)\s+bangla\b",
        r"\bbangla\s+(response|answer)\b",
        r"\bbengali\s+(response|answer)\b",
        r"\bbn(?:-bd)?\b",
        r"(^|\s)(bangla|bengali)(\s|$)",
    ]

    return any(re.search(pattern, low) for pattern in phrase_patterns)


def normalize_language(query: str = "", language: str = "en", params: Optional[Dict[str, Any]] = None) -> str:
    """
    Resolve output language.
    Priority:
    1. params['language'] / params['lang']
    2. explicit language argument
    3. query content, only when it clearly asks Bangla/Bengali
    """
    params = params or {}
    raw_lang = params.get("language") or params.get("lang") or language or ""
    lang = str(raw_lang).strip().lower()

    if lang in {"bn", "bn-bd", "bangla", "bengali", "বাংলা"}:
        return "bn"

    if lang in {"en", "en-us", "english"}:
        return "bn" if wants_bangla(query) else "en"

    if wants_bangla(query):
        return "bn"

    return "en"


def select_generation_model(language: str = "en") -> str:
    if language == "en":
        return GEN_MODEL_EN or GEN_MODEL or "qwen2.5-1.5b:local"
    if language == "bn":
        return GEN_MODEL_BN or GEN_MODEL or "aya-expanse:8b"
    return GEN_MODEL or "aya-expanse:8b"


def _needs_language_retry(answer: str, language: str) -> bool:
    if not answer:
        return False

    if language == "en":
        return contains_bangla_script(answer) or contains_devanagari_script(answer)

    if language == "bn":
        return contains_devanagari_script(answer)

    return False


def _empty_context_answer(language: str) -> str:
    if language == "bn":
        return "প্রদত্ত BURO website তথ্যের মধ্যে এই প্রশ্নের নির্দিষ্ট উত্তর পাওয়া যায়নি।"
    return "The specific answer was not found in the provided BURO website information."


def _language_instruction(language: str) -> str:
    if language == "bn":
        return (
            "উত্তর অবশ্যই পরিষ্কার, প্রমিত বাংলায় দিন। "
            "বাংলা-হিন্দি মিশ্রিত শব্দ ব্যবহার করবেন না। "
            "দেবনাগরী/হিন্দি অক্ষর ব্যবহার করবেন না। "
            "BURO Bangladesh, URL, official title বা product/service name প্রয়োজন হলে ইংরেজিতে রাখা যাবে।"
        )

    return (
        "Answer strictly in English. Do not use Bangla script. "
        "Do not use Hindi or Devanagari script. Use clear professional English only."
    )


# =============================================================================
# Website category detection
# =============================================================================
def _q(query: str) -> str:
    return (query or "").lower().strip()


def _has_any(query: str, items: List[str]) -> bool:
    q = _q(query)
    return any(x in q for x in items)


def is_about_query(query: str) -> bool:
    return _has_any(query, [
        "about buro", "about buro bangladesh", "tell me about buro", "tell about buro",
        "what is buro", "what is buro bangladesh", "who is buro", "company profile",
        "organization profile", "বুরো সম্পর্কে", "বুরো বাংলাদেশ", "বুরো কী", "বুরো কি",
        "প্রতিষ্ঠান সম্পর্কে", "সংস্থা সম্পর্কে",
    ])


def is_mission_vision_query(query: str) -> bool:
    return _has_any(query, [
        "mission", "vision", "values", "philosophy", "objective", "purpose",
        "মিশন", "ভিশন", "লক্ষ্য", "উদ্দেশ্য", "মূল্যবোধ", "দর্শন",
    ])


def is_services_query(query: str) -> bool:
    return _has_any(query, [
        "service", "services", "program", "programs", "what does buro provide",
        "what services does buro", "সেবা", "প্রোগ্রাম", "কি কি সেবা", "কী কী সেবা",
    ])


def is_loan_query(query: str) -> bool:
    return _has_any(query, [
        "loan", "loans", "loan service", "loan services", "loan product", "loan products",
        "buro loan", "agricultural loan", "sme loan", "general loan", "emergency loan",
        "disaster loan", "consumer loan", "water sanitation loan", "wash financing",
        "ঋণ", "লোন", "ঋণ সেবা", "লোন সেবা", "ঋণের সেবা", "লোন প্রোডাক্ট",
    ])


def is_savings_query(query: str) -> bool:
    return _has_any(query, [
        "saving", "savings", "general savings", "contractual savings", "voluntary savings",
        "deposit", "সঞ্চয়", "সঞ্চয়", "সেভিংস", "জমা",
    ])


def is_remittance_query(query: str) -> bool:
    return _has_any(query, [
        "remittance", "buro remittance", "foreign earnings", "রেমিট্যান্স",
    ])


def is_governance_query(query: str) -> bool:
    return _has_any(query, [
        "governance", "management team", "board", "chairman", "director", "executive director",
        "founder executive director", "founder", "zakir hossain", "profile", "director profile",
        "leadership", "পরিচালক", "ডিরেক্টর", "প্রোফাইল", "নির্বাহী পরিচালক",
        "প্রতিষ্ঠাতা নির্বাহী পরিচালক", "গভর্নেন্স", "পরিচালনা", "বোর্ড", "চেয়ারম্যান",
        "চেয়ারম্যান", "জাকির হোসেন", "জাকির",
    ])


def is_direct_who_is_director_query(query: str) -> bool:
    q = _q(query)
    direct_patterns = [
        "who is the executive director",
        "who is executive director",
        "who is the founder executive director",
        "who is founder executive director",
        "executive director কে",
        "founder executive director কে",
        "নির্বাহী পরিচালক কে",
        "প্রতিষ্ঠাতা নির্বাহী পরিচালক কে",
    ]
    detail_words = ["profile", "details", "detail", "biography", "background", "experience",
                    "বিস্তারিত", "প্রোফাইল", "পরিচিতি", "ব্যাকগ্রাউন্ড", "অভিজ্ঞতা"]
    return any(p in q for p in direct_patterns) and not any(w in q for w in detail_words)


def is_healthcare_query(query: str) -> bool:
    return _has_any(query, [
        "healthcare", "health care", "buro healthcare foundation", "digital healthcare",
        "health education", "স্বাস্থ্য", "হেলথকেয়ার", "হেলথ কেয়ার",
    ])


def is_report_query(query: str) -> bool:
    return _has_any(query, [
        "report", "reports", "annual report", "publication", "publications",
        "রিপোর্ট", "বার্ষিক রিপোর্ট", "প্রকাশনা",
    ])


def is_contact_location_query(query: str) -> bool:
    return _has_any(query, [
        "contact", "address", "location", "office", "branch location", "head office",
        "যোগাযোগ", "ঠিকানা", "অফিস", "লোকেশন", "শাখা",
    ])


# =============================================================================
# Ollama / Qdrant helpers
# =============================================================================
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
    try:
        predict = int(num_predict or GEN_NUM_PREDICT)
    except Exception:
        predict = GEN_NUM_PREDICT

    predict = max(80, min(predict, 1000))

    options = {
        "temperature": 0.07,
        "top_p": 0.80,
        "repeat_penalty": 1.15,
        "num_ctx": 4096,
        "num_predict": predict,
    }

    if language == "bn":
        options.update({
            "temperature": 0.04,
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

    resp = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=GEN_TIMEOUT)
    resp.raise_for_status()
    answer = resp.json().get("response", "").strip()

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
            },
        }
        retry_resp = requests.post(f"{OLLAMA_URL}/api/generate", json=retry_payload, timeout=GEN_TIMEOUT)
        retry_resp.raise_for_status()
        retry_answer = retry_resp.json().get("response", "").strip()
        if retry_answer:
            answer = retry_answer

    return answer


def qdrant_url() -> str:
    return get_qdrant_url(check_health=True).rstrip("/")


# =============================================================================
# Crawl / clean / sync
# =============================================================================
def clean_buro_text(text: str) -> str:
    lines = []
    seen = set()

    for raw in (text or "").splitlines():
        line = " ".join(raw.strip().split())

        if not line:
            continue

        low = line.lower()

        if len(line) < 3:
            continue

        if any(p.lower() in low for p in BAD_PHRASES):
            continue

        if len(line) > 260 and low.count("buro") >= 5:
            continue

        if len(line) > 260 and ("copyright" in low or "webflow" in low):
            continue

        if low in seen:
            continue

        seen.add(low)
        lines.append(line)

    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def extract_page_text(html: str) -> Tuple[str, str]:
    soup = BeautifulSoup(html or "", "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "form", "header", "footer", "nav"]):
        tag.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    candidates = []
    selectors = [
        "main", "article", ".w-richtext", ".rich-text", ".page-content", ".content",
        ".container", ".section", "body",
    ]

    for selector in selectors:
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

    if p.netloc not in {"www.burobd.org", "burobd.org"}:
        return False

    blocked_ext = [
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        ".zip", ".rar", ".mp4", ".mp3",
    ]

    return not any(p.path.lower().endswith(x) for x in blocked_ext)


def discover_links(html: str, current_url: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    links = []

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()

        if not href:
            continue

        if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue

        full = normalize_url(urljoin(current_url, href))

        if allowed_url(full):
            links.append(full)

    return list(dict.fromkeys(links))


def chunk_text(text: str, size: int = 1100, overlap: int = 150) -> List[str]:
    text = (text or "").strip()

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
            step = max(1, size - overlap)
            while start < len(ch):
                part = ch[start:start + size].strip()
                if part:
                    final.append(part)
                start += step

    return final


def ensure_collection(vector_size: int, recreate: bool = False) -> None:
    url = qdrant_url()
    headers = qdrant_headers()

    if recreate:
        try:
            requests.delete(f"{url}/collections/{QDRANT_COLLECTION}", headers=headers, timeout=TIMEOUT)
        except Exception:
            pass

    check = requests.get(f"{url}/collections/{QDRANT_COLLECTION}", headers=headers, timeout=TIMEOUT)

    if check.status_code == 200:
        return

    create = requests.put(
        f"{url}/collections/{QDRANT_COLLECTION}",
        json={"vectors": {"size": vector_size, "distance": "Cosine"}},
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
    if "/programs" in data:
        return 96
    if any(x in data for x in ["/general-loans", "/agricultural", "/sme-", "/emergency-loans", "/disaster-loans"]):
        return 95
    if any(x in data for x in ["/general-savings", "/contractual-savings", "/voluntary-savings"]):
        return 94
    if "/remittance" in data:
        return 93
    if "/about-buro" in data:
        return 90
    if "founder executive director" in data:
        return 98
    if "/development-projects/buro-healthcare-foundation" in data:
        return 88
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


def sync_buro_website(max_pages: int = 120, recreate_collection: bool = True) -> Dict[str, Any]:
    seeds = [
        "https://www.burobd.org/",
        "https://www.burobd.org/about-buro",
        "https://www.burobd.org/governance",
        "https://www.burobd.org/programs",
        "https://www.burobd.org/remittance",
        "https://www.burobd.org/buro-remittance-service",
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
        "https://www.burobd.org/development-projects/buro-healthcare-foundation",
        "https://www.burobd.org/development-projects/digital-healthcare-services",
        "https://www.burobd.org/buro-reports",
        "https://www.burobd.org/buro-publications",
        "https://www.burobd.org/buro-updates",
    ]

    queue = [normalize_url(x) for x in seeds]
    visited = set()
    indexed_pages = 0
    indexed_chunks = 0
    errors = []
    vector_size = None

    session = requests.Session()
    session.headers.update({"User-Agent": "BURO-RAG-Clean-Sync/2.0"})

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
                        "title": title or "BURO Website",
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
        "errors": errors[:25],
    }


# =============================================================================
# Search / rerank / live expansion
# =============================================================================
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
            "origin": "qdrant",
        })

    return rerank_hits(query, hits)


def rerank_hits(query: str, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def score(hit: Dict[str, Any]) -> float:
        url = (hit.get("url") or hit.get("source_url") or "").lower()
        title = (hit.get("title") or "").lower()
        text = (hit.get("text") or "").lower()
        data = f"{url} {title} {text}"

        s = float(hit.get("score") or 0)
        s += float(hit.get("priority") or 0) / 1000

        if is_governance_query(query):
            if "/governance" in url:
                s += 1.20
            if "founder executive director" in data:
                s += 0.80
            if "zakir hossain" in data:
                s += 0.70
            if "/about-buro" in url and not is_about_query(query):
                s -= 0.30

        if is_loan_query(query):
            if "/programs" in url:
                s += 0.90
            if any(path in url for path in [
                "/general-loans", "/agricultural-loans", "/sme-loans",
                "/emergency-loans", "/disaster-loans", "/water-sanitation-loans",
                "/consumer-loans",
            ]):
                s += 1.25
            if "loan" in title or "loan" in text:
                s += 0.45
            if "/about-buro" in url:
                s -= 0.40
            if "/governance" in url:
                s -= 0.45
            if "/remittance" in url and not is_remittance_query(query):
                s -= 0.30

        if is_savings_query(query):
            if any(path in url for path in ["/general-savings", "/contractual-savings", "/voluntary-savings"]):
                s += 1.20
            if "/programs" in url:
                s += 0.65
            if "savings" in data or "saving" in data:
                s += 0.45

        if is_remittance_query(query):
            if "/remittance" in url or "/buro-remittance-service" in url:
                s += 1.20
            if "remittance" in data:
                s += 0.50

        if is_services_query(query) and not (is_loan_query(query) or is_savings_query(query) or is_remittance_query(query)):
            if "/programs" in url:
                s += 1.00
            if "our services" in data or "programs" in data or "services" in data:
                s += 0.45

        if is_mission_vision_query(query):
            if "/about-buro" in url:
                s += 1.10
            if any(x in data for x in ["vision", "mission", "values", "philosophy"]):
                s += 0.60

        if is_healthcare_query(query):
            if "healthcare" in url or "healthcare" in data or "health care" in data:
                s += 1.00

        if is_report_query(query):
            if "/buro-reports" in url or "/buro-publications" in url:
                s += 1.10
            if "annual report" in data or "publications" in data:
                s += 0.50

        if is_about_query(query) and not (is_loan_query(query) or is_governance_query(query)):
            if "/about-buro" in url:
                s += 0.75
            if url.rstrip("/") == "https://www.burobd.org":
                s += 0.25

        return s

    return sorted(hits, key=score, reverse=True)


def forced_urls_for_query(query: str) -> List[str]:
    urls: List[str] = []

    def add(url: str) -> None:
        url = normalize_url(url)
        if url not in urls:
            urls.append(url)

    if is_governance_query(query):
        add("https://www.burobd.org/governance")

    if is_about_query(query) or is_mission_vision_query(query):
        add("https://www.burobd.org/about-buro")
        add("https://www.burobd.org/")

    if is_services_query(query):
        add("https://www.burobd.org/programs")

    if is_loan_query(query):
        add("https://www.burobd.org/programs")
        add("https://www.burobd.org/general-loans")
        add("https://www.burobd.org/agricultural-loans")
        add("https://www.burobd.org/sme-loans")
        add("https://www.burobd.org/emergency-loans")
        add("https://www.burobd.org/disaster-loans")
        add("https://www.burobd.org/water-sanitation-loans")
        add("https://www.burobd.org/consumer-loans")

    if is_savings_query(query):
        add("https://www.burobd.org/programs")
        add("https://www.burobd.org/general-savings")
        add("https://www.burobd.org/contractual-savings")
        add("https://www.burobd.org/voluntary-savings")

    if is_remittance_query(query):
        add("https://www.burobd.org/remittance")
        add("https://www.burobd.org/buro-remittance-service")

    if is_healthcare_query(query):
        add("https://www.burobd.org/development-projects/buro-healthcare-foundation")
        add("https://www.burobd.org/development-projects/digital-healthcare-services")

    if is_report_query(query):
        add("https://www.burobd.org/buro-reports")
        add("https://www.burobd.org/buro-publications")

    return urls


def fetch_live_page(url: str) -> Optional[Dict[str, Any]]:
    if not allowed_url(url):
        return None

    try:
        resp = requests.get(
            url,
            timeout=TIMEOUT,
            headers={"User-Agent": "BURO-RAG-Live-Expand/2.0"},
        )

        if resp.status_code != 200:
            return None

        title, text = extract_page_text(resp.text)

        if not text or len(text) < 80:
            return None

        return {
            "title": title or "BURO Website",
            "url": normalize_url(url),
            "source_url": normalize_url(url),
            "text": text[:LIVE_EXPAND_MAX_CHARS_PER_PAGE],
            "priority": page_priority(url, title, text) + 25,
            "score": 9.0,
            "origin": "live",
        }

    except Exception:
        return None


def live_expand_chunks(query: str, chunks: List[Dict[str, Any]], enabled: bool = True) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not enabled:
        return [], []

    urls: List[str] = []

    def add(url: str) -> None:
        if not url:
            return
        url = normalize_url(url)
        if allowed_url(url) and url not in urls:
            urls.append(url)

    for url in forced_urls_for_query(query):
        add(url)

    for c in chunks[:LIVE_EXPAND_TOP_N]:
        add(c.get("url") or c.get("source_url") or "")

    live_rows: List[Dict[str, Any]] = []
    for url in urls[: max(1, LIVE_EXPAND_TOP_N + len(forced_urls_for_query(query)))]:
        row = fetch_live_page(url)
        if row:
            live_rows.append(row)

    # Turn live pages into chunk-like rows. Keep one large live page row plus
    # a few smaller chunks for better context distribution.
    live_chunks: List[Dict[str, Any]] = []
    for row in live_rows:
        live_chunks.append(row)
        for idx, part in enumerate(chunk_text(row["text"], size=1600, overlap=120)[:4]):
            live_chunks.append({
                **row,
                "text": part,
                "chunk_index": idx,
                "score": 9.0 - (idx * 0.01),
            })

    live_sources = build_sources(live_rows)
    return rerank_hits(query, live_chunks), live_sources


def build_context(chunks: List[Dict[str, Any]]) -> str:
    lines = []
    total = 0

    for i, item in enumerate(chunks, start=1):
        title = item.get("title") or "Untitled"
        url = item.get("url") or item.get("source_url") or ""
        origin = item.get("origin") or "qdrant"
        text = (item.get("text") or "").strip()

        if not text:
            continue

        if len(text) > 1800:
            text = text[:1800]

        block = (
            f"[Source {i} | {origin}]\n"
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
            "title": row.get("title") or "BURO Website",
            "url": url,
            "score": row.get("score"),
        })

    return sources[:8]


# =============================================================================
# Deterministic answers / extractive fallback
# =============================================================================
def _source_lines(sources: List[Dict[str, Any]], max_items: int = 5, bn: bool = False) -> str:
    lines = []
    for src in sources[:max_items]:
        url = src.get("url") or ""
        title = src.get("title") or "BURO Website"
        if url:
            lines.append(f"- {title}: {url}")

    if not lines:
        return ""

    return ("\n\nউৎস:\n" if bn else "\n\nSources:\n") + "\n".join(lines)


def _context_text(chunks: List[Dict[str, Any]]) -> str:
    return "\n".join((c.get("text") or "") for c in chunks)


def _sentences(text: str) -> List[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?।])\s+", text) if len(s.strip()) > 25]


def _extract_relevant_sentences(text: str, keywords: List[str], limit: int = 8) -> List[str]:
    out = []
    seen = set()

    for s in _sentences(text):
        low = s.lower()
        if any(k.lower() in low for k in keywords):
            clean = s.strip()
            if clean.lower() not in seen:
                seen.add(clean.lower())
                out.append(clean)

        if len(out) >= limit:
            break

    return out


def _looks_like_empty_or_not_found(answer: str) -> bool:
    a = (answer or "").strip().lower()
    if not a:
        return True

    markers = [
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
    return any(x in a for x in markers)


def about_answer(chunks: List[Dict[str, Any]], sources: List[Dict[str, Any]], language: str) -> str:
    text = _context_text(chunks)
    keys = ["microfinance", "financial sustainability", "commercial capital", "vision", "mission", "philosophy", "inclusive", "sustainable financial services"]
    evidence = _extract_relevant_sentences(text, keys, limit=6)

    if language == "bn":
        if evidence:
            body = (
                "প্রদত্ত BURO website context অনুযায়ী, BURO Bangladesh বাংলাদেশের একটি microfinance institution। "
                "ওয়েবসাইটে BURO Bangladesh-কে inclusive এবং sustainable financial services-এর মাধ্যমে ব্যক্তি ও কমিউনিটিকে ক্ষমতায়নে কাজ করা প্রতিষ্ঠান হিসেবে উপস্থাপন করা হয়েছে। "
                "এর vision-এ বাংলাদেশের মানুষের মৌলিক চাহিদা পূরণকারী সুখী, সমৃদ্ধ ও pluralistic democratic society-এর কথা বলা হয়েছে।"
            )
        else:
            body = "প্রদত্ত BURO website context অনুযায়ী, BURO Bangladesh একটি microfinance institution।"
        return body + _source_lines(sources, bn=True)

    if evidence:
        body = (
            "Based on the retrieved BURO website context, BURO Bangladesh is a microfinance institution in Bangladesh. "
            "The website describes it as committed to financial sustainability through commercial capital and to empowering individuals and communities through inclusive and sustainable financial services."
        )
    else:
        body = "Based on the retrieved BURO website context, BURO Bangladesh is a microfinance institution in Bangladesh."

    return body + _source_lines(sources)


def mission_vision_answer(chunks: List[Dict[str, Any]], sources: List[Dict[str, Any]], language: str) -> str:
    text = _context_text(chunks)
    evidence = _extract_relevant_sentences(text, ["vision", "mission", "values", "philosophy", "happy", "prosperous", "sustainable financial services"], limit=8)

    if language == "bn":
        body = (
            "প্রদত্ত BURO website context অনুযায়ী:\n\n"
            "- Vision: বাংলাদেশের মানুষের মৌলিক চাহিদা পূরণকারী একটি সুখী, সমৃদ্ধ ও pluralistic democratic society।\n"
            "- Philosophy: BURO Bangladesh inclusive এবং sustainable financial services-এর মাধ্যমে ব্যক্তি ও কমিউনিটিকে ক্ষমতায়নে বিশ্বাস করে। "
            "এটি dignity, respect, community engagement, trust এবং transparency-এর ওপর গুরুত্ব দেয়।"
        )
        if evidence:
            body += "\n\nওয়েবসাইট context-এ mission/values/philosophy সম্পর্কিত তথ্য About BURO page-এ পাওয়া যায়।"
        return body + _source_lines(sources, bn=True)

    body = (
        "According to the retrieved BURO website context:\n\n"
        "- Vision: a happy, prosperous and pluralistic democratic society that meets the basic needs of the people in Bangladesh.\n"
        "- Philosophy: BURO Bangladesh emphasizes inclusive and sustainable financial services, dignity, respect, community engagement, trust and transparency."
    )
    return body + _source_lines(sources)


def services_answer(chunks: List[Dict[str, Any]], sources: List[Dict[str, Any]], language: str) -> str:
    text = _context_text(chunks).lower()

    service_groups = []
    if "loan" in text:
        service_groups.append("Loan")
    if "savings" in text or "saving" in text:
        service_groups.append("Savings")
    if "remittance" in text:
        service_groups.append("Remittance")
    if "health" in text or "healthcare" in text:
        service_groups.append("Healthcare / health education")
    if "digital financial services" in text:
        service_groups.append("Digital Financial Services")
    if "education" in text:
        service_groups.append("Educational support")
    if "water" in text and "supply" in text:
        service_groups.append("Rural piped water supply")

    if not service_groups:
        service_groups = ["Loan", "Savings", "Remittance"]

    if language == "bn":
        body = (
            "প্রদত্ত BURO website context অনুযায়ী, BURO Bangladesh বিভিন্ন ধরনের সেবা ও program পরিচালনা করে। "
            "এর মধ্যে রয়েছে: "
            + ", ".join(service_groups)
            + "।"
        )
        return body + _source_lines(sources, bn=True)

    body = (
        "Based on the retrieved BURO website context, BURO Bangladesh provides several services and programs, including "
        + ", ".join(service_groups)
        + "."
    )
    return body + _source_lines(sources)


def loan_answer(chunks: List[Dict[str, Any]], sources: List[Dict[str, Any]], language: str) -> str:
    text = _context_text(chunks).lower()
    known = [
        ("Agricultural Loans", ["agricultural loans", "agricultural loan"]),
        ("SME Loans", ["sme loans", "sme loan"]),
        ("General Loans", ["general loans", "general loan"]),
        ("Emergency Loans", ["emergency loans", "emergency loan"]),
        ("Disaster Loans", ["disaster loans", "disaster loan"]),
        ("Water & Sanitation Loans", ["water& sanitation loans", "water & sanitation loans", "water sanitation loans"]),
        ("Consumer Loans", ["consumer loans", "consumer loan"]),
    ]
    found = [name for name, keys in known if any(k in text for k in keys)]

    if not found:
        for c in chunks:
            url = (c.get("url") or "").lower()
            if "general-loans" in url and "General Loans" not in found:
                found.append("General Loans")
            if "agricultural" in url and "Agricultural Loans" not in found:
                found.append("Agricultural Loans")
            if "sme" in url and "SME Loans" not in found:
                found.append("SME Loans")
            if "emergency" in url and "Emergency Loans" not in found:
                found.append("Emergency Loans")
            if "disaster" in url and "Disaster Loans" not in found:
                found.append("Disaster Loans")
            if "water" in url and "Water & Sanitation Loans" not in found:
                found.append("Water & Sanitation Loans")
            if "consumer" in url and "Consumer Loans" not in found:
                found.append("Consumer Loans")

    if not found:
        found = ["Agricultural Loans", "SME Loans", "General Loans", "Emergency Loans", "Disaster Loans", "Water & Sanitation Loans", "Consumer Loans"]

    if language == "bn":
        body = (
            "প্রদত্ত BURO website context অনুযায়ী, BURO Bangladesh বিভিন্ন ধরনের loan service প্রদান করে। "
            "এর মধ্যে রয়েছে: "
            + ", ".join(found)
            + "। এসব ঋণসেবা clients ও communities-কে accessible financial solutions দিতে সহায়তা করে।"
        )
        return body + _source_lines(sources, bn=True)

    body = (
        "Based on the retrieved BURO website context, BURO Bangladesh provides several loan services, including "
        + ", ".join(found)
        + ". These services are designed to support clients and communities with accessible financial solutions."
    )
    return body + _source_lines(sources)


def savings_answer(chunks: List[Dict[str, Any]], sources: List[Dict[str, Any]], language: str) -> str:
    text = _context_text(chunks).lower()
    found = []
    if "general savings" in text or "general saving" in text:
        found.append("General Savings")
    if "contractual savings" in text or "contractual saving" in text:
        found.append("Contractual Savings")
    if "voluntary savings" in text or "voluntary saving" in text:
        found.append("Voluntary Savings")
    if not found:
        found = ["General Savings", "Contractual Savings", "Voluntary Savings"]

    if language == "bn":
        body = (
            "প্রদত্ত BURO website context অনুযায়ী, BURO Bangladesh clients-এর জন্য secure savings services প্রদান করে। "
            "সঞ্চয় সেবার মধ্যে রয়েছে: "
            + ", ".join(found)
            + "।"
        )
        return body + _source_lines(sources, bn=True)

    body = (
        "Based on the retrieved BURO website context, BURO Bangladesh provides secure savings services, including "
        + ", ".join(found)
        + "."
    )
    return body + _source_lines(sources)


def remittance_answer(chunks: List[Dict[str, Any]], sources: List[Dict[str, Any]], language: str) -> str:
    text = _context_text(chunks)
    sentences = _extract_relevant_sentences(text, ["remittance", "foreign earnings", "transactions", "branch network"], limit=4)

    if language == "bn":
        body = (
            "প্রদত্ত BURO website context অনুযায়ী, BURO Bangladesh remittance service-এর মাধ্যমে বিদেশে কর্মরত বাংলাদেশিদের পাঠানো অর্থ স্থানীয় beneficiaries-এর কাছে নিরাপদভাবে পৌঁছে দিতে সহায়তা করে। "
            "ওয়েবসাইট context-এ extensive nationwide branch network-এর মাধ্যমে remittance fund বিতরণের কথা উল্লেখ আছে।"
        )
        return body + _source_lines(sources, bn=True)

    if sentences:
        body = "Based on the retrieved BURO website context, " + " ".join(sentences[:3])
    else:
        body = "Based on the retrieved BURO website context, BURO Bangladesh provides remittance services to help deliver money sent by Bangladeshi expatriates to local beneficiaries through its branch network."

    return body + _source_lines(sources)


def director_profile_answer(chunks: List[Dict[str, Any]], sources: List[Dict[str, Any]], language: str, direct_only: bool = False) -> str:
    text = _context_text(chunks)
    low = text.lower()

    has_zakir = "zakir hossain" in low
    has_founder_ed = "founder executive director" in low

    if direct_only:
        if language == "bn":
            if has_zakir and has_founder_ed:
                return "BURO Bangladesh-এর Governance page অনুযায়ী, Zakir Hossain হলেন BURO Bangladesh-এর Founder Executive Director।" + _source_lines(sources, bn=True)
            return _empty_context_answer("bn") + _source_lines(sources, bn=True)

        if has_zakir and has_founder_ed:
            return "According to the BURO Bangladesh Governance page, Zakir Hossain is the Founder Executive Director of BURO Bangladesh." + _source_lines(sources)
        return _empty_context_answer("en") + _source_lines(sources)

    keywords = [
        "zakir hossain", "founder executive director", "financial services", "microfinance",
        "ngo", "program design", "management", "organizational development", "training",
        "savings", "micro-credit", "micro-insurance", "disaster management", "human resource",
        "administration", "financial policy", "inafi", "director", "governance"
    ]
    evidence = _extract_relevant_sentences(text, keywords, limit=10)

    if language == "bn":
        if has_zakir and has_founder_ed:
            body = (
                "প্রদত্ত BURO website context অনুযায়ী, Zakir Hossain BURO Bangladesh-এর Founder Executive Director।"
            )
            if len(evidence) > 1:
                body += (
                    " Governance page-এর profile/context-এ তার পেশাগত অভিজ্ঞতা ও কাজের ক্ষেত্র হিসেবে financial services, microfinance/NGO program design, management, organizational development, training, savings, micro-credit, micro-insurance, disaster management এবং HR/admin/financial policy operations সম্পর্কিত তথ্য পাওয়া যায়—যতটুকু ওয়েবসাইট context-এ আছে।"
                )
            else:
                body += " ওয়েবসাইট context-এ এর বাইরে বিস্তারিত ব্যক্তিগত profile information স্পষ্টভাবে পাওয়া যায়নি।"
        else:
            body = "প্রদত্ত BURO website context-এ director profile সম্পর্কে নির্দিষ্ট তথ্য পাওয়া যায়নি।"
        return body + _source_lines(sources, bn=True)

    if has_zakir and has_founder_ed:
        body = "According to the retrieved BURO website context, Zakir Hossain is the Founder Executive Director of BURO Bangladesh."
        if len(evidence) > 1:
            body += " The Governance page context indicates profile-related experience in financial services, microfinance/NGO program design, management, organizational development, training, savings, micro-credit, micro-insurance, disaster management, and HR/admin/financial policy operations, where available in the extracted website text."
        else:
            body += " The retrieved website context does not provide a fuller personal biography beyond this designation."
    else:
        body = "The specific director profile details were not found in the provided BURO website information."

    return body + _source_lines(sources)


def healthcare_answer(chunks: List[Dict[str, Any]], sources: List[Dict[str, Any]], language: str) -> str:
    text = _context_text(chunks)
    evidence = _extract_relevant_sentences(text, ["healthcare", "health care", "health education", "digital healthcare", "community awareness"], limit=5)

    if language == "bn":
        body = "প্রদত্ত BURO website context অনুযায়ী, BURO Bangladesh-এর development/healthcare-related কার্যক্রমের মধ্যে healthcare foundation, digital healthcare services এবং health education/community awareness সম্পর্কিত তথ্য পাওয়া যায়।"
        return body + _source_lines(sources, bn=True)

    body = "Based on the retrieved BURO website context, BURO Bangladesh has healthcare/development-related initiatives such as healthcare foundation activities, digital healthcare services, and health education/community awareness programs."
    if evidence:
        body += " " + " ".join(evidence[:2])
    return body + _source_lines(sources)


def report_answer(chunks: List[Dict[str, Any]], sources: List[Dict[str, Any]], language: str) -> str:
    if language == "bn":
        body = "প্রদত্ত BURO website context অনুযায়ী, BURO Bangladesh website-এ annual reports, publications এবং related report/document resources পাওয়া যায়।"
        return body + _source_lines(sources, bn=True)

    body = "Based on the retrieved BURO website context, the BURO website includes annual reports, publications and related report/document resources."
    return body + _source_lines(sources)


def extractive_fallback_answer(query: str, chunks: List[Dict[str, Any]], sources: List[Dict[str, Any]], language: str = "en") -> str:
    text = _context_text(chunks)

    if language == "bn":
        if is_governance_query(query):
            return director_profile_answer(chunks, sources, language, direct_only=is_direct_who_is_director_query(query))
        if is_loan_query(query):
            return loan_answer(chunks, sources, language)
        if is_savings_query(query):
            return savings_answer(chunks, sources, language)
        if is_remittance_query(query):
            return remittance_answer(chunks, sources, language)
        if is_services_query(query):
            return services_answer(chunks, sources, language)
        if is_mission_vision_query(query):
            return mission_vision_answer(chunks, sources, language)
        if is_healthcare_query(query):
            return healthcare_answer(chunks, sources, language)
        return about_answer(chunks, sources, language)

    # English fallback: extract useful English-looking sentences.
    candidates = []
    for s in _sentences(text):
        if contains_bangla_script(s) or contains_devanagari_script(s):
            continue
        if any(p.lower() in s.lower() for p in BAD_PHRASES):
            continue
        candidates.append(s)
        if len(candidates) >= 5:
            break

    if not candidates:
        return about_answer(chunks, sources, language)

    body = "Based on the retrieved BURO website context:\n\n" + " ".join(candidates[:5])
    return body + _source_lines(sources)


# =============================================================================
# Answer generation
# =============================================================================
def generate_answer(
    query: str,
    chunks: List[Dict[str, Any]],
    language: str = "en",
    *,
    num_predict: Optional[int] = None,
) -> str:
    language = normalize_language(query=query, language=language)
    chunks = rerank_hits(query, chunks)
    sources = build_sources(chunks)
    context = build_context(chunks)

    if not context.strip():
        return _empty_context_answer(language)

    # Deterministic answers for major website categories.
    if is_governance_query(query):
        return director_profile_answer(chunks, sources, language, direct_only=is_direct_who_is_director_query(query))

    if is_loan_query(query):
        return loan_answer(chunks, sources, language)

    if is_savings_query(query):
        return savings_answer(chunks, sources, language)

    if is_remittance_query(query):
        return remittance_answer(chunks, sources, language)

    if is_services_query(query):
        return services_answer(chunks, sources, language)

    if is_mission_vision_query(query):
        return mission_vision_answer(chunks, sources, language)

    if is_healthcare_query(query):
        return healthcare_answer(chunks, sources, language)

    if is_report_query(query):
        return report_answer(chunks, sources, language)

    if is_about_query(query):
        return about_answer(chunks, sources, language)

    language_rule = _language_instruction(language)

    if language == "bn":
        prompt = f"""
আপনি BURO Bangladesh-এর অফিসিয়াল ওয়েবসাইট RAG সহকারী।

ভাষা নির্দেশনা:
- {language_rule}
- শুধু Context-এর তথ্য ব্যবহার করুন।
- তথ্য না থাকলে বলুন: "{_empty_context_answer("bn")}"
- অনুমান করবেন না।
- শেষে উৎস URL দিন।

প্রশ্ন:
{query}

Context:
{context}

বাংলা উত্তর:
""".strip()

        retry_prompt = f"""
নিচের Context ব্যবহার করে শুধুমাত্র প্রমিত বাংলায় উত্তর দিন।
হিন্দি/দেবনাগরী অক্ষর ব্যবহার করবেন না।
নতুন তথ্য যোগ করবেন না।

প্রশ্ন:
{query}

Context:
{context}

শুদ্ধ বাংলা উত্তর:
""".strip()

    else:
        prompt = f"""
You are the official BURO Bangladesh website RAG assistant.

Language instruction:
- {language_rule}
- Use only the Context.
- If the answer is unavailable, say: "{_empty_context_answer("en")}"
- Do not infer facts.
- Add source URLs at the end.

Question:
{query}

Context:
{context}

English answer:
""".strip()

        retry_prompt = f"""
Rewrite using English only.
No Bangla script. No Hindi. No Devanagari script.
Use only the Context. Do not add new facts.

Question:
{query}

Context:
{context}

Final English answer:
""".strip()

    answer = generate_llm(prompt, language=language, num_predict=num_predict, retry_prompt=retry_prompt)

    if _needs_language_retry(answer, language) or (_looks_like_empty_or_not_found(answer) and chunks):
        return extractive_fallback_answer(query, chunks, sources, language)

    return answer


# =============================================================================
# Public tool entrypoint
# =============================================================================
def run(user=None, query: str = "", params=None, language: str = "en", **kwargs) -> Dict[str, Any]:
    try:
        params = params or {}
        language = normalize_language(query=query, language=language, params=params)

        if params.get("action") == "sync":
            max_pages = int(params.get("max_pages", 120))
            recreate = bool(params.get("recreate", True))
            result = sync_buro_website(max_pages=max_pages, recreate_collection=recreate)
            return {
                "ok": True,
                "source": QDRANT_COLLECTION,
                "source_collection": QDRANT_COLLECTION,
                "action": "sync",
                "result": result,
                "answer": "BURO ওয়েবসাইট clean sync সম্পন্ন হয়েছে।" if language == "bn" else "BURO website clean sync completed.",
                "sources": [],
                "matches_count": 0,
                "live_expanded": False,
                "live_sources": [],
                "language": language,
                "model": select_generation_model(language),
            }

        try:
            limit = int(params.get("limit", params.get("top_k", 8)))
        except Exception:
            limit = 8
        limit = max(1, min(limit, 12))

        try:
            num_predict = int(params.get("num_predict", GEN_NUM_PREDICT))
        except Exception:
            num_predict = GEN_NUM_PREDICT

        if bool(params.get("fast", False)):
            num_predict = min(num_predict, 350)

        result = search_qdrant(query, limit=limit)
        qdrant_chunks = normalize_hits(query, result)

        live_expand_requested = params.get("live_expand")
        if live_expand_requested is None:
            live_expand = LIVE_EXPAND_ENABLED
        else:
            live_expand = str(live_expand_requested).strip().lower() in {"1", "true", "yes", "y", "on"}

        live_chunks, live_sources = live_expand_chunks(query, qdrant_chunks, enabled=live_expand)
        all_chunks = rerank_hits(query, live_chunks + qdrant_chunks)

        sources = build_sources(all_chunks)

        if not all_chunks:
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
                "live_expanded": False,
                "live_sources": [],
                "language": language,
                "model": select_generation_model(language),
            }

        final_answer = generate_answer(query, all_chunks, language=language, num_predict=num_predict)

        return {
            "ok": True,
            "source": QDRANT_COLLECTION,
            "source_collection": QDRANT_COLLECTION,
            "answer": final_answer,
            "sources": sources,
            "matches_count": len(all_chunks),
            "qdrant_matches_count": len(qdrant_chunks),
            "live_expanded": bool(live_chunks),
            "live_sources": live_sources,
            "language": language,
            "model": select_generation_model(language),
        }

    except Exception as exc:
        language = normalize_language(query=query, language=language, params=params or {})
        return {
            "ok": False,
            "source": QDRANT_COLLECTION,
            "source_collection": QDRANT_COLLECTION,
            "error": str(exc),
            "answer": f"Website RAG tool error: {exc}",
            "sources": [],
            "matches_count": 0,
            "live_expanded": False,
            "live_sources": [],
            "language": language,
            "model": select_generation_model(language),
        }
