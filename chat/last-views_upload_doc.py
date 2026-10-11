# chat/views_upload_doc.py
# ------------------------------------------------------------
# STRICT production-ready "uploaded document" summarizer / Q&A
#
# Fix included:
# ✅ deterministic extractive fallback (no hallucination)
#    - avoids banned "after uploading / test prompts" lines
#    - guarantees output format even if LLM fails validator
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, List

import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import UploadedFile


# -------------------------
# Config
# -------------------------
def _env(k: str, default: str) -> str:
    return (os.getenv(k, default) or "").strip()


def _env_int(k: str, default: str) -> int:
    try:
        return int(_env(k, default))
    except Exception:
        return int(default)


def _env_float(k: str, default: str) -> float:
    try:
        return float(_env(k, default))
    except Exception:
        return float(default)


@dataclass(frozen=True)
class Cfg:
    OLLAMA_URL: str = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    GEN_MODEL: str = _env("GEN_MODEL", "aya-23-8b:q5").strip() or "aya-23-8b:q5"
    GEN_TIMEOUT: float = _env_float("GEN_TIMEOUT", "6000")

    DOC_MAX_CHARS: int = _env_int("DOC_MAX_CHARS", "180000000")

    DOC_REQUIRE_AUTH: bool = (_env("DOC_REQUIRE_AUTH", "1").lower() not in ("0", "false", "no"))
    DOC_ALLOWED_GROUPS_CSV: str = _env("DOC_ALLOWED_GROUPS", "")  # e.g. "general,policy,core"


CFG = Cfg()


# -------------------------
# Basic helpers
# -------------------------
def _json_body(request) -> Dict[str, Any]:
    try:
        raw = (request.body or b"{}").decode("utf-8", errors="replace")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _clean(v: Any) -> str:
    return ("" if v is None else str(v)).strip()


def _user_group_names(user) -> set:
    try:
        if not user or not getattr(user, "is_authenticated", False):
            return set()
        return set(user.groups.values_list("name", flat=True))
    except Exception:
        return set()


def _authz_or_error(request) -> Optional[JsonResponse]:
    if not CFG.DOC_REQUIRE_AUTH:
        return None

    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

    if getattr(user, "is_superuser", False):
        return None

    allow_csv = (CFG.DOC_ALLOWED_GROUPS_CSV or "").strip()
    if not allow_csv:
        return None  # any authenticated user allowed

    allowed = {x.strip() for x in allow_csv.split(",") if x.strip()}
    groups = _user_group_names(user)
    if not (groups & allowed):
        return JsonResponse(
            {
                "ok": False,
                "error": "Not allowed for document summary.",
                "hint": f"User must belong to one of groups: {sorted(list(allowed))}",
                "user_groups": sorted(list(groups)),
            },
            status=403,
        )
    return None


def _clean_pdf_artifacts(text: str) -> str:
    t = text or ""
    t = re.sub(r"\(cid:\d+\)", "•", t)  # common pdf bullet artifact
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _get_uploaded_file(file_token: str) -> Optional[UploadedFile]:
    tok = _clean(file_token)
    if not tok:
        return None

    qs = UploadedFile.objects.all()

    # Try file_id
    try:
        if hasattr(UploadedFile, "file_id"):
            uf = qs.filter(file_id=tok).first()
            if uf:
                return uf
    except Exception:
        pass

    # Try numeric pk
    try:
        pk = int(tok)
        return qs.filter(id=pk).first()
    except Exception:
        return None


# -------------------------
# STRICT output enforcement
# -------------------------
TASKY_PHRASES = [
    "summarize the", "extract the", "explain the", "users can", "after uploading",
    "test prompts", "designed to", "the system", "this feature", "this service",
]

FORBIDDEN_EXTERNAL_TERMS = [
    "chatbot", "chatbots", "translation", "speech recognition",
    "algorithms", "data analysis", "machine learning", "deep learning",
]


def _strip_markdown_headings(ans: str) -> str:
    t = (ans or "").strip()
    t = re.sub(r"^\s*\*\*.*?\*\*\s*:?[\r\n]*", "", t, flags=re.M)
    return t.strip()


def _first_block_only(ans: str) -> str:
    t = (ans or "").strip()
    i = t.find("BULLETS:")
    j = t.find("PARAGRAPH:")
    if i == -1 or j == -1 or j < i:
        return t.strip()

    after_para = t[j:]
    k = after_para.find("\nBULLETS:")
    if k != -1:
        return (t[: j + k]).strip()
    return t.strip()


