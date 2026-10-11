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
    allowed_roles = tool_config.get("allowed_roles", [])
    if not allowed_roles:
        return True

    user_roles = get_user_role_names(user)

    if "admin" in user_roles:
        return True

    return bool(set(user_roles).intersection(set(allowed_roles)))
