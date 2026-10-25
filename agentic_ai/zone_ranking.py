"""Verified presentation contract for database-ranked, authorized zone reports."""
from datetime import date
from decimal import Decimal, InvalidOperation
import re

TOOL_NAME = 'zone-overdue-ranking'
INPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'zoneCode': {'type': 'string', 'minLength': 1, 'maxLength': 64,
                     'description': 'Exact zone code explicitly supplied by the employee. Preserve leading zeros.'},
        'zoneName': {'type': 'string', 'minLength': 1, 'maxLength': 150,
                     'description': 'Exact zone name supplied by the employee. Backend must resolve uniquely or return clarification.'},
        'reportDate': {'type': 'string', 'format': 'date'},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                  'description': 'Top member count explicitly requested; default 10.'},
    },
    'required': ['reportDate', 'limit'],
    'oneOf': [{'required': ['zoneCode'], 'not': {'required': ['zoneName']}},
              {'required': ['zoneName'], 'not': {'required': ['zoneCode']}}],
    'additionalProperties': False,
}
DESCRIPTION = ('Rank the top overdue members across authorized branches in one zone as of an explicit date. '
               'Accept zone code OR name. Rank members by combined DUEAMOUNT descending, '
               'with branchCode then memberId ascending for ties. Default top 10, maximum 100. '
               'Database aggregates loans before limiting. This is not top members per branch or a branch ranking. '
               'Backend enforces the employee branch scope and resolves zone names uniquely.')
COLUMNS = ['RANK', 'ZONECODE', 'ZONENAME', 'BRANCHCODE', 'BRANCHNAME', 'MEMBERID',
           'DUEAMOUNT', 'OUTSTANDINGP', 'DUE_DAYS']


def validate_request(arguments, query):
    from .mcp_gateway import _validator
    _validator(INPUT_SCHEMA).validate(arguments)
    if date.fromisoformat(arguments['reportDate']).isoformat() != arguments['reportDate']:
        raise ValueError('Invalid report date')
    for key in ('zoneCode', 'zoneName', 'reportDate'):
        if key in arguments:
            value = arguments[key]
            if value != value.strip() or not re.search(r'(?<!\w)' + re.escape(value) + r'(?!\w)', query, re.I):
                raise ValueError('State the exact zone and ISO date in the question')
    counts = re.findall(r'\btop\s+(\d+)\b', query, re.I)
    if counts and (len(set(counts)) != 1 or int(counts[0]) != arguments['limit']):
        raise ValueError('Requested limit does not match the question')
    if not counts and arguments['limit'] != 10:
        raise ValueError('Default limit is 10')


def _text(value):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 150 or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid report identifier')
    return value


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError('Money must be a decimal string or integer, never a float')
    if not re.fullmatch(r'[0-9]{1,30}(?:\.[0-9]{1,8})?', str(value)):
        raise ValueError('Invalid decimal amount')
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError('Invalid amount') from None
    if not amount.is_finite() or amount < 0 or len(str(value)) > 64:
        raise ValueError('Invalid amount')
    return amount


def present_ranking(raw, arguments):
    """Do not accept a partial branch report or calculate rankings with an LLM."""
    import json
    data = raw.get('structuredContent')
    if data is None:
        texts = [x['text'] for x in raw.get('content', []) if x.get('type') == 'text']
        if len(texts) != 1:
            raise ValueError('Expected ranking envelope')
        data = json.loads(texts[0])
    if not isinstance(data, dict):
        raise ValueError('Expected ranking envelope')
    if data.get('status') == 'ambiguous_zone':
        return {'kind': 'clarification', 'clarification_required': True,
                'answer': 'More than one zone matches that name. Please provide the unique zone code.',
                'raw': {'structuredContent': {'rows': []}}}
    expected = {'status': 'ok', 'reportDate': arguments['reportDate'], 'limit': arguments['limit'],
                'ranking': 'member_due_desc', 'aggregation': 'branch_member',
                'scopeComplete': True, 'authorizationApplied': True}
    if any(data.get(key) != value or type(data.get(key)) is not type(value) for key, value in expected.items()):
        raise ValueError('Incomplete, unauthorized or mismatched ranking contract')
    code, name = _text(data.get('zoneCode')), _text(data.get('zoneName'))
    if ('zoneCode' in arguments and code != arguments['zoneCode']) or ('zoneName' in arguments and name.casefold() != arguments['zoneName'].casefold()):
        raise ValueError('Zone mismatch')
    rows, count = data.get('rows'), data.get('eligibleMemberCount')
    if type(count) is not int or count < 0 or not isinstance(rows, list) or len(rows) != min(count, arguments['limit']):
        raise ValueError('Truncated or invalid ranking')
    displayed, keys, seen = [], [], set()
    for position, row in enumerate(rows, 1):
        if not isinstance(row, dict) or any(key not in row for key in COLUMNS):
            raise ValueError('Missing ranking fields')
        if type(row['RANK']) is not int or row['RANK'] != position or row['ZONECODE'] != code or row['ZONENAME'] != name:
            raise ValueError('Invalid rank or zone')
        branch, member = _text(row['BRANCHCODE']), _text(row['MEMBERID'])
        _text(row['BRANCHNAME'])
        due = _money(row['DUEAMOUNT'])
        _money(row['OUTSTANDINGP'])
        if due <= 0 or type(row['DUE_DAYS']) is not int or row['DUE_DAYS'] < 0 or (branch, member) in seen:
            raise ValueError('Invalid member aggregate')
        seen.add((branch, member))
        keys.append((-due, branch, member))
        displayed.append({column: row[column] for column in COLUMNS})
    if keys != sorted(keys):
        raise ValueError('Ranking order mismatch')
    answer = (f'Top {len(rows)} of {count} overdue members in zone {name} ({code}), '
              f'as of {arguments["reportDate"]}, within your authorized branches. '
              'Ranked by combined overdue amount per branch/member; ties use branch code then member ID. '
              'These are review priorities, not findings of misconduct.')
    for row in displayed:
        answer += f'\n{row["RANK"]}. Branch {row["BRANCHCODE"]} · member {row["MEMBERID"]} · due {row["DUEAMOUNT"]} · overdue days {row["DUE_DAYS"]}'
    return {'answer': answer, 'raw': {'structuredContent': {'columns': COLUMNS, 'rows': displayed,
            'row_count': count, 'truncated': count > len(rows), 'scope': 'authorized zone overdue members',
            'ranking': 'member_due_desc', 'report_date': arguments['reportDate']}}}
