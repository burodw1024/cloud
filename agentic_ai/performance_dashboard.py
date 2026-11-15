"""Staff-only view of measured query bottlenecks; no backend credentials."""
from collections import defaultdict
from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import permission_required
from django.core.paginator import Paginator
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from .performance import STAGES, percentile, trace_rows
from .review import filtered_logs

SAMPLE_LIMIT = 1000


def metrics_for(rows):
    totals, measured, stages, tools = [], [], defaultdict(list), defaultdict(list)
    interruptions = defaultdict(int)
    for row in rows:
        trace = row['performance_summary']
        if not trace_rows(trace):
            continue
        total = trace['total_ms']
        totals.append(total)
        measured.append(row)
        tools[row['selected_tool'] or 'No tool selected'].append(total)
        for item in trace_rows(trace):
            stages[item['name']].append(item['duration_ms'])
            interruptions[item['name']] += item.get('status') == 'interrupted'
    elapsed = sum(totals)
    breakdown = sorted([
        {'name': name, 'label': STAGES[name][0], 'hint': STAGES[name][1], 'count': len(values),
         'avg': round(sum(values) / len(values), 2), 'p95': percentile(values, .95),
         'share': round(sum(values) / max(elapsed, 1) * 100, 1), 'interrupted': interruptions[name]}
        for name, values in stages.items()], key=lambda item: item['share'], reverse=True)
    return {
        'measured': len(totals), 'unmeasured': len(rows) - len(totals),
        'avg': round(sum(totals) / len(totals), 2) if totals else None,
        'p50': percentile(totals, .5), 'p95': percentile(totals, .95),
        'failed': sum(row['status'] == 'failed' for row in measured),
        'blocked': sum(row['status'] in {'blocked', 'approval_required'} for row in measured),
        'clarifications': sum(row['performance_summary'].get('outcome') == 'clarification' for row in measured),
        'completed_reports': sum(row['status'] == 'success' and row['performance_summary'].get('completed') is True for row in measured),
        'stages': breakdown, 'bottleneck': breakdown[0] if breakdown else None,
        'tools': sorted([{'name': name, 'count': len(values), 'avg': round(sum(values) / len(values), 2),
                          'p95': percentile(values, .95)} for name, values in tools.items()],
                        key=lambda item: item['avg'], reverse=True)[:20],
    }


@staff_member_required(login_url='/login/')
@permission_required('agentic_ai.view_agentrunlog', raise_exception=True)
@require_GET
def performance_dashboard(request):
    logs, filters = filtered_logs(request)
    window = request.GET.get('window', '24h')
    windows = {'1h': 1, '24h': 24, '7d': 168}
    if window not in windows:
        window = '24h'
    logs = logs.filter(created_at__gte=timezone.now() - timedelta(hours=windows[window]))
    # Bound Python aggregation and avoid fetching stored reports or tool arguments.
    sampled = list(logs.values('performance_summary', 'status', 'selected_tool')[:SAMPLE_LIMIT])
    metrics = metrics_for(sampled)
    page = Paginator(logs.only('created_at', 'user__username', 'user_id', 'user_query',
                               'selected_tool', 'status', 'performance_summary'), 25).get_page(request.GET.get('page'))
    attempts = []
    for log in page:
        trace = log.performance_summary
        measured = bool(trace_rows(trace))
        attempts.append({'log': log, 'measured': measured, 'trace': trace if measured else {},
                         'stages': trace_rows(trace)})
    query = request.GET.copy()
    query.pop('page', None)
    with timezone.override('Asia/Dhaka'):
        response = render(request, 'agentic_ai/performance.html', {
            'metrics': metrics, 'attempts': attempts, 'page_obj': page, 'window': window,
            'filters': filters, 'filter_query': query.urlencode(), 'sample_limit': SAMPLE_LIMIT,
            'sampled': len(sampled), 'statuses': [('success', 'Success'), ('failed', 'Failed'),
                                                 ('blocked', 'Blocked'), ('approval_required', 'Approval required')],
        })
    response['Cache-Control'] = 'no-store'
    return response
