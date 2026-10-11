from __future__ import annotations

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .zypl_cashflow_scoring_agent import (
    SESSION_KEY,
    SYSTEM_DEFAULT_EXTRA_FIELDS,
    USER_REQUIRED_REQUEST_FIELDS,
    ZYPL_ALLOWED_ROLES,
    ZYPL_BASE_URL,
    ZYPL_MODEL_ID,
    ZYPL_PARTNER_ID,
    ZYPL_PASSWORD,
    ZYPL_REQUIRE_LOGIN,
    ZYPL_USERNAME,
    clear_agent_state,
    mask_secret,
    process_zypl_agent,
)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def zypl_cashflow_score_agent_api(request):
    if request.method == "GET":
        return JsonResponse(
            {
                "ok": True,
                "module": "zypl_cashflow_scoring_agent",
                "description": "Cashflow-role protected Zypl agentic scoring service.",
                "endpoint": f"{ZYPL_BASE_URL}/partner/{ZYPL_PARTNER_ID}/model/{ZYPL_MODEL_ID}/score",
                "access_control": {
                    "require_login": ZYPL_REQUIRE_LOGIN,
                    "allowed_roles_or_groups": ZYPL_ALLOWED_ROLES,
                },
                "config": {
                    "username": ZYPL_USERNAME,
                    "password": mask_secret(ZYPL_PASSWORD),
                    "partner_id": ZYPL_PARTNER_ID,
                    "model_id": ZYPL_MODEL_ID,
                },
                "user_must_provide": ["Loan Application ID"] + USER_REQUIRED_REQUEST_FIELDS,
                "system_default_extra_fields": SYSTEM_DEFAULT_EXTRA_FIELDS,
                "payload_shape": [
                    {
                        "client_app_id": "Loan Application ID",
                        "group_scoring": False,
                        "request_data": {
                            "USER_STEP_1_FIELDS": "user-provided",
                            "SYSTEM_EXTRA_FIELDS": "default values",
                        },
                    }
                ],
                "post_examples": {
                    "chat_start": {
                        "message": "zypl score client"
                    },
                    "full_json": {
                        "loan_application_id": "APP-2026-001",
                        "request_data": {
                            "total_wealth": 10000,
                            "monthly_payment": 1000,
                            "interest_rate": 12,
                            "loan_amount": 10000,
                            "duration_at_disbursement_months": 12,
                            "age": 34,
                            "quantity_of_prior_loans": 3,
                            "quantity_of_dependants": 5,
                            "branch": "branch_other",
                            "location": "district_other",
                            "product_name": "Agriculture Loan",
                            "type_of_product": "product_1.01",
                            "gender": "Female"
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

    result = process_zypl_agent(request, payload)

    return JsonResponse(
        result,
        status=200 if result.get("ok") else 400,
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def zypl_cashflow_score_reset_api(request):
    clear_agent_state(request)

    return JsonResponse(
        {
            "ok": True,
            "message": "Zypl cashflow scoring session reset successfully.",
            "session_key": SESSION_KEY,
        },
        json_dumps_params={"ensure_ascii": False},
    )
