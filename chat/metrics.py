# chat/metrics.py
from prometheus_client import Counter, Histogram, Gauge

CHAT_REQUESTS_TOTAL = Counter(
    "chat_requests_total",
    "Total number of chat requests",
    ["endpoint", "mode", "model", "status"],
)

CHAT_REQUEST_DURATION_SECONDS = Histogram(
    "chat_request_duration_seconds",
    "End-to-end chat request duration in seconds",
    ["endpoint", "mode", "model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 60, 120),
)

LLM_REQUEST_DURATION_SECONDS = Histogram(
    "llm_request_duration_seconds",
    "LLM request duration in seconds",
    ["model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 60, 120),
)

LLM_LOAD_DURATION_SECONDS = Histogram(
    "llm_load_duration_seconds",
    "LLM model load duration in seconds",
    ["model"],
    buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 20),
)

LLM_PROMPT_TOKENS_TOTAL = Counter(
    "llm_prompt_tokens_total",
    "Total prompt tokens processed by the LLM",
    ["model"],
)

LLM_OUTPUT_TOKENS_TOTAL = Counter(
    "llm_output_tokens_total",
    "Total generated output tokens",
    ["model"],
)

LLM_REQUESTS_TOTAL = Counter(
    "llm_requests_total",
    "Total LLM calls",
    ["model", "result"],
)

RAG_SEARCH_DURATION_SECONDS = Histogram(
    "rag_search_duration_seconds",
    "RAG or memory retrieval duration in seconds",
    ["collection", "result"],
    buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10),
)

RAG_HITS_TOTAL = Counter(
    "rag_hits_total",
    "Total retrieval hits",
    ["collection"],
)

NIFI_REQUEST_DURATION_SECONDS = Histogram(
    "nifi_request_duration_seconds",
    "NiFi API request duration in seconds",
    ["route", "result"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 20, 30, 60),
)

NIFI_REQUESTS_TOTAL = Counter(
    "nifi_requests_total",
    "Total NiFi API requests",
    ["route", "result"],
)

ACTIVE_CHAT_REQUESTS = Gauge(
    "active_chat_requests",
    "Number of in-flight chat requests",
)
