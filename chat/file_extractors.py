# chat/file_extractors.py
from __future__ import annotations

from typing import Optional
from pypdf import PdfReader
from docx import Document

def extract_text_from_pdf(path: str) -> str:
    out = []
    reader = PdfReader(path)
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            out.append(t)
    return "\n\n".join(out).strip()

def extract_text_from_docx(path: str) -> str:
    doc = Document(path)
    parts = []
    for p in doc.paragraphs:
        if p.text and p.text.strip():
            parts.append(p.text.strip())
    return "\n".join(parts).strip()

def extract_text_from_txt(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read().strip()

def extract_text(path: str, content_type: str, filename: str) -> str:
    fn = (filename or "").lower()
    ct = (content_type or "").lower()

    if fn.endswith(".pdf") or "pdf" in ct:
        return extract_text_from_pdf(path)

    if fn.endswith(".docx") or "officedocument.wordprocessingml" in ct:
        return extract_text_from_docx(path)

    if fn.endswith(".txt") or "text/plain" in ct:
        return extract_text_from_txt(path)

    # Unsupported types (images need OCR; add later)
    return ""
