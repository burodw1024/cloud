# chat/app_config.py

from __future__ import annotations

import os
from dataclasses import dataclass


def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except Exception:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "y", "on")


def strip_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


@dataclass(frozen=True)
class LLMConfig:
    ollama_url: str = strip_url(env_str("OLLAMA_URL", "http://ollama.ollama.svc.cluster.local:11434"))
    gen_model: str = env_str("GEN_MODEL", "aya-23-8b:q5")
    sql_model: str = env_str("SQL_MODEL", "sqlcoder-7b:q4km")
    gen_timeout: float = env_float("GEN_TIMEOUT", 600.0)
    temperature: float = env_float("LLM_TEMPERATURE", 0.2)
    top_p: float = env_float("LLM_TOP_P", 0.85)
    repeat_penalty: float = env_float("LLM_REPEAT_PENALTY", 1.2)
    num_ctx: int = env_int("LLM_NUM_CTX", 4096)
    num_predict: int = env_int("LLM_NUM_PREDICT", 220)


@dataclass(frozen=True)
class QdrantConfig:
    qdrant_url: str = strip_url(env_str("QDRANT_URL", "http://qdrant.qdrant.svc.cluster.local:6333"))
    api_key: str = env_str("QDRANT_API_KEY", "")
    timeout: float = env_float("QDRANT_TIMEOUT", 30.0)

    collection_policy: str = env_str("QDRANT_COLLECTION_POLICY", "buro_policy")
    collection_notice: str = env_str("QDRANT_COLLECTION_NOTICE", "hr_notices")
    collection_customer_support: str = env_str("QDRANT_COLLECTION_CUSTOMER_SUPPORT", "customer_support_knowledge")
    collection_long_memory: str = env_str("QDRANT_COLLECTION_LONG_MEMORY", "user_long_memory")
    collection_cashflow: str = env_str("QDRANT_COLLECTION_CASHFLOW", "dw_cashflow")

    vector_size: int = env_int("QDRANT_VECTOR_SIZE", 768)
    vector_name: str = env_str("QDRANT_VECTOR_NAME", "default")


@dataclass(frozen=True)
class NiFiConfig:
    nifi_base_url: str = strip_url(env_str("NIFI_BASE_URL", "http://nifi.nifi.svc.cluster.local:8080"))
    timeout: float = env_float("NIFI_TIMEOUT", 60.0)

    customer_support_url: str = strip_url(
        env_str("NIFI_CUSTOMER_SUPPORT_URL", "http://nifi.nifi.svc.cluster.local:8080/core/query")
    )
    hr_url: str = strip_url(
        env_str("NIFI_HR_URL", "http://nifi.nifi.svc.cluster.local:8080/hr/query")
    )
    core_url: str = strip_url(
        env_str("NIFI_CORE_URL", "http://nifi.nifi.svc.cluster.local:8080/core/query")
    )
    cashflow_url: str = strip_url(
        env_str("NIFI_CASHFLOW_URL", "http://nifi.nifi.svc.cluster.local:8080/cashflow/query")
    )


@dataclass(frozen=True)
class AppSecurityConfig:
    enforce_staff_only: bool = env_bool("ENFORCE_STAFF_ONLY", True)
    allow_anon_chat: bool = env_bool("ALLOW_ANON_CHAT", True)
    allow_anon_public: bool = env_bool("ALLOW_ANON_PUBLIC", True)


@dataclass(frozen=True)
class MemoryConfig:
    stm_limit: int = env_int("STM_LIMIT", 10)
    ltm_top_k: int = env_int("LTM_TOP_K", 5)


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig = LLMConfig()
    qdrant: QdrantConfig = QdrantConfig()
    nifi: NiFiConfig = NiFiConfig()
    security: AppSecurityConfig = AppSecurityConfig()
    memory: MemoryConfig = MemoryConfig()


APP_CONFIG = AppConfig()
