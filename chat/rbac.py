# chat/rbac.py
from __future__ import annotations

from functools import wraps
from typing import Set

from django.http import JsonResponse


ROLE_MODULE_PERMISSIONS = {
    # Existing module-style groups
    "general": [
        "general",
    ],

    "cashflow": [
        "cashflow",
    ],

    "hr": [
        "hr",
    ],

    "core": [
        "core",
        "core_policy",
    ],

    "policy": [
        "policy",
    ],

    "notice": [
        "notice",
    ],

    # Customer Support module groups
    "customer_support": [
        "customer_support",
    ],

    "branch_staff": [
        "customer_support",
        "customer_support_sensitive",
        "customer_ticket_create",
        "customer_ticket_status",
    ],

    "support_staff": [
        "customer_support",
        "customer_ticket_create",
        "customer_ticket_status",
    ],

    "support_manager": [
        "customer_support",
        "customer_support_sensitive",
        "customer_ticket_create",
        "customer_ticket_status",
        "customer_ticket_admin",
    ],

    "admin": [
        "general",
        "cashflow",
        "hr",
        "core",
        "core_policy",
        "policy",
        "notice",
        "customer_support",
        "customer_support_sensitive",
        "customer_ticket_create",
        "customer_ticket_status",
        "customer_ticket_admin",
    ],

    # Optional public role. Anonymous users are handled separately.
    "public_user": [
        "customer_support",
    ],
}


def _user_group_names(user) -> Set[str]:
    try:
        if not user or not user.is_authenticated:
            return set()
        return set(user.groups.values_list("name", flat=True))
    except Exception:
        return set()


def user_has_permission(user, permission_key: str) -> bool:
    """
    Permission helper used by module handlers.

    Example permission keys:
    - customer_support
    - customer_support_sensitive
    - customer_ticket_create
    - customer_ticket_status
    - customer_ticket_admin
    - policy
    - notice
    - core
    - hr
    - cashflow
    """

    if not permission_key:
        return False

    try:
        if user and getattr(user, "is_superuser", False):
            return True

        if not user or not getattr(user, "is_authenticated", False):
            role_names = ["public_user"]
        else:
            role_names = list(user.groups.values_list("name", flat=True))

            if not role_names:
                role_names = ["public_user"]

        for role in role_names:
            allowed_permissions = ROLE_MODULE_PERMISSIONS.get(role, [])
            if permission_key in allowed_permissions:
                return True

        return False

    except Exception:
        return False


def require_any_group(*allowed_groups: str, allow_superuser: bool = True, allow_staff: bool = False):
    """
    Enforce: user must be in at least one of allowed_groups.

    - allow_superuser=True  => superuser bypass
    - allow_staff=False     => staff alone is NOT enough
    """

    allowed = set(g.strip() for g in allowed_groups if g and g.strip())

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = getattr(request, "user", None)

            if not user or not user.is_authenticated:
                return JsonResponse(
                    {
                        "ok": False,
                        "error": "Authentication required.",
                    },
                    status=401,
                )

            if allow_superuser and getattr(user, "is_superuser", False):
                return view_func(request, *args, **kwargs)

            if allow_staff and getattr(user, "is_staff", False):
                return view_func(request, *args, **kwargs)

            user_groups = _user_group_names(user)

            if allowed and (user_groups & allowed):
                return view_func(request, *args, **kwargs)

            return JsonResponse(
                {
                    "ok": False,
                    "error": "Access denied.",
                    "required_groups": sorted(list(allowed)),
                    "your_groups": sorted(list(user_groups))[:50],
                },
                status=403,
            )

        return _wrapped

    return decorator
