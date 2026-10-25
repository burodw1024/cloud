"""Exact member totals over a verified complete loan snapshot, before top-N.

Never infer completeness, permissions, currency or loan identity from columns.
"""
from decimal import Decimal, localcontext
import re


def aggregate_complete_report(data, arguments):
    from .zone_ranking import _money, _text, COLUMNS
    expected = {'status': 'ok', 'resultType': 'complete_loan_report',
                'reportDate': arguments['reportDate'], 'limit': arguments['limit'],
                'scopeComplete': True, 'authorizationApplied': True, 'rowGrain': 'loan'}
    if any(data.get(key) != value or type(data.get(key)) is not type(value) for key, value in expected.items()):
        raise ValueError('Complete authorized loan snapshot metadata is required')
    code, name = _text(data.get('zoneCode')), _text(data.get('zoneName'))
    if ('zoneCode' in arguments and arguments['zoneCode'] != code
            or 'zoneName' in arguments and arguments['zoneName'].casefold() != name.casefold()):
        raise ValueError('Snapshot zone mismatch')
    currency = data.get('currency')
    if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
        raise ValueError('One declared report currency is required')
    rows, count = data.get('rows'), data.get('sourceRowCount')
    if not isinstance(rows, list) or type(count) is not int or count != len(rows):
        raise ValueError('Snapshot is truncated or missing its complete row count')
    key_columns = data.get('loanKeyColumns')
    if (not isinstance(key_columns, list) or not 1 <= len(key_columns) <= 8
            or any(not isinstance(key, str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,63}', key) for key in key_columns)
            or len(key_columns) != len(set(key_columns))):
        raise ValueError('Backend must declare a verified unique loan key')
    seen, key_types, groups, branch_names = set(), {}, {}, {}
    # Inputs have at most 38 significant digits. The bounded MCP response cannot
    # contain enough rows to exhaust this precision; no currency rounding occurs.
    with localcontext() as context:
        context.prec = 64
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid loan row')
            branch, member = _text(row.get('BRANCHCODE')), _text(row.get('MEMBERID'))
            branch_name = _text(row.get('BRANCHNAME'))
            if row.get('ZONENAME') != name or ('ZONECODE' in row and row['ZONECODE'] != code):
                raise ValueError('Loan row is outside the verified zone')
            if 'CURRENCY' in row and row['CURRENCY'] != currency:
                raise ValueError('Mixed report currencies')
            if branch in branch_names and branch_names[branch] != branch_name:
                raise ValueError('Inconsistent branch identity')
            branch_names[branch] = branch_name
            key = []
            for column in key_columns:
                value = row.get(column)
                if type(value) not in (str, int) or isinstance(value, str) and (not value or value != value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value)):
                    raise ValueError('Missing or invalid loan key')
                if column in key_types and key_types[column] is not type(value):
                    raise ValueError('Inconsistent loan key types')
                key_types[column] = type(value)
                key.append(value)
            key = tuple(key)
            if key in seen:
                raise ValueError('Duplicate loan identity; totals would be unreliable')
            seen.add(key)
            due, principal = _money(row.get('DUEAMOUNT')), _money(row.get('OUTSTANDINGP'))
            days = row.get('DUE_DAYS')
            if type(days) is not int or days < 0:
                raise ValueError('Invalid overdue days')
            group = groups.setdefault((branch, member), {
                'ZONECODE': code, 'ZONENAME': name, 'BRANCHCODE': branch,
                'BRANCHNAME': branch_name, 'MEMBERID': member,
                'DUEAMOUNT': Decimal(0), 'OUTSTANDINGP': Decimal(0), 'DUE_DAYS': 0})
            group['DUEAMOUNT'] += due
            group['OUTSTANDINGP'] += principal
            group['DUE_DAYS'] = max(group['DUE_DAYS'], days)
        eligible = [row for row in groups.values() if row['DUEAMOUNT'] > 0]
        eligible.sort(key=lambda row: (-row['DUEAMOUNT'], row['BRANCHCODE'], row['MEMBERID']))
        displayed = []
        for rank, row in enumerate(eligible[:arguments['limit']], 1):
            item = {**row, 'RANK': rank, 'DUEAMOUNT': format(row['DUEAMOUNT'], 'f'),
                    'OUTSTANDINGP': format(row['OUTSTANDINGP'], 'f')}
            displayed.append({key: item[key] for key in COLUMNS})
    return {'status': 'ok', 'zoneCode': code, 'zoneName': name,
            'reportDate': arguments['reportDate'], 'limit': arguments['limit'],
            'ranking': 'member_due_desc', 'aggregation': 'branch_member',
            'scopeComplete': True, 'authorizationApplied': True,
            'currency': currency, 'eligibleMemberCount': len(eligible),
            'sourceRowCount': count, 'rows': displayed}
