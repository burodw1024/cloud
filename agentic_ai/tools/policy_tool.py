def run(user=None, query="", params=None, language="en", **kwargs):
    """
    Wrapper around your existing policy module.
    Update import paths below if your function names are different.
    """
    try:
        try:
            from chat.policy_retrieval import search_policy
            result = search_policy(query)
        except Exception:
            try:
                from chat.policy_topic_router import route_policy_query
                result = route_policy_query(query)
            except Exception:
                result = {
                    "message": "Policy module found but callable function name may need mapping."
                }

        return {
            "ok": True,
            "source": "buro_policy",
            "data": result,
            "answer": result,
        }

    except Exception as exc:
        return {
            "ok": False,
            "source": "buro_policy",
            "error": str(exc),
            "answer": f"Policy tool error: {exc}",
        }
