import requests
from django.conf import settings


CUSTOMER_SUPPORT_NIFI_ENABLED = getattr(
    settings,
    "CUSTOMER_SUPPORT_NIFI_ENABLED",
    True,
)

CUSTOMER_SUPPORT_NIFI_URL = getattr(
    settings,
    "CUSTOMER_SUPPORT_NIFI_URL",
    "http://192.168.1.139:8880/customer/query",
)

CUSTOMER_SUPPORT_NIFI_TIMEOUT = getattr(
    settings,
    "CUSTOMER_SUPPORT_NIFI_TIMEOUT",
    250,
)


def get_customer_support_role(user):
    if not user or not user.is_authenticated:
        return "public"

    if user.is_superuser:
        return "customer_support_admin"

    group_names = set(user.groups.values_list("name", flat=True))

    if "customer_support_admin" in group_names:
        return "customer_support_admin"

    if "customer_support_head_office" in group_names:
        return "customer_support_head_office"

    if "customer_support_area" in group_names:
        return "customer_support_area"

    if "customer_support_branch" in group_names:
        return "customer_support_branch"

    if "customer_support_customer" in group_names:
        return "customer_support_customer"

    return "public"


def build_customer_support_nifi_payload(user, data, intent, ticket=None):
    return {
        "module": "customer_support",
        "intent": intent,

        "username": user.username if user and user.is_authenticated else "anonymous",
        "role": get_customer_support_role(user),

        "member_id": data.get("member_id") or "",
        "mobile_no": data.get("mobile_no") or "",
        "branch_code": data.get("branch_code") or "",

        "loan_account_no": data.get("loan_account_no") or "",
        "savings_account_no": data.get("savings_account_no") or "",

        "ticket_id": str(ticket.id) if ticket else str(data.get("ticket_id") or ""),
        "complaint_type": data.get("complaint_type") or intent,
        "complaint_text": data.get("message") or data.get("complaint_text") or "",

        "escalation_level": data.get("escalation_level") or "",
        "note": data.get("note") or "",
        "priority": data.get("priority") or "NORMAL",
    }


def call_customer_support_nifi(payload):
    if not CUSTOMER_SUPPORT_NIFI_ENABLED:
        return {
            "called": False,
            "enabled": False,
            "success": False,
            "message": "Customer Support NiFi integration is disabled.",
        }

    try:
        response = requests.post(
            CUSTOMER_SUPPORT_NIFI_URL,
            json=payload,
            timeout=CUSTOMER_SUPPORT_NIFI_TIMEOUT,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            response_data = response.json()
        except Exception:
            response_data = {
                "raw_response": response.text,
            }

        return {
            "called": True,
            "enabled": True,
            "success": response.ok,
            "status_code": response.status_code,
            "data": response_data,
        }

    except requests.exceptions.Timeout:
        return {
            "called": True,
            "enabled": True,
            "success": False,
            "error": "timeout",
            "message": "NiFi request timed out.",
        }

    except requests.exceptions.ConnectionError:
        return {
            "called": True,
            "enabled": True,
            "success": False,
            "error": "connection_error",
            "message": "Could not connect to NiFi.",
        }

    except requests.exceptions.RequestException as exc:
        return {
            "called": True,
            "enabled": True,
            "success": False,
            "error": str(exc),
            "message": "NiFi request failed.",
        }
