def user_can_start_workflow(user, workflow_definition):
    allowed = workflow_definition.allowed_groups or []

    if user.is_superuser:
        return True

    if not allowed:
        return True

    user_groups = set(user.groups.values_list("name", flat=True))
    return bool(user_groups.intersection(set(allowed)))
