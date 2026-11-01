"""Permission-protected review of submitted assistant queries."""
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import permission_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render

from .models import AgentRunLog, AgentApprovalRequest


def filtered_logs(request):
    logs = AgentRunLog.objects.select_related('user').order_by('-created_at', '-pk')
    filters = {key: request.GET.get(key, '').strip()[:200] for key in ('q', 'user', 'status', 'tool')}
    if filters['q']:
        logs = logs.filter(Q(user_query__icontains=filters['q']) |
                           Q(final_answer__icontains=filters['q']) |
                           Q(error_message__icontains=filters['q']))
    if filters['user']:
        logs = logs.filter(user__username__icontains=filters['user'])
    if filters['status']:
        logs = logs.filter(status=filters['status'])
    if filters['tool']:
        logs = logs.filter(selected_tool=filters['tool'])
    return logs, filters


@staff_member_required(login_url='/login/')
@permission_required('agentic_ai.view_agentrunlog', raise_exception=True)
def dashboard(request):
    logs, filters = filtered_logs(request)
    page = Paginator(logs, 50).get_page(request.GET.get('page'))
    query = request.GET.copy()
    query.pop('page', None)
    can_approve = request.user.has_perm('agentic_ai.view_agentapprovalrequest')
    response = render(request, 'agentic_ai/dashboard.html', {
        'logs': page, 'page_obj': page, 'filters': filters,
        'filter_query': query.urlencode(), 'statuses': AgentRunLog.STATUS_CHOICES,
        'show_approvals': can_approve,
        'approvals': AgentApprovalRequest.objects.select_related('user').filter(status='pending')[:50]
                     if can_approve else [],
    })
    response['Cache-Control'] = 'no-store'
    return response


@staff_member_required(login_url='/login/')
@permission_required('agentic_ai.view_agentrunlog', raise_exception=True)
def logs_api(request):
    logs, _ = filtered_logs(request)
    page = Paginator(logs, 100).get_page(request.GET.get('page'))
    response = JsonResponse({'ok': True, 'page': page.number,
        'pages': page.paginator.num_pages, 'count': page.paginator.count,
        'logs': [{'id': row.pk, 'created_at': row.created_at.isoformat(),
                  'user': row.user.username if row.user else None,
                  'query': row.user_query, 'intent': row.detected_intent,
                  'tool': row.selected_tool, 'status': row.status, 'risk_level': row.risk_level,
                  'answer': row.final_answer, 'error': row.error_message} for row in page]})
    response['Cache-Control'] = 'no-store'
    return response
