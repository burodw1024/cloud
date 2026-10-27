"""Declared snake_case GET_LOAN_DUE_QUERY contract; legacy reports stay separate.

Database performs grouping and top-N. Never rank a truncated branch report locally.
Role/approval checks still run in the gateway immediately before dispatch. Branch
scope is enforced by the authenticated backend, never supplied by the model.
"""
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
import json
import re

LEVELS = {'LOAN', 'MEMBER', 'BRANCH', 'ZONE', 'AREA', 'DIVISION', 'PRODUCT', 'EMPLOYEE'}
SORTS = {'DUEAMOUNT', 'OUTSTANDINGP', 'OUTSTANDING_PI', 'DUE_DAYS', 'LOAN_COUNT'}
DESCRIPTION = (
    ' Native NiFi query: use snake_case fields from this schema, never operation or camelCase. '
    'For highest overdue loan use level LOAN, sort_by DUEAMOUNT, sort_order DESC, top_n 1. '
    'For member totals use level MEMBER; for branch totals use BRANCH; for zone totals use ZONE. '
    'branch_code and zone_code are filters, not ranking levels. '
    'Translate explicit calendar dates to ISO; report_date is the balance snapshot date. '
    'disbursed_from/disbursed_to refer to loan disbursement dates, not balance comparisons. '
    'No top_n defaults to 10. Backend groups before limiting and enforces authorized branch scope. '
    'Never claim a complete zone when only a subset of its branches is authorized.'
)


def supports_native_query(tool, report_names):
    if tool.name not in report_names or tool.inputSchema.get('type') != 'object':
        return False
    props = tool.inputSchema.get('properties', {})
    return (all(props.get(k, {}).get('type') == 'string'
                for k in ('report_date', 'branch_code', 'zone_code'))
            and {'LOAN', 'MEMBER', 'BRANCH', 'ZONE'} <= set(props.get('level', {}).get('enum', []))
            and 'DUEAMOUNT' in props.get('sort_by', {}).get('enum', [])
            and props.get('top_n', {}).get('type') == 'integer'
            and 'report_date' in tool.inputSchema.get('required', []))


MONTH = (r'(?:January|February|March|April|May|June|July|August|September|October|November|December'
         r'|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)')
DATE_PATTERN = rf'(?:\d{{4}}-\d{{2}}-\d{{2}}|{MONTH}\s+\d{{1,2}}(?:,\s*|\s+)\d{{4}}|\d{{1,2}}\s+{MONTH}\s+\d{{4}})'


