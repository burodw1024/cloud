from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

from chat.dynamic_config import get_int_setting, get_setting


POLICY_INSTRUCTIONS = """
You are the Organization Policy AI assistant.

Rules:
1. Answer policy questions using the configured File Search knowledge base.
2. Treat the indexed effective policy as the source of truth.
3. Do not invent policy rules, dates, limits, approvals, exceptions, or procedures.
4. If the policy evidence does not support an answer, clearly say that the available policy does not provide enough information.
5. For scenario questions, apply the relevant effective rule to the facts supplied by the user and explain the conclusion.
6. For follow-up questions, preserve conversational context while still grounding policy facts in File Search.
7. Reply in Bangla when the user asks mainly in Bangla; reply in English when the user asks mainly in English.
8. Text marked [RETRIEVAL_ALIASES_NOT_POLICY] is retrieval metadata only and must not be presented as an independent policy rule.
9. Prefer concise, precise answers and mention the relevant clause/source when available.
10. If an amendment/override conflicts with a base-policy rule, the amendment takes precedence. If multiple amendment versions conflict, prefer the latest explicitly identified effective version/date in the retrieved source.
""".strip()


_client = None
_client_signature: Optional[Tuple[str, str]] = None


def get_client():
    global _client, _client_signature

    api_key = get_setting("OPENAI_API_KEY", "").strip()
    base_url = get_setting("OPENAI_BASE_URL", "").strip()

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not configured. "
            "Set it in /admin-tools/settings/."
        )

    signature = (api_key, base_url)

    if _client is None or _client_signature != signature:
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url

        _client = OpenAI(**kwargs)
        _client_signature = signature

    return _client


def collect_sources(response: Any) -> List[Dict[str, str]]:
    seen = set()
    sources = []

    for item in getattr(response, "output", []) or []:
        item_type = getattr(item, "type", None)

        if item_type == "message":
            for content in getattr(item, "content", []) or []:
                for annotation in getattr(content, "annotations", []) or []:
                    if getattr(annotation, "type", None) != "file_citation":
                        continue

                    file_id = str(getattr(annotation, "file_id", "") or "")
                    filename = str(getattr(annotation, "filename", "") or "")
                    key = (file_id, filename)

                    if key not in seen:
                        seen.add(key)
                        sources.append({"file_id": file_id, "filename": filename})

        elif item_type == "file_search_call":
            for result in getattr(item, "results", []) or []:
                file_id = str(getattr(result, "file_id", "") or "")
                filename = str(getattr(result, "filename", "") or "")
                key = (file_id, filename)

                if key not in seen:
                    seen.add(key)
                    sources.append({"file_id": file_id, "filename": filename})

    return sources


def ask_policy(question: str, previous_response_id: Optional[str] = None) -> Dict[str, Any]:
    question = (question or "").strip()

    if not question:
        raise ValueError("Question is required.")

    if len(question) > 12000:
        raise ValueError("Question exceeds 12000 characters.")

    vector_store_id = get_setting("OPENAI_VECTOR_STORE_ID", "").strip()
    model = get_setting("OPENAI_MODEL", "").strip()
    max_results = max(1, min(get_int_setting("FILE_SEARCH_MAX_RESULTS", 8), 50))

    if not vector_store_id:
        raise RuntimeError(
            "OPENAI_VECTOR_STORE_ID is not configured. "
            "Set Policy Vector Store ID in /admin-tools/settings/."
        )

    if not model:
        raise RuntimeError(
            "OPENAI_MODEL is not configured. "
            "Set OpenAI Model in /admin-tools/settings/."
        )

    payload = {
        "model": model,
        "instructions": POLICY_INSTRUCTIONS,
        "input": question,
        "tools": [{
            "type": "file_search",
            "vector_store_ids": [vector_store_id],
            "max_num_results": max_results,
        }],
        "include": ["file_search_call.results"],
        "store": True,
    }

    if previous_response_id:
        payload["previous_response_id"] = previous_response_id

    result = get_client().responses.create(**payload)
    answer = (getattr(result, "output_text", "") or "").strip() or "No answer was returned."

    return {
        "response_id": result.id,
        "answer": answer,
        "sources": collect_sources(result),
    }
