from __future__ import annotations

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .zypl_cashflow_logs_agent import (
    SESSION_KEY,
    clear_logs_state,
    process_zypl_logs_agent,
)
from .zypl_cashflow_scoring_agent import (
    ZYPL_BASE_URL,
    ZYPL_MODEL_ID,
    ZYPL_PARTNER_ID,
)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def zypl_cashflow_scoring_logs_api(request):
    if request.method == "GET":
        return JsonResponse(
            {
                "ok": True,
                "module": "zypl_cashflow_scoring_logs",
                "description": "Fetch Zypl scoring logs/results for cashflow role.",
                "endpoint": f"{ZYPL_BASE_URL}/partner/{ZYPL_PARTNER_ID}/scoring_logs",
                "method": "POST",
                "examples": {
                    "show_all_between_dates": {
                        "message": "show all zypl records between 2026-05-01 and 2026-05-31"
                    },
                    "by_scoring_id": {
                        "message": "zypl result scoring_id 145"
                    },
                    "by_customer_id": {
                        "message": "zypl result customer_id 2605137949"
                    },
                    "by_client_app_id": {
                        "message": "zypl logs client_app_id APP-20260513-E2C7949B"
                    },
                    "raw_payload": {
                        "page": 1,
                        "limit": 50,
                        "mlmodel_id": ZYPL_MODEL_ID,
                        "start_date": "2026-05-01",
                        "end_date": "2026-05-31",
                        "scoring_id": 0,
                        "user_id": 0,
                        "client_app_id": "",
                        "value_filters": {
                            "customer_id": 123
                        }
                    }
                },
            },
            json_dumps_params={"ensure_ascii": False},
        )

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}

    result = process_zypl_logs_agent(request, payload)

    return JsonResponse(
        result,
        status=200 if result.get("ok") else 400,
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def zypl_cashflow_scoring_logs_reset_api(request):
    clear_logs_state(request)

    return JsonResponse(
        {
            "ok": True,
            "message": "Zypl cashflow scoring logs session reset successfully.",
            "session_key": SESSION_KEY,
        },
        json_dumps_params={"ensure_ascii": False},
    )
