import json
import os
from typing import Any, Dict, List, Optional

import requests
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST


LAND_GROUP_NAME = os.getenv("LAND_GROUP_NAME", "land_user")

QDRANT_URL = os.getenv("LAND_QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")
COLLECTION_NAME = os.getenv("LAND_COLLECTION_NAME", "bhumipedia_land_laws")

OLLAMA_URL = os.getenv("LAND_OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
EMBED_MODEL = os.getenv("LAND_EMBED_MODEL", "nomic-embed-text")
LLM_MODEL = os.getenv("LAND_LLM_MODEL", "aya-expanse:8b")

TOP_K = int(os.getenv("LAND_TOP_K", "6"))


BAD_TERMS = [
    "food times",
    "fssai",
    "food safety and standards authority of india",
    "ram nath kovind",
    "land acquisition act, 1894",
    "gazette of india",
    "<eos_token>",
]


def land_has_access(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name=LAND_GROUP_NAME).exists()


def json_error(message: str, status: int = 400):
    return JsonResponse(
        {"ok": False, "error": message},
        status=status,
        json_dumps_params={"ensure_ascii": False},
    )


def embed_text(text: str) -> List[float]:
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": [text]},
            timeout=120,
        )
        if r.status_code == 200:
            data = r.json()
            embs = data.get("embeddings")
            if embs:
                return embs[0]
    except Exception:
        pass

    r = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def search_qdrant(question: str) -> List[Dict[str, Any]]:
    vector = embed_text(question)

    r = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points/search",
        json={
            "vector": vector,
            "limit": TOP_K,
            "with_payload": True,
            "with_vector": False,
        },
        timeout=120,
    )

    if r.status_code >= 400:
        raise RuntimeError(f"Qdrant error: {r.status_code} {r.text}")

    return r.json().get("result", [])


def scroll_by_source_file(source_file: str, limit: int = 5) -> List[Dict[str, Any]]:
    r = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points/scroll",
        json={
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
            "filter": {
                "must": [
                    {
                        "key": "source_file",
                        "match": {
                            "value": source_file
                        }
                    }
                ]
            }
        },
        timeout=120,
    )

    if r.status_code >= 400:
        raise RuntimeError(f"Qdrant scroll error: {r.status_code} {r.text}")

    points = r.json().get("result", {}).get("points", [])

    results = []
    for p in points:
        results.append(
            {
                "id": p.get("id"),
                "score": 1.0,
                "payload": p.get("payload") or {},
            }
        )

    return results


def is_1993_acquisition_amendment_question(question: str) -> bool:
    q = (question or "").lower()
    return (
        ("1993" in q or "১৯৯৩" in question)
        and ("acquisition" in q or "অধিগ্রহণ" in question)
        and ("amendment" in q or "সংশোধন" in question or "পরিবর্তন" in question)
    )


def direct_answer_if_possible(question: str) -> Optional[Dict[str, Any]]:
    if not is_1993_acquisition_amendment_question(question):
        return None

    source_file = "The Acquisition and Requisition of Immovable Property (Amendment) Ordinance, 1993.pdf"
    results = scroll_by_source_file(source_file, limit=3)

    if not results:
        return None

    payload = results[0].get("payload") or {}

    answer = (
        "১৯৯৩ সালের The Acquisition and Requisition of Immovable Property "
        "(Amendment) Ordinance, 1993 অনুযায়ী ১৯৮২ সালের Acquisition and "
        "Requisition of Immovable Property Ordinance-এর section 8(2)-এ "
        "“twenty per centum” শব্দগুলোর পরিবর্তে “fifty per centum” বসানো হয়েছে।\n\n"
        "Source:\n"
        f"- Law: {payload.get('law_name')}\n"
        f"- File: {payload.get('source_file')}\n"
        f"- Page: {payload.get('page')}\n"
        f"- Section: {payload.get('section')}\n"
        f"- Status: {payload.get('status')}"
    )

    return {
        "answer": answer,
        "results": results,
        "llm_error": None,
        "mode": "direct_document_answer",
    }


def build_context(results: List[Dict[str, Any]]) -> str:
    blocks = []

    for i, item in enumerate(results, start=1):
        p = item.get("payload") or {}
        text = (p.get("text") or "").strip()

        if len(text) > 1600:
            text = text[:1600] + "..."

        blocks.append(
            f"""[Source {i}]
Score: {item.get("score")}
Law name: {p.get("law_name", "")}
Source file: {p.get("source_file", "")}
Page: {p.get("page", "")}
Section: {p.get("section", "")}
Status: {p.get("status", "")}

Text:
{text}
"""
        )

    return "\n\n".join(blocks)


