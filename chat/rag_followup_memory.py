import re
import time
from typing import Any, Dict, List, Optional


SESSION_KEY = "rag_followup_memory_v2"

MAX_CONTEXT_CHARS = 12000
MAX_ANSWER_CHARS = 5000
MAX_QUESTION_CHARS = 2000
MAX_HISTORY = 6
MAX_SOURCES = 12


FOLLOWUP_KEYWORDS = [
    # English
    "more", "details", "detail", "explain", "explain more", "continue",
    "again", "full", "summary", "summarize", "how many", "how much",
    "who", "when", "where", "why", "which", "what about", "how about",
    "this", "that", "it", "same", "above", "previous", "these", "those",
    "approval", "approve", "authority", "days", "day", "rule", "clause",

    # Bangla / mixed
    "আরও", "আরো", "বিস্তারিত", "ব্যাখ্যা", "আবার", "চালিয়ে", "চালিয়ে",
    "সম্পূর্ণ", "সারাংশ", "কত", "কত দিন", "কতদিন", "কে", "কারা",
    "কখন", "কোথায়", "কোথায়", "কেন", "কিভাবে", "কীভাবে",
    "এটা", "এইটা", "ওটা", "সেটা", "আগের", "উপরের", "একই",
    "অনুমোদন", "অনুমতি", "নিয়ম", "নিয়ম", "ধারা", "দিন",
]


COMMON_STOPWORDS = {
    "the", "is", "are", "am", "a", "an", "to", "for", "of", "in", "on",
    "and", "or", "with", "from", "by", "about", "tell", "me", "please",
    "what", "how", "why", "when", "where", "who", "which", "can", "i",
    "you", "it", "this", "that", "these", "those", "same", "previous",
    "details", "more", "explain", "policy", "rule",

    "কি", "কী", "কেন", "কখন", "কোথায়", "কোথায়", "কিভাবে", "কীভাবে",
    "বলুন", "বলেন", "আমাকে", "এই", "ওই", "সে", "এটা", "সেটা", "আরও",
    "আরো", "বিস্তারিত", "নীতি", "নিয়ম", "নিয়ম", "ধারা",
}


def clean_text(value: Any, max_chars: Optional[int] = None) -> str:
    if value is None:
        return ""

    text = str(value)
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text).strip()

    if max_chars and len(text) > max_chars:
        return text[:max_chars] + "..."

    return text


def normalize_lower(value: Any) -> str:
    return clean_text(value).lower()


def safe_sources(sources: Any) -> List[Any]:
    if not sources:
        return []

    if isinstance(sources, list):
        return sources[:MAX_SOURCES]

    return [sources]


def get_rag_memory(request) -> Dict[str, Any]:
    memory = request.session.get(SESSION_KEY, {})
    if isinstance(memory, dict):
        return memory
    return {}


def clear_rag_memory(request) -> None:
    if SESSION_KEY in request.session:
        del request.session[SESSION_KEY]
        request.session.modified = True


def extract_keywords(text: str, limit: int = 30) -> List[str]:
    text = normalize_lower(text)

    tokens = re.findall(r"[a-zA-Z0-9_]+|[\u0980-\u09FF]+", text)

    words: List[str] = []
    for token in tokens:
        token = token.strip().lower()
        if len(token) < 2:
            continue
        if token in COMMON_STOPWORDS:
            continue
        if token not in words:
            words.append(token)

    return words[:limit]


def is_generic_followup_question(question: str) -> bool:
    q = normalize_lower(question)
    if not q:
        return False

    words = q.split()

    # Very short follow-up
    if len(words) <= 8:
        for keyword in FOLLOWUP_KEYWORDS:
            if keyword.lower() in q:
                return True

    # Common Bangla/English short questions
    short_patterns = [
        r"^how many\b",
        r"^how much\b",
        r"^who\b",
        r"^when\b",
        r"^why\b",
        r"^which\b",
        r"^what about\b",
        r"^details\b",
        r"^more\b",
        r"^explain\b",
        r"কত",
        r"কত দিন",
        r"কতদিন",
        r"কে",
        r"কারা",
        r"কখন",
        r"কিভাবে",
        r"বিস্তারিত",
        r"আরও",
        r"আরো",
        r"ধারা",
    ]

    for pattern in short_patterns:
        if re.search(pattern, q):
            return True

    return False


