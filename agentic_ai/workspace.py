"""Role-aware workspace metadata. Loading the home page never executes a tool."""
from .models import AgentTool, AgentRunLog, AgentApprovalRequest
from .mcp_gateway import _allowed, gateway_enabled
from .permissions import get_user_role_names


def workspace_context(user):
    if not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_active', False):
        return {'capabilities': [], 'capability_count': 0, 'recent_activity': [],
                'pending_approvals': [], 'pending_approval_count': 0}
    records = list(AgentTool.objects.filter(is_active=True).order_by('category', 'name'))
    roles = set(get_user_role_names(user))
    catalog = []
    if gateway_enabled():
        for record in records:
            if not _allowed(record, user, roles):
                continue
            catalog.append({'name': record.name,
                'title': record.display_name or record.name.replace('_', ' ').replace('-', ' ').title(),
                'description': record.description[:1200], 'category': record.category.strip() or 'General',
                'example': record.example_question, 'requires_approval': record.requires_approval})
    else:
        from .tool_registry import available_tool_catalog, TOOL_REGISTRY
        metadata = {record.name: record for record in records}
        for name, config in available_tool_catalog(user).items():
            record = metadata.get(name)
            catalog.append({'name': name, 'title': record.display_name if record and record.display_name else name.replace('_', ' ').title(),
                'description': config['description'], 'category': record.category if record else 'General',
                'example': record.example_question if record else '',
                'requires_approval': TOOL_REGISTRY[name].get('requires_approval', False)})
    activity = list(AgentRunLog.objects.filter(user=user).order_by('-created_at').values(
        'user_query', 'status', 'created_at')[:5])
    approvals = list(AgentApprovalRequest.objects.filter(user=user, status='pending').order_by('-created_at').values(
        'tool_name', 'created_at')[:3])
    return {'capabilities': catalog, 'capability_count': len(catalog),
            'recent_activity': activity, 'pending_approvals': approvals,
            'pending_approval_count': AgentApprovalRequest.objects.filter(user=user, status='pending').count()}
