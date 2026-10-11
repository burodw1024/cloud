def run(user=None, query="", params=None, language="en", **kwargs):
    """
    Wrapper around HR Notice RAG.
    Update import paths below if your actual service function has another name.
    """
    try:
        try:
            from hr_notice_rag.services import search_notice_knowledge
            result = search_notice_knowledge(query=query, user=user)
        except Exception:
            try:
                from hr_notice_rag.qdrant_indexer import search_notice_context
                result = search_notice_context(query)
            except Exception:
                result = {
                    "message": "Notice module found but callable function name may need mapping."
                }

        return {
            "ok": True,
            "source": "hr_notices",
            "data": result,
            "answer": result,
        }

    except Exception as exc:
        return {
            "ok": False,
            "source": "hr_notices",
            "error": str(exc),
            "answer": f"Notice tool error: {exc}",
        }
