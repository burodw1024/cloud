"""Project a scope-verified NiFi savings result into the workspace table."""
from decimal import Decimal

from .native_loan_query import _envelope

DIMENSIONS = {
    'zone_code': 'ZONECODE', 'zone_name': 'ZONENAME',
    'branch_code': 'BRANCHCODE', 'branch_name': 'BRANCHNAME',
    'item_code': 'ITEMCODE',
}
COUNTS = ('register_row_count', 'excluded_amount_rows', 'missing_amount_rows')
AMOUNTS = ('total_deposits', 'total_withdrawals', 'net_movement')
SAVINGS_MEASURES = {field.upper() for field in (*COUNTS, *AMOUNTS)}
DISPLAY_FIELDS = {
    **DIMENSIONS, 'register_row_count': 'REGISTER_ROW_COUNT',
    **{field: field.upper() for field in (*AMOUNTS, *COUNTS[1:])},
}


def present_savings(raw, operation):
    data = _envelope(raw)
    rows = data['data']
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError('Expected savings report rows')
    fields = [field for field in DISPLAY_FIELDS if any(field in row for row in rows)]
    if rows and not fields:
        raise ValueError('No supported savings fields returned')
    projected = []
    for row in rows[:100]:
        output = {}
        for field in fields:
            value = row.get(field)
            if value is not None:
                if field in COUNTS and (type(value) is not int or value < 0):
                    raise ValueError('Invalid savings row count')
                if field in AMOUNTS:
                    if type(value) not in (int, float, Decimal):
                        raise ValueError('Invalid savings amount')
                    amount = Decimal(str(value))
                    if not amount.is_finite():
                        raise ValueError('Invalid savings amount')
                    value = str(amount)
                if field in DIMENSIONS and not isinstance(value, str):
                    raise ValueError('Invalid savings report dimension')
            output[DISPLAY_FIELDS[field]] = value
        projected.append(output)
    scope = {key: data['scope'][key] for key in (
        'branch_code', 'zone_code', 'authorized_branch_count', 'from_date', 'to_date_exclusive'
    ) if key in data['scope']}
    start, end = scope['from_date'], scope['to_date_exclusive']
    answer = (f"Savings activity for branch {scope['branch_code']} from {start} through the day before {end}. "
              f'{len(projected)} of {len(rows)} returned rows shown.' if rows
              else f"No matching savings activity for branch {scope['branch_code']} from {start} through the day before {end}.")
    if rows and operation.startswith('zone-'):
        answer += ' Zone figures cover this branch only.'
    return {'answer': answer, 'raw': {'structuredContent': {
        'columns': [DISPLAY_FIELDS[field] for field in fields], 'rows': projected,
        'row_count': len(rows), 'truncated': len(rows) > 100, 'scope': scope,
    }}}
