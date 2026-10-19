"""Approved projections of the existing Oracle loan/member result."""
from decimal import Decimal, InvalidOperation

OPERATIONS = {
    "loan_balance": {
        "OUTSTANDINGP": "outstanding_principal",
        "Outstanding(P+I)": "outstanding_total",
    },
    "loan_due": {
        "DUEAMOUNT": "due_amount",
        "DUE_DAYS": "due_days",
        "DUEINSTNO_U": "due_installments",
    },
    "loan_installment": {
        "INSTALLMENT_AMOUNT": "installment_amount",
        "PAYMENTFREQUENCY": "payment_frequency",
    },
    "savings_balance": {
        "GS_BALANCE": "gs_balance",
        "CS_BALANCE": "cs_balance",
    },
}


def project_result(data, operation, member_id=None):
    """Accept a row, row list, or common JSON envelope; reject unknown shapes.

    Missing columns are errors, never fabricated null/zero financial balances.
    Projection is not account authorization or aggregation.
    """
    fields = OPERATIONS[operation]
    if isinstance(data, dict):
        if (data.get("ok") is False or data.get("success") is False or data.get("error")
                or data.get("status") in ("error", "failed", "failure")):
            raise ValueError("NiFi reported an unsuccessful response.")
        if any(key in data for key in fields):
            rows = [data]
        else:
            for key in ("rows", "data", "results", "result"):
                if key in data:
                    return project_result(data[key], operation, member_id=member_id)
            raise ValueError("Unsupported NiFi response: expected Oracle rows.")
    elif isinstance(data, list):
        rows = data
    else:
        raise ValueError("Unsupported NiFi response: expected Oracle rows.")
    projected = []
    if len(rows) > 100:
        raise ValueError("Too many rows for a member inquiry; use an approved report operation.")
    for row in rows:
        if not isinstance(row, dict) or not all(key in row for key in fields):
            raise ValueError("NiFi response is missing required operation columns.")
        if member_id is not None and str(row.get("MEMBERID", "")) != member_id:
            raise ValueError("NiFi returned rows outside the requested member scope.")
        for column in fields:
            value = row[column]
            if value is None:
                continue
            if column == "PAYMENTFREQUENCY":
                if not isinstance(value, str) or len(value) > 100:
                    raise ValueError("Invalid payment frequency.")
            else:
                try:
                    if isinstance(value, bool) or not Decimal(str(value)).is_finite():
                        raise ValueError("Invalid financial value.")
                except InvalidOperation as exc:
                    raise ValueError("Invalid financial value.") from exc
        projected.append({alias: row[column] for column, alias in fields.items()})
    return projected
