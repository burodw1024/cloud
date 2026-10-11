import json
import re
import requests
from django.conf import settings


KEYWORD_INTENTS = {
    "open_account": [
        "open account",
        "open an account",
        "create account",
        "account open",
        "অ্যাকাউন্ট খুলতে চাই",
        "একাউন্ট খুলতে চাই",
        "নতুন অ্যাকাউন্ট",
    ],
    "loan_application": [
        "apply for loan",
        "loan application",
        "apply loan",
        "loan apply",
        "want to apply for loan",
        "i want to apply for loan",
        "i need a loan",
        "need a loan",
        "take loan",
        "personal loan",
        "business loan",
        "home loan",
        "car loan",
        "ঋণ নিতে চাই",
        "লোন নিতে চাই",
        "লোনের জন্য আবেদন করতে চাই",
        "ঋণের আবেদন",
        "লোন আবেদন",
        "লোন করতে চাই",
    ],
    "human_handoff": [
        "talk to agent",
        "talk to human",
        "customer care",
        "human support",
        "speak to agent",
        "speak to human",
        "এজেন্ট চাই",
        "মানুষের সাথে কথা বলতে চাই",
    ],
}


def detect_intent_keyword(message: str):
    text = (message or "").strip().lower()
    for intent, patterns in KEYWORD_INTENTS.items():
        for p in patterns:
            if p in text:
                return intent
    return None


def detect_intent_llm(message: str):
    base_url = getattr(settings, "OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    url = f"{base_url}/api/generate"
    model = getattr(settings, "WORKFLOW_INTENT_MODEL", "aya-23-8b:q5")

    prompt = f"""
Classify the user message into one of the following intents only:
- open_account
- loan_application
- human_handoff
- general_chat

Return only valid JSON:
{{"intent": "...", "confidence": 0.0}}

User message: {message}
""".strip()

    try:
        resp = requests.post(
            url,
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=20,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json() or {}
        text = (data.get("response") or "").strip()

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None, 0.0

        parsed = json.loads(match.group(0))
        intent = (parsed.get("intent") or "").strip()
        confidence = float(parsed.get("confidence", 0.0))

        allowed = {"open_account", "loan_application", "human_handoff", "general_chat"}
        if intent not in allowed:
            return None, 0.0

        return intent, confidence
    except Exception:
        return None, 0.0


def detect_intent(message: str):
    text = (message or "").strip().lower()

    # TEMP HARD TEST
    if "loan" in text or "লোন" in text or "ঋণ" in text:
        return "loan_application"

    keyword_intent = detect_intent_keyword(message)
    if keyword_intent:
        return keyword_intent

    llm_intent, confidence = detect_intent_llm(message)
    if llm_intent and llm_intent != "general_chat" and confidence >= 0.75:
        return llm_intent

    return None
