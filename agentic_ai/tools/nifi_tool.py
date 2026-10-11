import requests

from chat.dynamic_config import get_nifi_url


def run(user=None, query="", params=None, language="en", **kwargs):
    """
    Data Warehouse query tool.
    Uses existing dynamic config:
    NIFI_CORE_URL from DB dashboard / env / app_config.py.
    """
    nifi_core_url = get_nifi_url("NIFI_CORE_URL")

    payload = {
        "query": query,
        "params": params or {},
        "language": language,
        "requested_by": getattr(user, "username", "anonymous") if user else "anonymous",
    }

    try:
        resp = requests.post(nifi_core_url, json=payload, timeout=60)
        resp.raise_for_status()

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        return {
            "ok": True,
            "source": "nifi",
            "url": nifi_core_url,
            "data": data,
            "answer": format_nifi_answer(data, language=language),
        }

    except Exception as exc:
        return {
            "ok": False,
            "source": "nifi",
            "url": nifi_core_url,
            "error": str(exc),
            "answer": f"NiFi/Core backend error: {exc}",
        }


def format_nifi_answer(data, language="en"):
    if language == "bn":
        return {
            "summary": "NiFi/Data Warehouse থেকে ফলাফল পাওয়া গেছে।",
            "raw": data,
        }

    return {
        "summary": "Result received from NiFi/Data Warehouse.",
        "raw": data,
    }
