"""Role-aware workspace metadata. Loading the home page never executes a tool."""
from .models import AgentTool, AgentRunLog, AgentApprovalRequest
from .mcp_gateway import _allowed, gateway_enabled
from .permissions import get_user_role_names


def workspace_context(user, module_slug=''):
    if not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_active', False):
        return {'capabilities': [], 'capability_count': 0, 'recent_activity': [],
                'pending_approvals': [], 'pending_approval_count': 0}
    records = list(AgentTool.objects.filter(is_active=True).select_related('module').order_by('category', 'name'))
    roles = set(get_user_role_names(user))
    catalog = []
    if gateway_enabled():
        for record in records:
            if not _allowed(record, user, roles):
                continue
            catalog.append({'name': record.name,
                'title': record.display_name or record.name.replace('_', ' ').replace('-', ' ').title(),
                'description': record.description[:1200], 'category': record.module.name if record.module_id else record.category.strip() or 'General',
                'module_slug': record.module.slug if record.module_id else '',
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
    permitted_names = {item['name'] for item in catalog}
    modules = []
    if gateway_enabled():
        from .models import BusinessModule
        for module in BusinessModule.objects.filter(is_active=True).prefetch_related('tools'):
            count = sum(tool.name in permitted_names for tool in module.tools.all())
            if count:
                modules.append({'name': module.name, 'slug': module.slug, 'description': module.description,
                                'count': count, 'questions': [q.strip() for q in module.starter_questions.splitlines() if q.strip()]})
    selected_module = next((module for module in modules if module['slug'] == module_slug), None)
    if module_slug:
        # Unknown/unavailable modules never reveal their names or capabilities.
        catalog = [item for item in catalog if selected_module and item.get('module_slug') == module_slug]
    from .loan_access import protected_tools
    loan_names = protected_tools() | {record.name for record in records if record.requires_loan_scope}
    activity = list(AgentRunLog.objects.filter(user=user).order_by('-created_at').values(
        'user_query', 'status', 'created_at', 'usage_summary')[:5])
    approvals = list(AgentApprovalRequest.objects.filter(user=user, status='pending').order_by('-created_at').values(
        'tool_name', 'created_at')[:3])
    return {'capabilities': catalog, 'capability_count': len(catalog),
            'has_loan_capabilities': any(item['name'] in loan_names for item in catalog),
            'business_modules': modules, 'selected_module': selected_module,
            'recent_activity': activity, 'pending_approvals': approvals,
            'pending_approval_count': AgentApprovalRequest.objects.filter(user=user, status='pending').count()}
