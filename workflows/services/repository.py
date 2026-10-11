from workflows.models import WorkflowDefinition, WorkflowSession


def get_active_definition(intent_name: str):
    return WorkflowDefinition.objects.filter(
        name=intent_name,
        is_active=True
    ).order_by("-version").first()


def get_active_workflow(user, session_id: str):
    return WorkflowSession.objects.filter(
        user=user,
        session_id=session_id,
        status__in=["active", "waiting_upload"]
    ).order_by("-started_at").first()
