"""Owner-scoped report history. Opening it never reruns a tool."""
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_safe

from .api import _can_replay_result
from .dashboard_studio import permitted
from .models import DashboardExport, ReportRun
from .reporting_config import workspace_settings


def render_history(request, context):
    response = render(request, 'agentic_ai/report_history.html', context)
    response['Cache-Control'] = 'no-store'
    return response


def owned_runs(request):
    return ReportRun.objects.filter(user=request.user, user__is_active=True)


@login_required
@require_safe
def report_history(request):
    query = request.GET.get('q', '').strip()[:150]
    runs = owned_runs(request)
    if query:
        runs = runs.filter(question__icontains=query)
    page = Paginator(runs, 20).get_page(request.GET.get('page'))
    authorized = [run for run in page.object_list if _can_replay_result(request.user, run.source)]
    tokens = [run.snapshot_token for run in authorized]
    statuses = {}
    if permitted(request.user):
        for source, status in DashboardExport.objects.filter(
                user=request.user, source__report_token__in=tokens).exclude(status='deleted').values_list('source', 'status'):
            statuses.setdefault(source['report_token'], set()).add(status)
    for run in authorized:
        run.dashboard_statuses = statuses.get(run.snapshot_token, set())
    return render_history(request, {'page': page, 'runs': authorized, 'search': query})


@login_required
@require_safe
def report_run(request, run_id):
    run = get_object_or_404(owned_runs(request), pk=run_id)
    if not _can_replay_result(request.user, run.source):
        return render_history(request, {'access_revoked': True})
    saved = cache.get('agentic:summary:' + run.snapshot_token)
    report = saved['report'] if saved and saved['user_id'] == request.user.pk else None
    exports = []
    can_publish = permitted(request.user)
    if can_publish:
        exports = DashboardExport.objects.filter(user=request.user, source__report_token=run.snapshot_token).exclude(status='deleted')
    return render_history(request, {
        'run': run, 'report': report, 'exports': exports, 'can_publish': can_publish,
        'powerbi': workspace_settings(request.user)['powerbi'],
        'table_rows': [[row.get(col) for col in report['columns']] for row in report['rows']] if report else [],
    })
