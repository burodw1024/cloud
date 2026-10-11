# chat/rbac.py
from __future__ import annotations
from functools import wraps
from typing import Iterable, Set
from django.http import JsonResponse

def _user_group_names(user) -> Set[str]:
    try:
        if not user or not user.is_authenticated:
            return set()
        return set(user.groups.values_list("name", flat=True))
    except Exception:
        return set()

def require_any_group(*allowed_groups: str, allow_superuser: bool = True, allow_staff: bool = False):
    """
    Enforce: user must be in at least one of allowed_groups.
    - allow_superuser=True  => superuser bypass
    - allow_staff=False     => staff alone is NOT enough (recommended)
    """
    allowed = set(g.strip() for g in allowed_groups if g and g.strip())

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = getattr(request, "user", None)

            if not user or not user.is_authenticated:
                return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

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
