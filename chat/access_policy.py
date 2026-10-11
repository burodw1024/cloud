# chat/access_policy.py
from __future__ import annotations
from typing import Dict, Set, Iterable

# Canonical group names (exactly as in Django admin -> Groups)
GROUPS = {
    "BRANCH_MANAGER",
    "DEPT_ACCOUNTS",
    "DEPT_FINANCE",
    "DEPT_HR",
    "DEPT_IT",
    "DEPT_IT_HEAD",
    "DEPT_PROCUREMENT",
    "DEPT_RESEARCH",
    "G1","G2","G3","G4","G5",
    "HEAD_OFFICE",
}

# Router/intent -> allowed groups
ROUTER_POLICY: Dict[str, Set[str]] = {
    # HR router (employee, salary, leave, etc.)
    "hr": {"DEPT_HR"},  # strict

    # Finance router
    "finance": {"DEPT_FINANCE", "HEAD_OFFICE"},

    # Accounts router
    "accounts": {"DEPT_ACCOUNTS", "HEAD_OFFICE"},

    # Procurement router
    "procurement": {"DEPT_PROCUREMENT", "HEAD_OFFICE"},

    # Research router
    "research": {"DEPT_RESEARCH", "HEAD_OFFICE"},

    # IT router (system / logs / internal tools)
    "it": {"DEPT_IT", "DEPT_IT_HEAD"},

    # Branch manager view
    "branch": {"BRANCH_MANAGER", "HEAD_OFFICE"},

    # Default doc-RAG router (policies, circulars, manuals)
    # (Often allowed for everyone authenticated; tune as you like)
    "rag_docs": set(GROUPS),
}

def user_groups_set(user) -> Set[str]:
    if not user or not user.is_authenticated:
        return set()
    return set(user.groups.values_list("name", flat=True))

def is_allowed(user, router_name: str) -> bool:
    allowed = ROUTER_POLICY.get(router_name)
    if allowed is None:
        # unknown router => deny by default (safer)
        return False

    # superuser bypass (optional, recommended)
    if getattr(user, "is_superuser", False):
        return True

    return len(user_groups_set(user) & allowed) > 0
