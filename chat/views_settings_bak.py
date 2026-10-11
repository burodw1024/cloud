# chat/views_settings.py

from functools import wraps

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect

from chat.dynamic_config import (
    DEFAULT_SETTINGS,
    clear_setting_cache,
    get_available_ollama_models,
    get_setting,
)
from chat.models import SystemSetting


ROOT_SETTINGS_USERNAME = "root_123"


def root_settings_user_required(view_func):
    """
    Allow access only to the sensitive URL settings page owner.

    Only this user can access:
    - /admin-tools/settings/
    - URL settings dashboard
    - Qdrant/Ollama/NiFi sensitive settings

    Other staff/admin users will receive 403 Forbidden.
    """

    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        user = request.user

        if not user.is_authenticated:
            return redirect_to_login(
                request.get_full_path(),
                login_url=reverse("admin:login"),
            )

        if not (
            user.is_active
            and user.is_staff
            and user.username == ROOT_SETTINGS_USERNAME
        ):
            raise PermissionDenied(
                "You are not allowed to access sensitive system URL settings."
            )

        return view_func(request, *args, **kwargs)

    return _wrapped_view


MANAGED_SETTINGS = [
    {
        "key": "QDRANT_URL",
        "label": "Main Qdrant URL",
        "description": "Default Qdrant URL used by application modules.",
        "input_type": "text",
    },
    {
        "key": "QDRANT_PRIMARY_URL",
        "label": "Primary Qdrant URL",
        "description": "Primary Qdrant endpoint.",
        "input_type": "text",
    },
    {
        "key": "QDRANT_SECONDARY_URL",
        "label": "Secondary Qdrant URL",
        "description": "Secondary/failover Qdrant endpoint.",
        "input_type": "text",
    },
    {
        "key": "OLLAMA_URL",
        "label": "Ollama URL",
        "description": "LLM/Ollama API endpoint.",
        "input_type": "text",
    },
    {
        "key": "OLLAMA_MODEL",
        "label": "Ollama LLM Model",
        "description": "Default Ollama model used by the whole LLM chat application.",
        "input_type": "select",
    },
    {
        "key": "NIFI_BASE_URL",
        "label": "NiFi Base URL",
        "description": "Base NiFi endpoint.",
        "input_type": "text",
    },
    {
        "key": "NIFI_CUSTOMER_SUPPORT_URL",
        "label": "NiFi Customer Support URL",
        "description": "NiFi endpoint for customer support module.",
        "input_type": "text",
    },
    {
        "key": "NIFI_HR_URL",
        "label": "NiFi HR URL",
        "description": "NiFi endpoint for HR module.",
        "input_type": "text",
    },
    {
        "key": "NIFI_CORE_URL",
        "label": "NiFi Core URL",
        "description": "NiFi endpoint for core module.",
        "input_type": "text",
    },
    {
        "key": "NIFI_CASHFLOW_URL",
        "label": "NiFi Cashflow URL",
        "description": "NiFi endpoint for cashflow module.",
        "input_type": "text",
    },
    {
        "key": "QDRANT_COLLECTION_POLICY",
        "label": "Policy Collection",
        "description": "Qdrant collection for policy RAG.",
        "input_type": "text",
    },
    {
        "key": "QDRANT_COLLECTION_NOTICE",
        "label": "Notice Collection",
        "description": "Qdrant collection for HR notice RAG.",
        "input_type": "text",
    },
    {
        "key": "QDRANT_COLLECTION_CUSTOMER_SUPPORT",
        "label": "Customer Support Collection",
        "description": "Qdrant collection for customer support knowledge.",
        "input_type": "text",
    },
    {
        "key": "QDRANT_COLLECTION_LONG_MEMORY",
        "label": "Long Memory Collection",
        "description": "Qdrant collection for user long memory.",
        "input_type": "text",
    },
    {
        "key": "QDRANT_COLLECTION_CASHFLOW",
        "label": "Cashflow Collection",
        "description": "Qdrant collection for cashflow RAG.",
        "input_type": "text",
    },
]


@root_settings_user_required
@csrf_protect
def settings_dashboard(request):
    managed_keys = [item["key"] for item in MANAGED_SETTINGS]

    if request.method == "POST":
        for item in MANAGED_SETTINGS:
            key = item["key"]
            value = (request.POST.get(key) or "").strip()

            SystemSetting.objects.update_or_create(
                key=key,
                defaults={
                    "value": value,
                    "description": item.get("description", ""),
                    "is_active": True,
                },
            )

        # Keep GEN_MODEL synced for old modules that still read GEN_MODEL.
        ollama_model = (request.POST.get("OLLAMA_MODEL") or "").strip()
        if ollama_model:
            SystemSetting.objects.update_or_create(
                key="GEN_MODEL",
                defaults={
                    "value": ollama_model,
                    "description": "Backward-compatible alias for OLLAMA_MODEL.",
                    "is_active": True,
                },
            )

        clear_setting_cache()

        messages.success(request, "System settings updated successfully.")
        return redirect("admin_tools_settings")

    rows = []

    db_settings = {
        obj.key: obj
        for obj in SystemSetting.objects.filter(key__in=managed_keys)
    }

    ollama_models = get_available_ollama_models()

    for item in MANAGED_SETTINGS:
        key = item["key"]
        db_obj = db_settings.get(key)

        if db_obj and db_obj.value:
            source = "Database"
            value = db_obj.value
        else:
            source = "Fallback"
            value = get_setting(key, DEFAULT_SETTINGS.get(key, ""))

        choices = []
        if key == "OLLAMA_MODEL":
            choices = ollama_models
            if value and value not in choices:
                choices.insert(0, value)

        rows.append(
            {
                "key": key,
                "label": item["label"],
                "description": item["description"],
                "value": value,
                "source": source,
                "input_type": item.get("input_type", "text"),
                "choices": choices,
            }
        )

    return render(
        request,
        "admin/settings_dashboard.html",
        {
            "rows": rows,
        },
    )
