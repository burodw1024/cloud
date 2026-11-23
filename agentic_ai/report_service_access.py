"""Superuser administration for named reporting and delivery permissions."""
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from .access_admin import record_event

SERVICE_PERMISSIONS = {
    'schedule_report': 'Schedule reports',
    'publish_dashboard': 'Publish private Superset dashboards',
    'publish_powerbi_report': 'Publish Power BI Report Server reports',
    'export_powerbi_snapshot': 'Download Power BI workbooks',
    'share_report_whatsapp': 'Send through WhatsApp',
    'share_report_gmail': 'Send through Gmail',
    'share_report_outlook': 'Send through Outlook',
    'share_report_viber': 'Send through Viber',
    'share_report_telegram': 'Send through Telegram',
}


def service_permission_queryset():
    return Permission.objects.filter(content_type__app_label='agentic_ai', codename__in=SERVICE_PERMISSIONS)


def service_grant_rows(target, kind='user'):
    permissions = service_permission_queryset()
    grants = target.permissions if kind == 'group' else target.user_permissions
    direct = set(grants.filter(pk__in=permissions).values_list('codename', flat=True))
    inherited = (set(permissions.filter(group__user=target).values_list('codename', flat=True))
                 if kind == 'user' else set())
    return [(key, label, key in direct, key in inherited) for key, label in SERVICE_PERMISSIONS.items()]


@staff_member_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
@csrf_protect
def service_access(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    values = request.POST if request.method == 'POST' else request.GET
    kind = values.get('kind', 'group')
    name = values.get('name', '').strip()[:150]
    model = Group if kind == 'group' else get_user_model()
    field = 'name' if kind == 'group' else 'username'
    target = None
    error = ''
    permissions = service_permission_queryset()
    if kind in {'group', 'user'} and name:
        target = model.objects.filter(**{field: name}).first()
        if target is None:
            error = 'No role or user with that exact name was found.'
    if request.method == 'POST' and target:
        selected = set(values.getlist('permissions'))
        reason = values.get('reason', '').strip()
        if selected - set(SERVICE_PERMISSIONS) or not 8 <= len(reason) <= 500:
            error = 'Choose valid grants and enter a reason of 8–500 characters.'
        elif kind == 'user' and target.is_superuser:
            error = 'Superusers already have these permissions.'
        else:
            with transaction.atomic():
                target = model.objects.select_for_update().get(pk=target.pk)
                grants = target.permissions if kind == 'group' else target.user_permissions
                before = set(grants.filter(pk__in=permissions).values_list('codename', flat=True))
                grants.remove(*permissions.filter(codename__in=before - selected))
                grants.add(*permissions.filter(codename__in=selected - before))
                record_event(request.user, target if kind == 'user' else None, 'report_service_access', reason,
                             {'kind': kind, 'name': name, 'grants': sorted(before)},
                             {'kind': kind, 'name': name, 'grants': sorted(selected)})
            if kind == 'user' and values.get('return_to') == 'user_access':
                return redirect(reverse('agentic_ai:data_access',
                                        query={'user': target.pk, 'services_saved': '1'}, fragment='service-grants'))
            return redirect(reverse('agentic_ai:service_access', query={'kind': kind, 'name': name, 'saved': '1'}))
    response = render(request, 'agentic_ai/report_service_access.html', {
        'kind': kind, 'name': name, 'target': target, 'error': error,
        'saved': request.GET.get('saved') == '1', 'roles': Group.objects.order_by('name'),
        'services': service_grant_rows(target, kind) if target else [],
    })
    response['Cache-Control'] = 'no-store'
    return response
