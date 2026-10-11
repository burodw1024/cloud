import os
import requests


QDRANT_URL = os.getenv("QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")
COLLECTION = os.getenv("CUSTOMER_SUPPORT_COLLECTION", "customer_support_knowledge")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")

# Your Qdrant collection uses named vector:
# {
#   "default": {
#     "size": 768,
#     "distance": "Cosine"
#   }
# }
VECTOR_NAME = os.getenv("CUSTOMER_SUPPORT_VECTOR_NAME", "default")


INTENT_CATEGORY_MAP = {
    "loan_application": "loan_application",
    "loan_eligibility": "loan_eligibility",
    "payment_method_info": "payment_method_info",
    "installment_payment_process": "payment_method_info",
    "account_closing": "account_closing",
    "required_documents": "required_documents",
    "member_admission": "required_documents",
    "branch_info": "branch_info",
    "branch_contact": "branch_info",
    "ticket_create": "ticket_create",
    "complaint": "ticket_create",
    "customer_complaint": "ticket_create",
    "general_faq": None,
}


def get_embedding(text: str):
    """
    Generate embedding from Ollama.
    """
    response = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={
            "model": EMBED_MODEL,
            "prompt": text,
        },
        timeout=60,
    )
    response.raise_for_status()

    data = response.json()
    embedding = data.get("embedding", [])

    if not embedding:
        raise RuntimeError("Ollama returned empty embedding.")

    return embedding


def build_qdrant_filter(intent: str):
    """
    Build Qdrant payload filter using the inserted payload field: intent.

    Example Qdrant payload field:
        intent = loan_application
        intent = loan_eligibility
        intent = payment_method_info
        intent = account_closing
        intent = required_documents
        intent = branch_info
        intent = ticket_create
    """
    mapped_intent = INTENT_CATEGORY_MAP.get(intent, intent)

    if not mapped_intent:
        return None

    return {
        "must": [
            {
                "key": "intent",
                "match": {
                    "value": mapped_intent,
                },
            }
        ]
    }


def build_search_vector(vector):
    """
    Your Qdrant collection uses named vector: default.

    Correct Qdrant search format:
        "vector": {
            "name": "default",
            "vector": [...]
        }
    """
    return {
        "name": VECTOR_NAME,
        "vector": vector,
    }


def search_customer_support(question: str, intent: str = "general_faq", limit: int = 5):
    """
    Search customer support knowledge from Qdrant.
    """
    vector = get_embedding(question)
    query_filter = build_qdrant_filter(intent)

    payload = {
        "vector": build_search_vector(vector),
        "limit": limit,
        "with_payload": True,
        "with_vector": False,
    }

    if query_filter:
        payload["filter"] = query_filter

    response = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
        json=payload,
        timeout=60,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Qdrant search failed: HTTP {response.status_code}. Body: {response.text[:1000]}"
        )

    data = response.json()

    results = []

    for item in data.get("result", []):
        payload_data = item.get("payload", {}) or {}

        results.append(
            {
                "score": item.get("score", 0),
                "topic_id": payload_data.get("topic_id"),
                "topic_bn": payload_data.get("topic_bn"),
                "topic_en": payload_data.get("topic_en"),
                "intent": payload_data.get("intent"),
                "domain": payload_data.get("domain"),
                "module": payload_data.get("module"),
                "source_type": payload_data.get("source_type"),
                "source_file": payload_data.get("source_file"),
                "language": payload_data.get("language"),
                "keywords_en": payload_data.get("keywords_en"),
                "keywords_bn": payload_data.get("keywords_bn"),
                "text": payload_data.get("text"),
            }
        )

    return results


def build_context(results):
    """
    Build text context for LLM prompt from Qdrant search results.
    """
    context_parts = []

    for idx, item in enumerate(results, start=1):
        topic = item.get("topic_bn") or item.get("topic_en") or item.get("topic_id")
        text = item.get("text") or ""

        context_parts.append(
            f"[Source {idx}]\n"
            f"Topic: {topic}\n"
            f"Intent: {item.get('intent')}\n"
            f"Score: {item.get('score')}\n"
            f"Source File: {item.get('source_file')}\n"
            f"Keywords EN: {item.get('keywords_en')}\n"
            f"Keywords BN: {item.get('keywords_bn')}\n"
            f"Text: {text}\n"
        )

    return "\n".join(context_parts)


def search_customer_support_knowledge(question: str, intent: str = "general_faq", limit: int = 5):
    """
    Alias function used by customer_support_handler.py.
    """
    return search_customer_support(
        question=question,
        intent=intent,
        limit=limit,
    )


def build_context_from_docs(docs):
    """
    Alias function used by customer_support_handler.py.
    """
    return build_context(docs)
