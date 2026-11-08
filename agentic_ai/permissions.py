from django.utils import timezone
from django.db.models import Q


def explicit_tool_permission(user, name):
    """None means legacy group rules; False is an explicit denial.

    Read current database state even when a request holds an older user object.
    """
    from .models import UserToolAccess
    if not getattr(user, 'is_authenticated', False):
        return None
    if getattr(user, 'pk', None) is None:
        return None
    if not getattr(user, 'is_active', False):
        return False
    policy = UserToolAccess.objects.filter(user_id=user.pk).first()
    if policy is None:
        return None
    if policy.expires_at and policy.expires_at <= timezone.now():
        return False
    return policy.tools.filter(name=name, is_active=True).filter(
        Q(module__isnull=True) |
        Q(module__is_active=True)
    ).exists()


def get_user_role_names(user):
    """
    Returns Django group names as role names.
    If user is superuser, returns admin.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return ["anonymous"]

    if getattr(user, "is_superuser", False):
        return ["admin"]

    return list(user.groups.values_list("name", flat=True)) or ["staff"]


def has_tool_permission(user, tool_config):
    explicit = explicit_tool_permission(user, tool_config.get("name", ""))
    if explicit is not None:
        return explicit
    allowed_roles = tool_config.get("allowed_roles", [])
    if not allowed_roles:
        return True

    user_roles = get_user_role_names(user)

    if "admin" in user_roles:
        return True

    return bool(set(user_roles).intersection(set(allowed_roles)))
