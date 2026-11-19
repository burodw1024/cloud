from django import template
from django.urls import reverse

from ..module_admin import MODULE_PERMISSIONS
from ..setup_views import SETUP_PERMISSIONS

register = template.Library()

ADMIN_LINKS = (
    ('modules', 'Business modules', MODULE_PERMISSIONS),
    ('setup', 'Integration settings', SETUP_PERMISSIONS),
    ('reporting_settings', 'Reporting setup', SETUP_PERMISSIONS),
    ('data_access', 'User access', ('agentic_ai.manage_loan_access',)),
    ('access_directory', 'Branch directory', ('agentic_ai.manage_loan_directory',)),
    ('dashboard', 'User attempts', ('agentic_ai.view_agentrunlog',)),
    ('performance', 'Query performance', ('agentic_ai.view_agentrunlog',)),
)


@register.simple_tag(takes_context=True)
def admin_navigation(context):
    request = context['request']
    user = request.user
    if not (user.is_authenticated and user.is_active and user.is_staff):
        return []
    current = getattr(request.resolver_match, 'view_name', None)
    links = [
        {'label': label, 'url': reverse(f'agentic_ai:{route}'),
         'current': current == f'agentic_ai:{route}'}
        for route, label, permissions in ADMIN_LINKS if user.has_perms(permissions)
    ]
    if user.is_superuser:
        links.append({'label': 'Report service access', 'url': reverse('agentic_ai:service_access'),
                      'current': current == 'agentic_ai:service_access'})
    return links
