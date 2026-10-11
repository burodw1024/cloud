def parse_change_command(text: str):
    t = (text or "").strip().lower()
    mapping = {
        "name": "full_name",
        "full name": "full_name",
        "mobile": "mobile",
        "phone": "mobile",
        "nid": "nid",
        "address": "address",
        "account type": "account_type",
    }
    for k, v in mapping.items():
        if f"change {k}" in t or f"edit {k}" in t:
            return v
    return None


def is_resume_command(text: str):
    t = (text or "").strip().lower()
    return t in {"resume", "continue", "চালিয়ে যান", "চালাও"}


def is_cancel_command(text: str):
    t = (text or "").strip().lower()
    return t in {"cancel", "stop", "না লাগবে", "বন্ধ", "বাতিল"}


def is_handoff_request(text: str):
    t = (text or "").strip().lower()
    patterns = [
        "talk to human",
        "talk to agent",
        "customer care",
        "support agent",
        "human",
        "এজেন্ট চাই",
        "মানুষের সাথে কথা বলবো",
    ]
    return any(p in t for p in patterns)
