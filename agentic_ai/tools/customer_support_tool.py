def run(user=None, query="", params=None, language="en", **kwargs):
    """
    Wrapper around existing customer support module.
    """
    try:
        try:
            from chat.customer_support_handler import handle_customer_support_query
            result = handle_customer_support_query(query=query, user=user)
        except Exception:
            try:
                from chat.customer_support_router import route_customer_support_query
                result = route_customer_support_query(query)
            except Exception:
                result = {
                    "message": "Customer support module found but callable function name may need mapping."
                }

        return {
            "ok": True,
            "source": "customer_support_knowledge",
            "data": result,
            "answer": result,
        }

    except Exception as exc:
        return {
            "ok": False,
            "source": "customer_support_knowledge",
            "error": str(exc),
            "answer": f"Customer support tool error: {exc}",
        }
