from .tools import (
    nifi_tool,
    policy_tool,
    notice_tool,
    customer_support_tool,
    website_rag_tool,
    outlook_tool,
    teams_tool,
)


TOOL_REGISTRY = {
    "nifi_tool": {
        "function": nifi_tool.run,
        "description": "Query Oracle/Data Warehouse through NiFi controlled API.",
        "requires_approval": False,
        "allowed_roles": ["admin", "manager", "branch_staff", "cashflow", "core"],
    },
    "policy_tool": {
        "function": policy_tool.run,
        "description": "Answer from existing BURO policy RAG module.",
        "requires_approval": False,
        "allowed_roles": ["admin", "manager", "staff", "branch_staff", "policy"],
    },
    "notice_tool": {
        "function": notice_tool.run,
        "description": "Answer from HR notice RAG module.",
        "requires_approval": False,
        "allowed_roles": ["admin", "manager", "staff", "branch_staff", "notice"],
    },
    "customer_support_tool": {
        "function": customer_support_tool.run,
        "description": "Customer support knowledge and support workflow.",
        "requires_approval": False,
        "allowed_roles": ["admin", "manager", "branch_staff", "support_staff", "support_manager", "customer_support"],
    },
    "website_rag_tool": {
        "function": website_rag_tool.run,
        "description": "Offline RAG over BURO website knowledge.",
        "requires_approval": False,
        "allowed_roles": ["admin", "manager", "staff", "branch_staff", "public_user", "anonymous"],
    },
    "outlook_tool": {
        "function": outlook_tool.run,
        "description": "Outlook email search, summary, and draft workflow.",
        "requires_approval": True,
        "allowed_roles": ["admin", "manager"],
    },
    "teams_tool": {
        "function": teams_tool.run,
        "description": "Teams selected channel search and summary.",
        "requires_approval": False,
        "allowed_roles": ["admin", "manager"],
    },
}
