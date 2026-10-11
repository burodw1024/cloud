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
    "GEN_MODEL": APP_CONFIG.llm.gen_model,
    "SQL_MODEL": APP_CONFIG.llm.sql_model,
    "GEN_TIMEOUT": str(APP_CONFIG.llm.gen_timeout),

    "NIFI_BASE_URL": APP_CONFIG.nifi.nifi_base_url,
    "NIFI_CUSTOMER_SUPPORT_URL": APP_CONFIG.nifi.customer_support_url,
    "NIFI_HR_URL": APP_CONFIG.nifi.hr_url,
    "NIFI_CORE_URL": APP_CONFIG.nifi.core_url,
    "NIFI_CASHFLOW_URL": APP_CONFIG.nifi.cashflow_url,
}


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


def get_ollama_url() -> str:
    return get_setting("OLLAMA_URL", APP_CONFIG.llm.ollama_url)


def get_nifi_url(key: str = "NIFI_BASE_URL") -> str:
    return get_setting(key, DEFAULT_SETTINGS.get(key, ""))
