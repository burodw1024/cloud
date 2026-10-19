"""Approved executable tool contracts. Imports are deferred until execution."""
from importlib import import_module


def _handler(module, function="run"):
    def execute(**kwargs):
        return getattr(import_module(f"agentic_ai.tools.{module}"), function)(**kwargs)
    return execute


TOOL_REGISTRY = {
    "nifi_tool": {
        "function": _handler("nifi_tool"),
        "description": "Query Oracle/Data Warehouse through NiFi controlled API.",
        "requires_approval": False,
        "allowed_roles": ["admin", "manager", "branch_staff", "cashflow", "core"],
    },
    "policy_tool": {
        "function": _handler("policy_tool"),
        "description": "Answer from existing BURO policy RAG module.",
        "requires_approval": False,
        "allowed_roles": ["admin", "manager", "staff", "branch_staff", "policy"],
    },
    "notice_tool": {
        "function": _handler("notice_tool"),
        "description": "Answer from HR notice RAG module.",
        "requires_approval": False,
        "allowed_roles": ["admin", "manager", "staff", "branch_staff", "notice"],
    },
    "customer_support_tool": {
        "function": _handler("customer_support_tool"),
        "description": "Customer support knowledge and support workflow.",
        "requires_approval": False,
        "allowed_roles": ["admin", "manager", "branch_staff", "support_staff", "support_manager", "customer_support"],
    },
    "website_rag_tool": {
        "function": _handler("website_rag_tool"),
        "description": "Offline RAG over BURO website knowledge.",
        "requires_approval": False,
        "allowed_roles": ["admin", "manager", "staff", "branch_staff", "public_user", "anonymous"],
    },
    "outlook_tool": {
        "function": _handler("outlook_tool"),
        "description": "Outlook email search, summary, and draft workflow.",
        "requires_approval": True,
        "allowed_roles": ["admin", "manager"],
    },
    "teams_tool": {
        "function": _handler("teams_tool"),
        "description": "Teams selected channel search and summary.",
        "requires_approval": False,
        "allowed_roles": ["admin", "manager"],
    },
}

# Reuse the existing NiFi role policy for each focused read operation.
for _name, _description in {
    "get_loan_balance": "Return principal and total outstanding from Oracle through NiFi.",
    "get_loan_due_details": "Return due amount, days and installment count through NiFi.",
    "get_loan_installment": "Return installment amount and payment frequency through NiFi.",
    "get_savings_balance": "Return GS and CS savings balances through NiFi.",
}.items():
    TOOL_REGISTRY[_name] = {
        **TOOL_REGISTRY["nifi_tool"],
        "function": _handler("nifi_tool", _name),
        "description": _description,
        "parameters": {
            "type": "object",
            "properties": {
                "member_id": {
                    "type": "string", "pattern": "[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
                    "description": "Exact member ID explicitly provided in the question; never infer ownership.",
                },
            },
            "required": ["member_id"],
            "additionalProperties": False,
        },
    }


def available_tool_catalog(user):
    """Advertise only executable, role-approved capabilities with input contracts."""
    from .permissions import has_tool_permission

    catalog = {}
    for name, config in TOOL_REGISTRY.items():
        # Generic NiFi accepts unrestricted natural-language reports and has no
        # validated report contract. It must not be an AI-selected escape hatch.
        if name == "nifi_tool" or not has_tool_permission(user, config):
            continue
        catalog[name] = {
            "description": config["description"],
            "parameters": config.get("parameters", {
                "type": "object", "properties": {}, "required": [],
                "additionalProperties": False,
            }),
        }
    return catalog
