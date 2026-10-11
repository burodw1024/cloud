from typing import Any, Callable, Dict, List, Optional

from .rag_followup_memory import (
    build_context_from_docs,
    build_rag_prompt,
    build_sources_from_docs,
    not_found_answer,
    not_related_answer,
    prepare_rag_followup,
    save_rag_turn,
)


def answer_with_rag_followup_memory(
    request,
    *,
    question: str,
    retrieve_fn: Callable[..., Any],
    llm_fn: Callable[[str], str],
    domain: str = "policy",
    collection: str = "",
    active_topic: str = "",
    intent: str = "",
    top_k: int = 8,
    strict_topic_lock: bool = False,
    domain_label: str = "policy",
    language_hint: str = "Bangla or English depending on user question",
) -> Dict[str, Any]:
    """
    Generic RAG + follow-up memory engine.

    retrieve_fn must accept:
        retrieve_fn(query, top_k=8)

    llm_fn must accept:
        llm_fn(prompt)

    Return:
        dict ready for JsonResponse
    """

    question = (question or "").strip()

    if not question:
        return {
            "ok": False,
            "answer": "Question is empty.",
            "error": "empty_question",
        }

    followup = prepare_rag_followup(
        request,
        question,
        domain=domain,
        collection=collection,
        strict_topic_lock=strict_topic_lock,
    )

    if followup["relation"] == "not_related":
        memory = followup.get("memory") or {}
        active = memory.get("active_topic") or active_topic
        answer = not_related_answer(active_topic=active, lang="bn")

        return {
            "ok": True,
            "answer": answer,
            "followup_used_memory": False,
            "relevance": "not_related",
            "active_topic": active,
            "sources": [],
        }

    search_query = followup["search_query"]

    try:
        try:
            docs = retrieve_fn(search_query, top_k=top_k)
        except TypeError:
            docs = retrieve_fn(search_query)
    except Exception as exc:
        return {
            "ok": False,
            "answer": f"RAG retrieval error: {str(exc)}",
            "error": "retrieval_error",
        }

    context_text = build_context_from_docs(docs)
    sources = build_sources_from_docs(docs)

    # If follow-up search returns weak/no context, still allow previous memory context.
    if not context_text and followup.get("use_memory"):
        memory = followup.get("memory") or {}
        context_text = memory.get("last_context", "")
        sources = memory.get("sources", [])

    if not context_text:
        active = active_topic or intent or question
        answer = not_found_answer(question, active_topic=active, lang="bn")

        save_rag_turn(
            request,
            user_question=question,
            answer=answer,
            context_text="",
            sources=[],
            domain=domain,
            collection=collection,
            active_topic=active,
            intent=intent,
        )

        return {
            "ok": True,
            "answer": answer,
            "followup_used_memory": followup.get("use_memory", False),
            "relevance": followup.get("relation"),
            "not_found": True,
            "active_topic": active,
            "sources": [],
        }

    prompt = build_rag_prompt(
        question=question,
        context_text=context_text,
        memory_prompt=followup.get("memory_prompt", ""),
        domain_label=domain_label,
        language_hint=language_hint,
    )

    try:
        answer = llm_fn(prompt)
    except Exception as exc:
        return {
            "ok": False,
            "answer": f"LLM error: {str(exc)}",
            "error": "llm_error",
        }

    answer = str(answer or "").strip()

    if not answer:
        answer = not_found_answer(
            question,
            active_topic=active_topic or intent or question,
            lang="bn",
        )

    final_topic = (
        active_topic
        or intent
        or (followup.get("memory") or {}).get("active_topic")
        or question
    )

    save_rag_turn(
        request,
        user_question=question,
        answer=answer,
        context_text=context_text,
        sources=sources,
        domain=domain,
        collection=collection,
        active_topic=final_topic,
        intent=intent,
        extra={
            "followup_used_memory": followup.get("use_memory", False),
            "relation": followup.get("relation"),
        },
    )

    return {
        "ok": True,
        "answer": answer,
        "followup_used_memory": followup.get("use_memory", False),
        "relevance": followup.get("relation"),
        "not_found": False,
        "active_topic": final_topic,
        "sources": sources,
    }