def _contains_any(s: str, terms: list) -> bool:
    sl = (s or "").lower()
    return any(x in sl for x in terms)


def _enforce_format_or_empty(ans: str) -> str:
    t = _first_block_only(_strip_markdown_headings(ans))

    if "BULLETS:" not in t or "PARAGRAPH:" not in t:
        return ""

    parts = t.split("PARAGRAPH:", 1)
    bullets_part = parts[0]
    para_part = parts[1].strip()

    blines = []
    for line in bullets_part.splitlines():
        line = line.strip()
        if line.startswith("- "):
            blines.append(line[2:].strip())

    if len(blines) != 3:
        return ""

    if not para_part or "BULLETS:" in para_part:
        return ""

    combined = "\n".join(blines) + "\n" + para_part

    if _contains_any(combined, TASKY_PHRASES):
        return ""

    if _contains_any(combined, FORBIDDEN_EXTERNAL_TERMS):
        return ""

    out = "BULLETS:\n" + "\n".join([f"- {b}" for b in blines]) + "\n\nPARAGRAPH:\n" + para_part
    return out.strip()


def _strict_postcheck(answer: str) -> str:
    t = (answer or "").strip()
    return t if t else "Not found in the document."


# -------------------------
# Deterministic fallback (EXTRACTIVE ONLY)
# -------------------------
def _is_tasky_line(s: str) -> bool:
    sl = (s or "").strip().lower()
    return any(p in sl for p in TASKY_PHRASES)


def _split_sentences(text: str) -> List[str]:
    # simple, safe splitter for extracted text
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t:
        return []
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", t) if x.strip()]


def _fallback_extractive(doc_text: str) -> str:
    """
    Build strict output without LLM:
    - bullets: first 3 bullet-like lines that are not "tasky"
    - paragraph: 2–3 sentences from the intro/body that are not "tasky"
    """
    raw_lines = [ln.strip() for ln in (doc_text or "").splitlines() if ln.strip()]

    # candidate bullet lines
    bullets = []
    for ln in raw_lines:
        # normalize bullet markers
        ln2 = ln
        if ln2.startswith("•"):
            ln2 = ln2.lstrip("•").strip()
        elif ln2.startswith("-"):
            ln2 = ln2.lstrip("-").strip()
        else:
            # also accept "•" appearing after artifacts or mid-line
            if ln2.startswith("• "):
                ln2 = ln2[2:].strip()

        if ln.startswith("•") or ln.startswith("-") or "(cid:" in ln:
            if ln2 and not _is_tasky_line(ln2):
                bullets.append(ln2)
        if len(bullets) == 3:
            break

    # paragraph sentences: prefer first 2–3 non-tasky sentences
    sents = _split_sentences(doc_text)
    para_sents = []
    for s in sents:
        if not _is_tasky_line(s):
            para_sents.append(s)
        if len(para_sents) == 3:
            break

    if len(bullets) != 3 or len(para_sents) < 2:
        return "Not found in the document."

    paragraph = " ".join(para_sents[:3])
    return "BULLETS:\n- " + "\n- ".join(bullets) + "\n\nPARAGRAPH:\n" + paragraph


# -------------------------
# Ollama call (/api/chat)
# -------------------------
def _ollama_chat(system: str, user: str, timeout: float) -> str:
    url = f"{CFG.OLLAMA_URL}/api/chat"
    payload = {
        "model": CFG.GEN_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {
            "temperature": 0.0,
            "top_p": 0.9,
            "repeat_penalty": 1.15,
            "num_ctx": 4096,
            "num_predict": 260,
        },
    }

    r = requests.post(url, json=payload, timeout=timeout, headers={"Accept": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"Ollama request failed: {r.status_code} {r.text[:500]}")
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"Ollama returned non-JSON: {r.text[:500]}")
    return _clean(((data.get("message") or {}).get("content") or ""))


