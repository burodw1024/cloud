import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .customer_support_router import detect_customer_support_intent
from .customer_support_handler import handle_customer_support_intent


def parse_request_data(request):
    if request.method == "GET":
        return {
            "message": request.GET.get("message", ""),
            "member_id": request.GET.get("member_id", ""),
            "mobile_no": request.GET.get("mobile_no", ""),
            "branch_code": request.GET.get("branch_code", ""),
            "loan_account_no": request.GET.get("loan_account_no", ""),
            "savings_account_no": request.GET.get("savings_account_no", ""),
            "ticket_id": request.GET.get("ticket_id", ""),
            "complaint_type": request.GET.get("complaint_type", ""),
            "escalation_level": request.GET.get("escalation_level", ""),
            "note": request.GET.get("note", ""),
            "priority": request.GET.get("priority", ""),
        }

    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        body = {}

    return {
        "message": body.get("message", ""),
        "member_id": body.get("member_id", ""),
        "mobile_no": body.get("mobile_no", ""),
        "branch_code": body.get("branch_code", ""),
        "loan_account_no": body.get("loan_account_no", ""),
        "savings_account_no": body.get("savings_account_no", ""),
        "ticket_id": body.get("ticket_id", ""),
        "complaint_type": body.get("complaint_type", ""),
        "escalation_level": body.get("escalation_level", ""),
        "note": body.get("note", ""),
        "priority": body.get("priority", ""),
    }


def response_status_code(result):
    status = result.get("status")

    if status in ["forbidden", "login_required", "verification_required"]:
        return 403

    if status == "not_found":
        return 404

    return 200


@csrf_exempt
def customer_support_ask(request):
    if request.method not in ["GET", "POST"]:
        return JsonResponse(
            {
                "module": "customer_support",
                "status": "error",
                "answer": "Only GET and POST methods are allowed.",
            },
            status=405,
            json_dumps_params={"ensure_ascii": False},
        )

    data = parse_request_data(request)
    message = data.get("message", "")

    intent = detect_customer_support_intent(message)

    result = handle_customer_support_intent(
        user=request.user,
        intent=intent,
        data=data,
    )

    return JsonResponse(
        result,
        status=response_status_code(result),
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
def customer_support_create_ticket(request):
    if request.method != "POST":
        return JsonResponse(
            {
                "module": "customer_support",
                "status": "error",
                "answer": "Only POST method is allowed.",
            },
            status=405,
            json_dumps_params={"ensure_ascii": False},
        )

    data = parse_request_data(request)

    result = handle_customer_support_intent(
        user=request.user,
        intent="ticket_create",
        data=data,
    )

    return JsonResponse(
        result,
        status=response_status_code(result),
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
def customer_support_ticket_status(request):
    data = parse_request_data(request)

    result = handle_customer_support_intent(
        user=request.user,
        intent="ticket_status",
        data=data,
    )

    return JsonResponse(
        result,
        status=response_status_code(result),
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
def customer_support_verify(request):
    data = parse_request_data(request)

    result = handle_customer_support_intent(
        user=request.user,
        intent="customer_verify",
        data=data,
    )

    return JsonResponse(
        result,
        status=response_status_code(result),
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
def customer_support_installment_status(request):
    data = parse_request_data(request)

    result = handle_customer_support_intent(
        user=request.user,
        intent="installment_problem",
        data=data,
    )

    return JsonResponse(
        result,
        status=response_status_code(result),
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
def customer_support_loan_summary(request):
    data = parse_request_data(request)

    result = handle_customer_support_intent(
        user=request.user,
        intent="loan_summary",
        data=data,
    )

    return JsonResponse(
        result,
        status=response_status_code(result),
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
def customer_support_savings_summary(request):
    data = parse_request_data(request)

    result = handle_customer_support_intent(
        user=request.user,
        intent="savings_summary",
        data=data,
    )

    return JsonResponse(
        result,
        status=response_status_code(result),
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
def customer_support_branch_info(request):
    data = parse_request_data(request)

    result = handle_customer_support_intent(
        user=request.user,
        intent="branch_info",
        data=data,
    )

    return JsonResponse(
        result,
        status=response_status_code(result),
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
def customer_support_escalate_ticket(request):
    data = parse_request_data(request)

    result = handle_customer_support_intent(
        user=request.user,
        intent="escalate_ticket",
        data=data,
    )

    return JsonResponse(
        result,
        status=response_status_code(result),
        json_dumps_params={"ensure_ascii": False},
    )