def _question_date(text):
    value = re.sub(r'\s+', ' ', text.strip().replace(',', ' '))
    value = re.sub(r'\bSept\b', 'Sep', value, flags=re.I)
    for fmt in ('%Y-%m-%d', '%B %d %Y', '%b %d %Y', '%d %B %Y', '%d %b %Y'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError('Invalid explicit question date')


def validate_native_call(tool, arguments, query):
    from .mcp_gateway import _validator
    _validator(tool.inputSchema).validate(arguments)
    if set(arguments) & {'operation', 'authorized_branches', 'auth.branch_codes'}:
        raise ValueError('Only backend-declared query fields are allowed')
    report = arguments.get('report_date')
    if not isinstance(report, str) or date.fromisoformat(report).isoformat() != report:
        raise ValueError('Provide an explicit report date')
    dates = {_question_date(m.group()) for m in re.finditer(DATE_PATTERN, query, re.I)}
    # 'January 1 and December 31, 2025' shares the explicitly supplied year.
    # Accept only same-year forward ranges; cross-year ranges need both years.
    shared_range = re.search(r'(' + MONTH + r'\s+\d{1,2})\s+(?:and|to|through)\s+('
                             + MONTH + r'\s+\d{1,2})(?:,\s*|\s+)(\d{4})', query, re.I)
    if shared_range:
        start = _question_date(shared_range.group(1) + ' ' + shared_range.group(3))
        end = _question_date(shared_range.group(2) + ' ' + shared_range.group(3))
        if start <= end:
            dates.update((start, end))
    if report not in dates:
        raise ValueError('Report date must be provided in the question')
    snapshot = re.search(r'(?:as\s+of|measured\s+as\s+of)\s+(' + DATE_PATTERN + ')', query, re.I)
    if snapshot and _question_date(snapshot.group(1)) != report:
        raise ValueError('Snapshot date differs from the question')
    for key in ('branch_code', 'zone_code', 'member_id', 'employee_code', 'product_name'):
        value = arguments.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()
                or not re.search(r'(?<![\w.-])' + re.escape(value) + r'(?![\w-])', query, re.I)):
            raise ValueError('Identifiers must appear explicitly in the question')
    for key in ('disbursed_from', 'disbursed_to'):
        if key in arguments:
            value = arguments[key]
            if not isinstance(value, str) or value not in dates or value > report:
                raise ValueError('Disbursement dates must be explicit and no later than the snapshot')
    if arguments.get('disbursed_from', '') > arguments.get('disbursed_to', '9999-12-31'):
        raise ValueError('Disbursement range is reversed')
    level = arguments.get('level', 'LOAN')
    sort = arguments.get('sort_by', 'DUEAMOUNT')
    if level not in LEVELS or sort not in SORTS or arguments.get('sort_order', 'DESC') not in {'ASC', 'DESC'}:
        raise ValueError('Unsupported native query operation')
    if level == 'LOAN' and sort == 'LOAN_COUNT':
        raise ValueError('Loan counts require grouping')
    count = arguments.get('top_n', 10)
    if type(count) is not int or not 1 <= count <= 1000:
        raise ValueError('Result limit must be from 1 to 1000')
    explicit = re.search(r'\btop\s+(\d+)\b', query, re.I)
    if explicit and int(explicit.group(1)) != count:
        raise ValueError('Result count differs from the question')
    if re.search(r'\b(?:exactly\s+one|only\s+one|single|top\s+one)\b', query, re.I) and count != 1:
        raise ValueError('The question requests one result')
    requested_level = re.search(
        r'\b(?:rank|ranking|top)\s+(?:the\s+)?(?:\d+\s+)?(?:overdue\s+)?'
        r'(members|branches|zones|areas|divisions|products|employees|loans)\b', query, re.I)
    labels = {'members': 'MEMBER', 'branches': 'BRANCH', 'zones': 'ZONE', 'areas': 'AREA',
              'divisions': 'DIVISION', 'products': 'PRODUCT', 'employees': 'EMPLOYEE', 'loans': 'LOAN'}
    if requested_level and level != labels[requested_level.group(1).lower()]:
        raise ValueError('Ranking level differs from the question')
    return dict(arguments)


def _envelope(raw):
    data = raw.get('structuredContent')
    if data is None:
        content = [x.get('text') for x in raw.get('content', []) if x.get('type') == 'text']
        if len(content) != 1:
            raise ValueError('Expected one native query response')
        data = content[0]
    for _ in range(8):
        if isinstance(data, str):
            data = json.loads(data, parse_float=Decimal)
        elif isinstance(data, dict):
            if data.get('error') or data.get('isError') or data.get('success') is False:
                raise ValueError('Backend reported a query error')
            if 'success' in data:
                return data
            if 'structuredContent' in data:
                data = data['structuredContent']
            elif isinstance(data.get('content'), list) and len(data['content']) == 1:
                data = data['content'][0].get('text')
            else:
                data = data.get('data', data.get('result'))
        else:
            break
    raise ValueError('Missing native query envelope')