# -------------------------
# Endpoint
# -------------------------
@csrf_exempt
@require_POST
def upload_doc_ask(request):
    """
    POST JSON:
      { "file_id": "<uuid or pk>", "question": "Summarize this document" }

    Returns:
      { ok, answer, file_id, filename, text_len }
    """
    deny = _authz_or_error(request)
    if deny:
        return deny

    body = _json_body(request)
    file_id = _clean(body.get("file_id"))
    question = _clean(body.get("question") or body.get("message") or "Summarize this document")

    if not file_id:
        return JsonResponse({"ok": False, "error": "file_id is required"}, status=400)

    uf = _get_uploaded_file(file_id)
    if not uf:
        return JsonResponse({"ok": False, "error": f"File not found for file_id={file_id}"}, status=404)

    doc_text = (uf.extracted_text or "").strip()
    if not doc_text:
        return JsonResponse({"ok": False, "error": "Uploaded file text is empty (extracted_text is blank)."}, status=400)

    doc_text = _clean_pdf_artifacts(doc_text)[: CFG.DOC_MAX_CHARS]
    if len(doc_text) < 80:
        return JsonResponse(
            {"ok": False, "error": "Document text too small; PDF extraction may have failed.", "text_len": len(doc_text)},
            status=400,
        )

    system_prompt = (
        "You are a STRICT document-only summarizer.\n"
        "RULES (must follow):\n"
        "1) Use ONLY the provided DOCUMENT TEXT.\n"
        "2) EXTRACTIVE ONLY: reuse the document's own words/phrases; do NOT add examples or explanations.\n"
        "3) Do NOT write what the user can do, do NOT describe the system; output only document facts.\n"
        "4) Do NOT add any external terms not present in the document.\n"
        "5) Output EXACTLY ONCE in this format:\n"
        "BULLETS:\n"
        "- <fact 1 from document>\n"
        "- <fact 2 from document>\n"
        "- <fact 3 from document>\n"
        "PARAGRAPH:\n"
        "<2–3 sentences using only document text>\n"
        "6) If you cannot comply, output exactly: Not found in the document.\n"
        "7) Plain text only. No markdown headings.\n"
        "8) IMPORTANT: Do NOT use lines about uploading/testing prompts.\n"
    )

    task = (
        f"QUESTION: {question}\n\n"
        "Create an EXTRACTIVE summary using ONLY phrases/sentences from the document.\n"
        "Bullets must be FACTS stated in the document (not instructions like 'summarize' / 'extract key points').\n"
        "Output EXACTLY ONCE.\n"
    )

    user_prompt = (
        "DOCUMENT TEXT:\n"
        "--------------------\n"
        f"{doc_text}\n"
        "--------------------\n\n"
        f"{task}\n"
    )

    answer = ""
    try:
        raw1 = _ollama_chat(system_prompt, user_prompt, CFG.GEN_TIMEOUT)
        norm1 = _enforce_format_or_empty(raw1)

        if not norm1:
            system_prompt2 = system_prompt + (
                "\nSTRICT MODE:\n"
                "If you output task instructions, system descriptions, or repeat templates, you FAIL.\n"
                "Return the required format EXACTLY ONCE.\n"
            )
            raw2 = _ollama_chat(system_prompt2, user_prompt, CFG.GEN_TIMEOUT)
            norm2 = _enforce_format_or_empty(raw2)
            answer = norm2
        else:
            answer = norm1

        # ✅ Deterministic fallback if LLM still fails strict validator
        if not answer:
            answer = _fallback_extractive(doc_text)

        answer = _strict_postcheck(answer)

    except Exception as e:
        # Even on Ollama failure, we can still do deterministic extractive fallback
        fb = _fallback_extractive(doc_text)
        if fb and fb != "Not found in the document.":
            return JsonResponse(
                {
                    "ok": True,
                    "answer": fb,
                    "file_id": (getattr(uf, "file_id", None) or str(uf.id)),
                    "filename": uf.filename,
                    "text_len": len(doc_text),
                    "warning": "Ollama failed; returned deterministic extractive summary instead.",
                    "ollama_error": str(e)[:500],
                }
            )

        return JsonResponse(
            {
                "ok": False,
                "error": str(e),
                "hint": "Check OLLAMA_URL and that your Ollama supports /api/chat and the model exists.",
                "ollama_url": CFG.OLLAMA_URL,
                "gen_model": CFG.GEN_MODEL,
            },
            status=502,
        )

    return JsonResponse(
        {
            "ok": True,
            "answer": answer,
            "file_id": (getattr(uf, "file_id", None) or str(uf.id)),
            "filename": uf.filename,
            "text_len": len(doc_text),
        }
    )
