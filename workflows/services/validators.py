import re
from typing import List


def validate_text(value: str):
    value = (value or "").strip()
    if not value:
        return False, "This field cannot be empty."
    return True, value


def validate_mobile(value: str):
    value = (value or "").strip().replace(" ", "")
    if not re.fullmatch(r"01[3-9]\d{8}", value):
        return False, "Please enter a valid 11-digit mobile number."
    return True, value


def validate_nid(value: str):
    value = (value or "").strip().replace(" ", "")
    if not re.fullmatch(r"\d{10}|\d{13}|\d{17}", value):
        return False, "Please enter a valid NID number."
    return True, value


def validate_choice(value: str, choices: List[str]):
    cleaned = (value or "").strip().lower()
    synonyms = {
        "saving": "savings",
        "savings": "savings",
        "সেভিংস": "savings",
        "current": "current",
        "কারেন্ট": "current",
    }
    normalized = synonyms.get(cleaned, cleaned)
    if normalized not in choices:
        return False, f"Allowed values: {', '.join(choices)}"
    return True, normalized


def validate_confirm(value: str):
    cleaned = (value or "").strip().lower()
    yes_values = {"yes", "y", "হ্যাঁ", "ok", "ঠিক"}
    no_values = {"no", "n", "না"}

    if cleaned in yes_values:
        return True, True
    if cleaned in no_values:
        return True, False
    return False, "Please reply with yes or no."
