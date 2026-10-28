# chat/dynamic_config.py

from __future__ import annotations

import os
from typing import Any

import requests
from django.apps import apps
from django.core.cache import cache

from chat.app_config import APP_CONFIG, strip_url


# Central fallback values.
# IPs remain centralized in app_config.py.
DEFAULT_SETTINGS = {
    "AGENT_PLANNER_MODEL": "",
    "AGENT_PLANNER_TIMEOUT": "60",
    "QDRANT_URL": APP_CONFIG.qdrant.qdrant_url,
    "QDRANT_PRIMARY_URL": getattr(APP_CONFIG.qdrant, "primary_url", APP_CONFIG.qdrant.qdrant_url),
    "QDRANT_SECONDARY_URL": getattr(APP_CONFIG.qdrant, "secondary_url", ""),
    "QDRANT_COLLECTION_POLICY": APP_CONFIG.qdrant.collection_policy,
    "QDRANT_COLLECTION_NOTICE": APP_CONFIG.qdrant.collection_notice,
    "QDRANT_COLLECTION_CUSTOMER_SUPPORT": APP_CONFIG.qdrant.collection_customer_support,
    "QDRANT_COLLECTION_LONG_MEMORY": APP_CONFIG.qdrant.collection_long_memory,
    "QDRANT_COLLECTION_CASHFLOW": APP_CONFIG.qdrant.collection_cashflow,
    "QDRANT_VECTOR_SIZE": str(APP_CONFIG.qdrant.vector_size),
    "QDRANT_VECTOR_NAME": APP_CONFIG.qdrant.vector_name,
    "QDRANT_TIMEOUT": str(APP_CONFIG.qdrant.timeout),

    "OLLAMA_URL": APP_CONFIG.llm.ollama_url,

    # New central model setting for the whole LLM chat application
    "OLLAMA_MODEL": APP_CONFIG.llm.gen_model,

    # Backward compatibility for old code
    "GEN_MODEL": APP_CONFIG.llm.gen_model,
    "SQL_MODEL": APP_CONFIG.llm.sql_model,
    "GEN_TIMEOUT": str(APP_CONFIG.llm.gen_timeout),

    "NIFI_BASE_URL": APP_CONFIG.nifi.nifi_base_url,
    "NIFI_CUSTOMER_SUPPORT_URL": APP_CONFIG.nifi.customer_support_url,
    "NIFI_HR_URL": APP_CONFIG.nifi.hr_url,
    "NIFI_CORE_URL": APP_CONFIG.nifi.core_url,
    "NIFI_CASHFLOW_URL": APP_CONFIG.nifi.cashflow_url,
}



# --- OPENAI_POLICY_DYNAMIC_SETTINGS_BEGIN ---
# These keys use the existing runtime priority:
# Database SystemSetting -> environment -> fallback.
DEFAULT_SETTINGS.update({
    "OPENAI_BASE_URL": os.getenv("OPENAI_BASE_URL", ""),
    "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
    "OPENAI_MODEL": os.getenv("OPENAI_MODEL", ""),
    "OPENAI_VECTOR_STORE_ID": os.getenv("OPENAI_VECTOR_STORE_ID", ""),
    "OPENAI_PREPROCESS_MODEL": os.getenv(
        "OPENAI_PREPROCESS_MODEL",
        os.getenv("OPENAI_MODEL", ""),
    ),
    "FILE_SEARCH_MAX_RESULTS": os.getenv("FILE_SEARCH_MAX_RESULTS", "8"),
    "ADMIN_MAX_UPLOAD_MB": os.getenv("ADMIN_MAX_UPLOAD_MB", "50"),
    "PREPROCESS_MAX_UPLOAD_MB": os.getenv("PREPROCESS_MAX_UPLOAD_MB", "50"),
    "PREPROCESS_BATCH_CHARS": os.getenv("PREPROCESS_BATCH_CHARS", "12000"),
    "GOOGLE_CLOUD_REGION": os.getenv("GOOGLE_CLOUD_REGION", "global"),
    "GOOGLE_TTS_MODEL": os.getenv("GOOGLE_TTS_MODEL", "gemini-2.5-flash-tts"),
    "GOOGLE_TTS_VOICE": os.getenv("GOOGLE_TTS_VOICE", "Kore"),
})
# --- OPENAI_POLICY_DYNAMIC_SETTINGS_END ---

# MCP gateway settings use the same database -> environment -> default priority.
DEFAULT_SETTINGS.update({
    "MCP_GATEWAY_ENABLED": "false",
    "MCP_WHATSAPP_TOOL": "",
    "MCP_WHATSAPP_RECIPIENTS": "[]",
    "MCP_GATEWAY_URL": "http://192.168.1.99:30444/mcp",
    "MCP_GATEWAY_TOKEN": "",
    "MCP_MODEL_PROVIDER": "ollama",
    "MCP_MODEL": "",
    "MCP_MODEL_BASE_URL": "",
    "MCP_MODEL_API_KEY": "",
    "MCP_TIMEOUT": "30",
    "MCP_REQUEST_TIMEOUT": "90",
})

URL_KEYS = {
    "QDRANT_URL",
    "QDRANT_PRIMARY_URL",
    "QDRANT_SECONDARY_URL",
    "OLLAMA_URL",
    "NIFI_BASE_URL",
    "NIFI_CUSTOMER_SUPPORT_URL",
    "NIFI_HR_URL",
    "NIFI_CORE_URL",
    "NIFI_CASHFLOW_URL",
}