def memory_keywords(memory: Dict[str, Any]) -> List[str]:
    joined = " ".join([
        clean_text(memory.get("active_topic")),
        clean_text(memory.get("intent")),
        clean_text(memory.get("domain")),
        clean_text(memory.get("collection")),
        clean_text(memory.get("last_question")),
        clean_text(memory.get("last_answer")),
        clean_text(memory.get("last_context")),
    ])

    return extract_keywords(joined, limit=50)


def looks_related_to_memory(question: str, memory: Dict[str, Any]) -> bool:
    q_keywords = extract_keywords(question, limit=20)
    m_keywords = memory_keywords(memory)

    if not q_keywords or not m_keywords:
        return False

    overlap = set(q_keywords).intersection(set(m_keywords))

    if overlap:
        return True

    q = normalize_lower(question)
    topic = normalize_lower(memory.get("active_topic", ""))

    if topic and topic in q:
        return True

    return False


def prepare_rag_followup(
    request,
    question: str,
    *,
    domain: str = "policy",
    collection: str = "",
    strict_topic_lock: bool = False,
) -> Dict[str, Any]:
    """
    Decides whether current question should use previous RAG memory.

    relation values:
    - new_topic: no memory or unrelated normal question
    - followup: short/generic follow-up question
    - related: related to previous topic
    - not_related: strict topic lock enabled and question is unrelated
    """

    question = clean_text(question, MAX_QUESTION_CHARS)
    memory = get_rag_memory(request)

    if not memory:
        return {
            "has_memory": False,
            "use_memory": False,
            "relation": "new_topic",
            "search_query": question,
            "memory_prompt": "",
            "memory": {},
        }

    # Do not mix different RAG modules
    old_domain = memory.get("domain")
    if old_domain and old_domain != domain:
        return {
            "has_memory": True,
            "use_memory": False,
            "relation": "new_topic",
            "search_query": question,
            "memory_prompt": "",
            "memory": memory,
        }

    generic_followup = is_generic_followup_question(question)
    related = looks_related_to_memory(question, memory)

    if generic_followup:
        relation = "followup"
        use_memory = True
    elif related:
        relation = "related"
        use_memory = True
    else:
        relation = "not_related" if strict_topic_lock else "new_topic"
        use_memory = False

    if not use_memory:
        return {
            "has_memory": True,
            "use_memory": False,
            "relation": relation,
            "search_query": question,
            "memory_prompt": "",
            "memory": memory,
        }

    active_topic = clean_text(memory.get("active_topic"))
    last_question = clean_text(memory.get("last_question"))
    last_answer = clean_text(memory.get("last_answer"), MAX_ANSWER_CHARS)
    last_context = clean_text(memory.get("last_context"), MAX_CONTEXT_CHARS)

    search_query = f"""
Active topic: {active_topic}
Previous question: {last_question}
Current follow-up question: {question}
Search only information related to the active topic and current follow-up.
""".strip()

    memory_prompt = f"""
Previous RAG memory:
Active topic: {active_topic}
Previous question: {last_question}

Previous answer:
{last_answer}

Previous retrieved source context:
{last_context}

Current user question:
{question}
""".strip()

    return {
        "has_memory": True,
        "use_memory": True,
        "relation": relation,
        "search_query": search_query,
        "memory_prompt": memory_prompt,
        "memory": memory,
    }


