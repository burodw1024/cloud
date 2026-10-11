# chat/views_settings.py

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_protect

from chat.dynamic_config import DEFAULT_SETTINGS, get_setting
from chat.models import SystemSetting


MANAGED_SETTINGS = [
    {
        "key": "QDRANT_URL",
        "label": "Main Qdrant URL",
        "description": "Default Qdrant URL used by application modules.",
    },
    {
        "key": "QDRANT_PRIMARY_URL",
        "label": "Primary Qdrant URL",
        "description": "Primary Qdrant endpoint.",
    },
    {
        "key": "QDRANT_SECONDARY_URL",
        "label": "Secondary Qdrant URL",
        "description": "Secondary/failover Qdrant endpoint.",
    },
    {
        "key": "OLLAMA_URL",
        "label": "Ollama URL",
        "description": "LLM/Ollama API endpoint.",
    },
    {
        "key": "NIFI_BASE_URL",
        "label": "NiFi Base URL",
        "description": "Base NiFi endpoint.",
    },
    {
        "key": "NIFI_CUSTOMER_SUPPORT_URL",
        "label": "NiFi Customer Support URL",
        "description": "NiFi endpoint for customer support module.",
    },
    {
        "key": "NIFI_HR_URL",
        "label": "NiFi HR URL",
        "description": "NiFi endpoint for HR module.",
    },
    {
        "key": "NIFI_CORE_URL",
        "label": "NiFi Core URL",
        "description": "NiFi endpoint for core module.",
    },
    {
        "key": "NIFI_CASHFLOW_URL",
        "label": "NiFi Cashflow URL",
        "description": "NiFi endpoint for cashflow module.",
    },
    {
        "key": "QDRANT_COLLECTION_POLICY",
        "label": "Policy Collection",
        "description": "Qdrant collection for policy RAG.",
    },
    {
        "key": "QDRANT_COLLECTION_NOTICE",
        "label": "Notice Collection",
        "description": "Qdrant collection for HR notice RAG.",
    },
    {
        "key": "QDRANT_COLLECTION_CUSTOMER_SUPPORT",
        "label": "Customer Support Collection",
        "description": "Qdrant collection for customer support knowledge.",
    },
    {
        "key": "QDRANT_COLLECTION_LONG_MEMORY",
        "label": "Long Memory Collection",
        "description": "Qdrant collection for user long memory.",
    },
    {
        "key": "QDRANT_COLLECTION_CASHFLOW",
        "label": "Cashflow Collection",
        "description": "Qdrant collection for cashflow RAG.",
    },
]


@staff_member_required
@csrf_protect
def settings_dashboard(request):
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

        messages.success(request, "System URL settings updated successfully.")
        return redirect("admin_tools_settings")

    rows = []

    db_settings = {
        obj.key: obj
        for obj in SystemSetting.objects.filter(
            key__in=[item["key"] for item in MANAGED_SETTINGS]
        )
    }

    for item in MANAGED_SETTINGS:
        key = item["key"]
        db_obj = db_settings.get(key)

        if db_obj and db_obj.value:
            source = "Database"
            value = db_obj.value
        else:
            source = "Fallback"
            value = get_setting(key, DEFAULT_SETTINGS.get(key, ""))

        rows.append(
            {
                "key": key,
                "label": item["label"],
                "description": item["description"],
                "value": value,
                "source": source,
            }
        )

    return render(
        request,
        "admin/settings_dashboard.html",
        {
            "rows": rows,
        },
    )
