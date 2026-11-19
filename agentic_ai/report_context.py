"""Metadata derived from an authorized, bounded report projection."""
import re

FIELD_LABELS = {
    'from_date': 'Period start', 'to_date_exclusive': 'Period end (exclusive)',
    'branch_code': 'Branch', 'zone_code': 'Zone', 'authorized_branch_count': 'Authorized branches',
    'level': 'Level', 'report_date': 'Requested report date', 'disbursed_from': 'Disbursed from', 'disbursed_to': 'Disbursed to',
    'sort_by': 'Sort field', 'sort_order': 'Sort direction', 'top_n': 'Requested row limit',
}


def report_context(report, tool):
    def fields(values):
        if not isinstance(values, dict):
            return {}
        return {FIELD_LABELS[key]: str(value)[:100] for key, value in values.items()
                if key in FIELD_LABELS and type(value) in (str, int) and value != ''}

    date = report.get('report_date')
    displayed = len(report['rows'])
    row_count = report.get('row_count')
    scope = fields(report.get('scope'))
    if report.get('level'):
        scope.update(fields({'level': report['level']}))
    return {
        'source': str(tool or '')[:150],
        'report_date': date if isinstance(date, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', date) else '',
        'displayed_rows': displayed,
        'source_rows': row_count if type(row_count) is int and row_count >= displayed else None,
        'limited': bool(report.get('truncated')),
        'scope': scope,
        'filters': fields(report.get('filters')),
    }