def _number(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError('Invalid numeric value')
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError('Invalid numeric value')
        return result
    except InvalidOperation as exc:
        raise ValueError('Invalid numeric value') from exc


def present_native_query(raw, arguments):
    data = _envelope(raw)
    expected = {'report_date': arguments['report_date'], 'level': arguments.get('level', 'LOAN'),
                'sort_by': arguments.get('sort_by', 'DUEAMOUNT'),
                'sort_order': arguments.get('sort_order', 'DESC'), 'top_n': arguments.get('top_n', 10)}
    if data.get('success') is not True or any(data.get(k) != v or type(data.get(k)) is not type(v)
                                           for k, v in expected.items()):
        raise ValueError('Query response metadata differs from the request')
    scope = data.get('scope')
    if (not isinstance(scope, dict)
            or any(scope.get(k) != arguments.get(k) for k in ('branch_code', 'zone_code'))
            or type(scope.get('authorized_branch_count')) is not int
            or scope['authorized_branch_count'] < 1):
        raise ValueError('Backend did not confirm the requested authorized scope')
    rows = data.get('data')
    if (not isinstance(rows, list) or type(data.get('row_count')) is not int
            or len(rows) != data['row_count'] or len(rows) > expected['top_n']):
        raise ValueError('Invalid bounded query result')
    loan = expected['level'] == 'LOAN'
    metric = expected['sort_by']
    metric = 'Outstanding(P+I)' if loan and metric == 'OUTSTANDING_PI' else metric
    columns = (['RANK_NO', 'BRANCHCODE', 'MEMBERID', 'PRODUCTNAME', 'DUEAMOUNT',
                'OUTSTANDINGP', 'Outstanding(P+I)', 'DUE_DAYS'] if loan else
               ['RANK_NO', 'BRANCHCODE', 'ENTITY_CODE', 'DUEAMOUNT', 'OUTSTANDINGP',
                'OUTSTANDING_PI', 'DUE_DAYS', 'LOAN_COUNT'])
    if not loan and expected['level'] not in {'MEMBER', 'EMPLOYEE'}:
        columns.insert(3, 'ENTITY_NAME')
    values, seen = [], set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or type(row.get('RANK_NO')) is not int or row['RANK_NO'] != index:
            raise ValueError('Invalid backend rank positions')
        values.append(_number(row.get(metric)))
        if loan:
            for key, result_key in [('branch_code', 'BRANCHCODE'), ('zone_code', 'ZONECODE'), ('member_id', 'MEMBERID')]:
                if key in arguments and row.get(result_key) != arguments[key]:
                    raise ValueError('Backend returned rows outside requested scope')
        else:
            identity = (row.get('BRANCHCODE'), row.get('ENTITY_CODE'))
            if not isinstance(identity[1], str) or not identity[1] or identity in seen:
                raise ValueError('Missing or duplicate grouped identity')
            seen.add(identity)
            if (expected['level'] in {'MEMBER', 'BRANCH', 'EMPLOYEE'}
                    and arguments.get('branch_code') and row.get('BRANCHCODE') != arguments['branch_code']):
                raise ValueError('Backend returned another branch')
            if expected['level'] == 'BRANCH' and row['ENTITY_CODE'] != row.get('BRANCHCODE'):
                raise ValueError('Inconsistent grouped branch identity')
            for level, argument in [('MEMBER', 'member_id'), ('EMPLOYEE', 'employee_code'), ('ZONE', 'zone_code')]:
                if expected['level'] == level and arguments.get(argument) and row['ENTITY_CODE'] != arguments[argument]:
                    raise ValueError('Grouped entity differs from the requested filter')
    if values != sorted(values, reverse=expected['sort_order'] == 'DESC'):
        raise ValueError('Backend ordering differs from the request')
    shown = []
    for row in rows[:100]:
        item = {}
        for key in columns:
            if key not in row:
                continue
            value = row[key]
            if value is not None and key in {'DUEAMOUNT', 'OUTSTANDINGP', 'OUTSTANDING_PI',
                    'Outstanding(P+I)', 'DUE_DAYS', 'LOAN_COUNT'}:
                value = format(_number(value), 'f')
            elif value is not None and (isinstance(value, (list, dict, bool))
                    or not isinstance(value, (str, int))):
                raise ValueError('Invalid display value')
            item[key] = value
        shown.append(item)
    columns = [key for key in columns if any(key in row for row in shown)]
    note = (f"As of {expected['report_date']}: {expected['level'].lower()} results ordered by "
            f"{expected['sort_by']} {expected['sort_order']}. Returned {len(rows)}; showing {len(shown)}. "
            f"Limited to the backend-authorized branch scope ({scope['authorized_branch_count']} branch(es)); "
            "this does not establish complete zone coverage.")
    if arguments.get('disbursed_from') or arguments.get('disbursed_to'):
        note += (' Loan disbursement dates: ' + arguments.get('disbursed_from', 'unbounded')
                 + ' through ' + arguments.get('disbursed_to', 'unbounded') + '.')
    lines = [note]
    for row in shown:
        lines.append(' · '.join(f'{key}: {value}' for key, value in row.items()))
    return {'answer': '\n'.join(lines), 'raw': {'structuredContent': {
        'columns': columns, 'rows': shown, 'row_count': len(rows), 'truncated': len(rows) > 100,
        'report_date': expected['report_date'], 'level': expected['level'], 'scope': scope,
        'filters': {key: value for key, value in arguments.items() if key not in {'level','sort_by','sort_order','top_n'}},
        'ranking_source': 'oracle_native_query'}}}