def save_rag_turn(
    request,
    *,
    user_question: str,
    answer: str,
    context_text: str = "",
    sources: Optional[List[Any]] = None,
    domain: str = "policy",
    collection: str = "",
    active_topic: str = "",
    intent: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    old_memory = get_rag_memory(request)
    old_history = old_memory.get("history", [])

    if not isinstance(old_history, list):
        old_history = []

    turn = {
        "question": clean_text(user_question, MAX_QUESTION_CHARS),
        "answer": clean_text(answer, MAX_ANSWER_CHARS),
        "time": int(time.time()),
    }

    history = old_history + [turn]
    history = history[-MAX_HISTORY:]

    memory = {
        "version": 2,
        "domain": domain,
        "collection": collection,
        "active_topic": clean_text(active_topic or intent or user_question, 300),
        "intent": clean_text(intent, 200),
        "last_question": clean_text(user_question, MAX_QUESTION_CHARS),
        "last_answer": clean_text(answer, MAX_ANSWER_CHARS),
        "last_context": clean_text(context_text, MAX_CONTEXT_CHARS),
        "sources": safe_sources(sources),
        "history": history,
        "saved_at": int(time.time()),
    }

    if extra:
        memory["extra"] = extra

    request.session[SESSION_KEY] = memory
    request.session.modified = True

    return memory


def build_context_from_docs(docs: Any) -> str:
    """
    Flexible Qdrant/result parser.
    Works with list of dicts, payload dicts, or objects with payload attribute.
    """

    if not docs:
        return ""

    lines: List[str] = []

    if not isinstance(docs, list):
        docs = [docs]

    for i, doc in enumerate(docs, start=1):
        text = ""

        if isinstance(doc, dict):
            payload = doc.get("payload") or doc.get("metadata") or doc

            text = (
                doc.get("text")
                or doc.get("content")
                or doc.get("page_content")
                or payload.get("text")
                or payload.get("content")
                or payload.get("answer")
                or payload.get("body")
                or ""
            )

            clause = payload.get("clause") or payload.get("section") or payload.get("topic") or ""
            source = payload.get("source") or payload.get("title") or payload.get("file") or ""

            if text:
                header_parts = []
                if source:
                    header_parts.append(f"source={source}")
                if clause:
                    header_parts.append(f"clause={clause}")

                header = " | ".join(header_parts)
                if header:
                    lines.append(f"[Source {i}] {header}\n{text}")
                else:
                    lines.append(f"[Source {i}]\n{text}")

        else:
            payload = getattr(doc, "payload", {}) or {}
            text = (
                getattr(doc, "text", "")
                or getattr(doc, "page_content", "")
                or payload.get("text", "")
                or payload.get("content", "")
            )

            if text:
                lines.append(f"[Source {i}]\n{text}")

    return clean_text("\n\n".join(lines), MAX_CONTEXT_CHARS)


def build_sources_from_docs(docs: Any) -> List[Any]:
    if not docs:
        return []

    if not isinstance(docs, list):
        docs = [docs]

    sources: List[Any] = []

    for i, doc in enumerate(docs, start=1):
        if isinstance(doc, dict):
            payload = doc.get("payload") or doc.get("metadata") or doc
            sources.append({
                "index": i,
                "source": payload.get("source") or payload.get("title") or payload.get("file") or "",
                "clause": payload.get("clause") or payload.get("section") or payload.get("topic") or "",
                "score": doc.get("score"),
            })
        else:
            payload = getattr(doc, "payload", {}) or {}
            sources.append({
                "index": i,
                "source": payload.get("source", ""),
                "clause": payload.get("clause") or payload.get("section") or payload.get("topic") or "",
                "score": getattr(doc, "score", None),
            })

    return sources[:MAX_SOURCES]


def build_rag_prompt(
    *,
    question: str,
    context_text: str,
    memory_prompt: str = "",
    domain_label: str = "policy",
    language_hint: str = "Bangla or English depending on user question",
) -> str:
    return f"""
You are an enterprise RAG assistant.

Domain:
{domain_label}

Language:
Answer in {language_hint}.

Rules:
1. Use only the provided RAG context and previous RAG memory.
2. If the current question is a follow-up, answer based on the active previous topic.
3. Do not invent policy, clause, amount, day count, approval authority, benefit, or rule.
4. If the question is related but the information is not available, clearly say the information was not found in the available context.
5. If the question is not related to the current topic, clearly say it is not related to the current topic.
6. Keep the answer clear and practical.

{memory_prompt}

Current retrieved RAG context:
{context_text}

Current user question:
{question}

Final answer:
""".strip()


def not_found_answer(question: str, active_topic: str = "", lang: str = "bn") -> str:
    q = normalize_lower(question)

    bangla_detected = bool(re.search(r"[\u0980-\u09FF]", q))
    use_bn = lang == "bn" or bangla_detected

    if use_bn:
        if active_topic:
            return f"এই তথ্যটি বর্তমান '{active_topic}' সম্পর্কিত প্রাপ্ত RAG কনটেক্সটে পাওয়া যায়নি।"
        return "এই তথ্যটি প্রাপ্ত RAG কনটেক্সটে পাওয়া যায়নি।"

    if active_topic:
        return f"This information was not found in the available RAG context for '{active_topic}'."

    return "This information was not found in the available RAG context."


def not_related_answer(active_topic: str = "", lang: str = "bn") -> str:
    if lang == "bn":
        if active_topic:
            return f"আপনার প্রশ্নটি বর্তমান '{active_topic}' আলোচনার সাথে সম্পর্কিত নয়।"
        return "আপনার প্রশ্নটি বর্তমান আলোচনার সাথে সম্পর্কিত নয়।"

    if active_topic:
        return f"This question is not related to the current topic: {active_topic}."

    return "This question is not related to the current topic."
