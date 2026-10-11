from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


def is_same_domain(url, base_domain):
    return urlparse(url).netloc == base_domain


def extract_text_from_html(html):
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = soup.get_text(" ", strip=True)

    return title, text


def crawl_site(start_url, max_pages=100, max_depth=3):
    parsed = urlparse(start_url)
    base_domain = parsed.netloc

    seen = set()
    queue = [(start_url, 0)]
    pages = []

    while queue and len(pages) < max_pages:
        url, depth = queue.pop(0)

        if url in seen or depth > max_depth:
            continue

        seen.add(url)

        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": "BURO-Agentic-RAG-Sync/1.0"})
            if "text/html" not in resp.headers.get("Content-Type", ""):
                continue

            title, text = extract_text_from_html(resp.text)
            pages.append({
                "url": url,
                "title": title,
                "text": text,
                "depth": depth,
            })

            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                next_url = urljoin(url, a["href"]).split("#")[0]

                if not next_url.startswith("http"):
                    continue

                if is_same_domain(next_url, base_domain) and next_url not in seen:
                    queue.append((next_url, depth + 1))

        except Exception:
            continue

    return pages
