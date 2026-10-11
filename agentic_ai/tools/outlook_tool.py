def run(user=None, query="", params=None, language="en", **kwargs):
    """
    Placeholder for Microsoft Graph Outlook integration.
    Production recommendation:
    - Search email
    - Summarize email
    - Draft only, do not auto-send
    - Require approval before sending
    """
    return {
        "ok": False,
        "source": "outlook",
        "requires_setup": True,
        "answer": "Outlook/Microsoft Graph tool is not configured yet. Configure Azure App Registration, tenant ID, client ID, secret/cert, and required Graph permissions.",
    }
