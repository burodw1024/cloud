AGENT_SYSTEM_PROMPT = """
You are an enterprise AI orchestration layer.
You must not directly access databases.
You must use approved tools only.
You must respect RBAC, audit logging, and approval rules.
For data answers, numbers must come from trusted tools such as NiFi, Oracle, or Qdrant.
"""


def wants_bangla(text):
    """
    Detect whether the user requested Bangla/Bengali response.
    """
    text = (text or "").lower()

    bangla_keywords = [
        "bangla",
        "bengali",
        "in bangla",
        "answer in bangla",
        "convert into bangla",
        "translate into bangla",
        "বাংলা",
        "বাংলায়",
        "বাংলায়",
        "বাংলাতে",
        "বাংলা করে",
        "বাংলায় বলুন",
        "বাংলায় বলুন",
        "bn",
        "bn-bd",
    ]

    return any(keyword in text for keyword in bangla_keywords)


def get_language_instruction(question):
    """
    Return language rule for LLM prompt.
    """
    if wants_bangla(question):
        return """
Language instruction:
- Answer only in natural Bangla.
- Do not answer in English.
- Use simple official Bangla.
- Keep the answer short and clear.
"""

    return """
Language instruction:
- Answer in the same language as the user question.
- Keep the answer short and clear.
"""
