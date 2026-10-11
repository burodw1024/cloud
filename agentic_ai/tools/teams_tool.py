def run(user=None, query="", params=None, language="en", **kwargs):
    """
    Placeholder for Microsoft Teams Graph integration.
    Production recommendation:
    - Start with selected channels only
    - Avoid personal chat sync at first
    - Use admin-approved Graph permissions
    """
    return {
        "ok": False,
        "source": "teams",
        "requires_setup": True,
        "answer": "Teams/Microsoft Graph tool is not configured yet. Configure selected channel sync and admin-approved Graph permissions.",
    }