def fallback_extractive_answer(question: str, results: List[Dict[str, Any]]) -> str:
    if not results:
        return "এই তথ্য প্রদত্ত ডকুমেন্টে পাওয়া যায়নি।"

    lines = [
        "LLM output blocked because it produced unrelated content.",
        "",
        "Retrieved document-based information:",
        "",
    ]

    for i, item in enumerate(results[:3], start=1):
        p = item.get("payload") or {}
        text = (p.get("text") or "").strip()[:700]

        lines.append(f"Source {i}:")
        lines.append(f"- Law: {p.get('law_name')}")
        lines.append(f"- File: {p.get('source_file')}")
        lines.append(f"- Page: {p.get('page')}")
        lines.append(f"- Section: {p.get('section')}")
        lines.append(f"- Status: {p.get('status')}")
        lines.append("")
        lines.append(text)
        lines.append("")

    return "\n".join(lines).strip()


def generate_answer(question: str, results: List[Dict[str, Any]]) -> str:
    context = build_context(results)

    prompt = f"""
You are a Bangladesh land-law RAG assistant.

STRICT RULES:
1. Use ONLY the retrieved context.
2. Do NOT use outside knowledge.
3. Do NOT mention India, FSSAI, Food Times, Land Acquisition Act 1894, or unrelated web/news content unless they appear in retrieved context.
4. If the answer is not in retrieved context, say exactly: "এই তথ্য প্রদত্ত ডকুমেন্টে পাওয়া যায়নি।"
5. Always mention source law name, page number, section if available, and status if available.
6. If status is Repealed, clearly mention that the source law is repealed.
7. Do not give legal advice. Give document-based information only.
8. Answer in Bangla unless the user asks in English.

Question:
{question}

Retrieved context:
{context}

Final answer:
""".strip()

    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": LLM_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.0,
                "num_ctx": 8192,
                "stop": ["<EOS_TOKEN>"],
            },
        },
        timeout=300,
    )

    if r.status_code >= 400:
        raise RuntimeError(f"Ollama LLM error: {r.status_code} {r.text}")

    answer = (r.json().get("response") or "").strip()

    low = answer.lower()
    if any(term in low for term in BAD_TERMS):
        return fallback_extractive_answer(question, results)

    return answer


def make_sources(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sources = []

    for item in results:
        p = item.get("payload") or {}
        sources.append(
            {
                "score": item.get("score"),
                "law_name": p.get("law_name"),
                "source_file": p.get("source_file"),
                "page": p.get("page"),
                "section": p.get("section"),
                "status": p.get("status"),
                "text_preview": (p.get("text") or "")[:700],
            }
        )

    return sources


@login_required(login_url="/login/")
def land_law_page(request):
    if not land_has_access(request.user):
        return render(
            request,
            "land_law/forbidden.html",
            {"required_group": LAND_GROUP_NAME},
            status=403,
        )

    return render(
        request,
        "land_law/page.html",
        {
            "collection_name": COLLECTION_NAME,
            "qdrant_url": QDRANT_URL,
            "ollama_url": OLLAMA_URL,
            "embed_model": EMBED_MODEL,
            "llm_model": LLM_MODEL,
        },
    )


@login_required(login_url="/login/")
@require_POST
def land_law_ask(request):
    if not land_has_access(request.user):
        return json_error(f"Permission denied. Required group: {LAND_GROUP_NAME}", status=403)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return json_error("Invalid JSON body.", status=400)

    question = (data.get("question") or "").strip()
    if not question:
        return json_error("Question is required.", status=400)

    try:
        direct = direct_answer_if_possible(question)
        if direct:
            return JsonResponse(
                {
                    "ok": True,
                    "question": question,
                    "answer": direct["answer"],
                    "sources": make_sources(direct["results"]),
                    "llm_error": direct["llm_error"],
                    "mode": direct["mode"],
                },
                json_dumps_params={"ensure_ascii": False},
            )

        results = search_qdrant(question)

        llm_error = None
        try:
            answer = generate_answer(question, results)
        except Exception as e:
            llm_error = str(e)
            answer = fallback_extractive_answer(question, results)

        return JsonResponse(
            {
                "ok": True,
                "question": question,
                "answer": answer,
                "sources": make_sources(results),
                "llm_error": llm_error,
                "mode": "rag_llm_or_extractive_fallback",
            },
            json_dumps_params={"ensure_ascii": False},
        )

    except Exception as e:
        return json_error(str(e), status=500)
