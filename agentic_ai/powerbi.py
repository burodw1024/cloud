"""Manual Power BI Report Server handoff for authorized snapshots."""
from io import BytesIO

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_safe

from .api import _can_replay_result
from .models import ReportRun
from .reporting_config import workspace_settings
from .superset_publisher import number
from .savings_presentation import SAVINGS_MEASURES

REPORT_MEASURES = {
    'GS_BALANCE', 'CS_BALANCE', 'LOAN_COUNT', 'DUEAMOUNT', 'OUTSTANDINGP', 'OUTSTANDING_PI',
    'Outstanding(P+I)', 'DUE_DAYS', 'DUEINSTNO_U', 'DISBURSE_AMOUNT', 'INSTALLMENT_AMOUNT',
} | SAVINGS_MEASURES


def workbook(report, context):
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.worksheet.table import Table, TableStyleInfo

    book = Workbook()
    data = book.active
    data.title = 'ReportData'
    data.freeze_panes = 'A2'

    def put(sheet, row, column, value):
        if isinstance(value, str):
            cell = sheet.cell(row, column, ILLEGAL_CHARACTERS_RE.sub('', value))
            cell.data_type = 's'  # Preserve literal report text, including formula-like strings.
        else:
            sheet.cell(row, column, value)

    for i, name in enumerate(report['columns'], 1):
        put(data, 1, i, name)
    for i, row in enumerate(report['rows'], 2):
        for j, name in enumerate(report['columns'], 1):
            value = row.get(name)
            if type(value) not in (str, int, float, bool) and value is not None:
                value = str(value)
            if type(value) in (int, float) or (name in REPORT_MEASURES and isinstance(value, str)):
                parsed = number(value)
                # Excel can preserve at most 15 significant digits as a number.
                if parsed is not None and len(parsed.normalize().as_tuple().digits) <= 15:
                    data.cell(i, j, parsed)
                    continue
                value = str(value)
            put(data, i, j, value)
    table = Table(displayName='ReportData', ref=data.dimensions)
    table.tableStyleInfo = TableStyleInfo(name='TableStyleMedium2', showRowStripes=True)
    data.add_table(table)

    scope = book.create_sheet('SourceScope')
    items = [
        ('Source tool', context.get('source')),
        ('Report date', context.get('report_date')),
        ('Displayed rows', context.get('displayed_rows')),
        ('Reported rows', context.get('source_rows')),
        ('Limited result', bool(context.get('limited'))),
    ]
    items += list(context.get('scope', {}).items())
    items += [(f'Filter: {key}', value) for key, value in context.get('filters', {}).items()]
    for i, (label, value) in enumerate([('Field', 'Value'), *items], 1):
        put(scope, i, 1, label)
        put(scope, i, 2, value)
    scope.freeze_panes = 'A2'
    scope.column_dimensions['A'].width = 24
    scope.column_dimensions['B'].width = 44

    output = BytesIO()
    book.save(output)
    output.seek(0)
    return output


@login_required
@require_safe
def powerbi_export(request, run_id):
    if not workspace_settings(request.user)['powerbi']['enabled']:
        return HttpResponse('Power BI snapshot export is unavailable for your account.', status=403,
                            headers={'Cache-Control': 'no-store'})
    run = get_object_or_404(ReportRun, pk=run_id, user=request.user)
    if not _can_replay_result(request.user, run.source):
        return HttpResponse('This report is unavailable under your current permissions.', status=403,
                            headers={'Cache-Control': 'no-store'})
    saved = cache.get('agentic:summary:' + run.snapshot_token)
    if not saved or saved['user_id'] != request.user.pk:
        return HttpResponse('The snapshot expired. Reuse the question to run the report again.', status=410,
                            headers={'Cache-Control': 'no-store'})
    response = FileResponse(workbook(saved['report'], run.context), as_attachment=True,
                            filename=f'agentic-powerbi-{run.pk}.xlsx')
    response['Cache-Control'] = 'no-store'
    return response
