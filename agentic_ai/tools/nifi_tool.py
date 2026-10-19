import requests
import re

from chat.dynamic_config import get_nifi_url
from ..nifi_operations import OPERATIONS, project_result


def run(user=None, query="", params=None, language="en", operation=None, **kwargs):
    """
    Data Warehouse query tool.
    Uses existing dynamic config:
    NIFI_CORE_URL from DB dashboard / env / app_config.py.
    """
    if operation is not None and operation not in OPERATIONS:
        return {"ok": False, "answer": "Unsupported NiFi business operation."}
    if operation:
        params = params or {}
        member_id = params.get("member_id")
        if (set(params) != {"member_id"} or not isinstance(member_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", member_id)):
            return {"ok": False, "answer": "A valid member_id is required for this operation."}
    nifi_core_url = get_nifi_url("NIFI_CORE_URL")

    payload = {
        "query": query,
        "params": params or {},
        "language": language,
        "requested_by": getattr(user, "username", "anonymous") if user else "anonymous",
    }

    try:
        resp = requests.post(nifi_core_url, json=payload, timeout=60)
        resp.raise_for_status()

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        if operation:
            data = project_result(data, operation, member_id=member_id)

        return {
            "ok": True,
            "source": "nifi",
            "url": nifi_core_url,
            "data": data,
            "operation": operation,
            "answer": format_nifi_answer(data, language=language, operation=operation),
        }

    except Exception as exc:
        return {
            "ok": False,
            "source": "nifi",
            "url": nifi_core_url,
            "error": "invalid_response" if isinstance(exc, ValueError) else "backend_unavailable",
            "answer": "NiFi returned an invalid or out-of-scope result." if isinstance(exc, ValueError)
                      else "NiFi/Core backend is unavailable. Please try again later.",
        }


def get_loan_balance(**kwargs):
    return run(operation="loan_balance", **kwargs)


def get_loan_due_details(**kwargs):
    return run(operation="loan_due", **kwargs)


def get_loan_installment(**kwargs):
    return run(operation="loan_installment", **kwargs)


def get_savings_balance(**kwargs):
    return run(operation="savings_balance", **kwargs)


def format_nifi_answer(data, language="en", operation=None):
    if operation:
        if not data:
            return "এই সদস্যের জন্য কোনো তথ্য পাওয়া যায়নি।" if language == "bn" else "No records returned for this member."
        # Deterministic rendering preserves exact financial values; no LLM math.
        labels = {
            "outstanding_principal": ("Outstanding principal", "বকেয়া মূলধন"),
            "outstanding_total": ("Outstanding (principal + interest)", "বকেয়া (মূলধন + সুদ)"),
            "due_amount": ("Due amount", "বকেয়া পরিমাণ"),
            "due_days": ("Due days", "বকেয়া দিন"),
            "due_installments": ("Due installments", "বকেয়া কিস্তি"),
            "installment_amount": ("Installment amount", "কিস্তির পরিমাণ"),
            "payment_frequency": ("Payment frequency", "কিস্তির সময়সূচি"),
            "gs_balance": ("GS balance", "GS স্থিতি"),
            "cs_balance": ("CS balance", "CS স্থিতি"),
        }
        lines = []
        for index, row in enumerate(data, 1):
            if len(data) > 1:
                lines.append(f"Record {index}:")
            for field, value in row.items():
                label = labels[field][1 if language == "bn" else 0]
                missing = "পাওয়া যায়নি" if language == "bn" else "not provided"
                lines.append(f"{label}: {missing if value is None else value}")
        return "\n".join(lines)
    if language == "bn":
        return {
            "summary": "NiFi/Data Warehouse থেকে ফলাফল পাওয়া গেছে।",
            "raw": data,
        }

    return {
        "summary": "Result received from NiFi/Data Warehouse.",
        "raw": data,
    }