def _safe_get_db_setting(key: str) -> str | None:
    """
    Read setting from database safely.
    Returns None if DB is not ready, migration not applied, or key not found.
    """
    try:
        SystemSetting = apps.get_model("chat", "SystemSetting")
        obj = (
            SystemSetting.objects
            .filter(key=key, is_active=True)
            .only("value")
            .first()
        )

        if obj and obj.value is not None:
            value = str(obj.value).strip()
            if value:
                return value

    except Exception:
        return None

    return None


def get_setting(key: str, default: Any = "") -> str:
    """
    Runtime setting resolver.

    Priority:
    1. Database dashboard value
    2. Environment variable
    3. DEFAULT_SETTINGS
    4. Given default
    """
    cache_key = f"system_setting:{key}"
    cached = cache.get(cache_key)

    if cached is not None:
        return cached

    db_value = _safe_get_db_setting(key)

    if db_value is not None:
        value = db_value
    else:
        value = (
            os.getenv(key)
            or DEFAULT_SETTINGS.get(key)
            or default
            or ""
        )

    value = str(value).strip()

    if key in URL_KEYS:
        value = strip_url(value)

    cache.set(cache_key, value, 60)
    return value


def clear_setting_cache(key: str | None = None) -> None:
    """
    Clear runtime setting cache.

    Use this after updating SystemSetting from the admin settings dashboard.
    """
    if key:
        cache.delete(f"system_setting:{key}")
        return

    keys = set(DEFAULT_SETTINGS.keys())
    keys.update(
        {
            "QDRANT_API_KEY",
            "OLLAMA_MODEL",
            "GEN_MODEL",
            "SQL_MODEL",
        }
    )

    for setting_key in keys:
        cache.delete(f"system_setting:{setting_key}")


def get_int_setting(key: str, default: int) -> int:
    try:
        return int(get_setting(key, str(default)))
    except Exception:
        return default


def get_float_setting(key: str, default: float) -> float:
    try:
        return float(get_setting(key, str(default)))
    except Exception:
        return default


def get_llm_model(default: str | None = None) -> str:
    """
    Central LLM model resolver for the whole chat application.

    Preferred key:
    - OLLAMA_MODEL

    Backward compatible fallback:
    - GEN_MODEL
    """
    fallback = default or DEFAULT_SETTINGS.get("OLLAMA_MODEL") or APP_CONFIG.llm.gen_model

    model = get_setting("OLLAMA_MODEL", "").strip()
    if model:
        return model

    model = get_setting("GEN_MODEL", "").strip()
    if model:
        return model

    return str(fallback).strip()


def get_sql_model(default: str | None = None) -> str:
    """
    Central SQL model resolver.
    Keep SQL model separate from general chat model.
    """
    fallback = default or DEFAULT_SETTINGS.get("SQL_MODEL") or APP_CONFIG.llm.sql_model
    return get_setting("SQL_MODEL", fallback)


def get_ollama_url() -> str:
    return get_setting("OLLAMA_URL", APP_CONFIG.llm.ollama_url)


def get_available_ollama_models(timeout: float = 5.0) -> list[str]:
    """
    Return models from Ollama /api/tags.

    Used by admin settings dropdown.
    If Ollama is unavailable, returns fallback models.
    """
    fallback_models = [
        get_llm_model(),
        DEFAULT_SETTINGS.get("OLLAMA_MODEL", ""),
        DEFAULT_SETTINGS.get("GEN_MODEL", ""),
        DEFAULT_SETTINGS.get("SQL_MODEL", ""),
        "aya-23-8b:q5",
        "aya-expanse:8b",
        "llama3.1:8b",
        "mistral:7b",
        "sqlcoder-7b:q4km",
    ]

    ollama_url = get_ollama_url().strip().rstrip("/")
    model_names: list[str] = []

    if ollama_url:
        try:
            response = requests.get(f"{ollama_url}/api/tags", timeout=timeout)
            response.raise_for_status()
            data = response.json()

            for item in data.get("models", []):
                name = str(item.get("name") or "").strip()
                if name and name not in model_names:
                    model_names.append(name)

        except requests.RequestException:
            pass
        except Exception:
            pass

    for model in fallback_models:
        model = str(model or "").strip()
        if model and model not in model_names:
            model_names.append(model)

    return model_names


def qdrant_headers() -> dict:
    api_key = get_setting("QDRANT_API_KEY", "")
    if api_key:
        return {"api-key": api_key}
    return {}


def is_qdrant_available(url: str, timeout: float = 3.0) -> bool:
    url = strip_url(url)

    if not url:
        return False

    try:
        response = requests.get(
            f"{url}/collections",
            headers=qdrant_headers(),
            timeout=timeout,
        )
        return response.status_code < 300
    except requests.RequestException:
        return False


def get_qdrant_url(check_health: bool = False) -> str:
    """
    Dynamic Qdrant resolver with failover.

    Candidate order:
    1. QDRANT_URL
    2. QDRANT_PRIMARY_URL
    3. QDRANT_SECONDARY_URL
    """
    qdrant_url = get_setting("QDRANT_URL", "")
    primary_url = get_setting("QDRANT_PRIMARY_URL", qdrant_url)
    secondary_url = get_setting("QDRANT_SECONDARY_URL", "")

    candidates: list[str] = []

    for url in [qdrant_url, primary_url, secondary_url]:
        url = strip_url(url)
        if url and url not in candidates:
            candidates.append(url)

    if not candidates:
        return ""

    if not check_health:
        return candidates[0]

    for url in candidates:
        if is_qdrant_available(url):
            return url

    return candidates[0]


def get_nifi_url(key: str = "NIFI_BASE_URL") -> str:
    return get_setting(key, DEFAULT_SETTINGS.get(key, ""))
