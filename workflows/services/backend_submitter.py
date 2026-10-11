import requests
from django.conf import settings


def submit_open_account_to_backend(workflow, payload: dict):
    url = getattr(settings, "WORKFLOW_OPEN_ACCOUNT_API", None)
    if not url:
        return {"ok": True, "reference": None, "message": "Backend API not configured"}

    try:
        resp = requests.post(
            url,
            json={
                "workflow_id": workflow.id,
                "session_id": workflow.session_id,
                "intent_name": workflow.intent_name,
                "data": payload,
            },
            timeout=30,
        )
        resp.raise_for_status()

        try:
            body = resp.json()
        except Exception:
            body = {}

        return {
            "ok": True,
            "reference": body.get("reference_id") or body.get("request_id"),
            "response": body,
        }
    except Exception as e:
        return {
            "ok": False,
            "reference": None,
            "error": str(e),
        }
