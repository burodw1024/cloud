"""Resolve explicit positive field lists against an approved report vocabulary.

This is a presentation contract, not a natural-language SQL generator. Unknown
phrasing, calculations, negations and unsupported scope use the model path.
"""
import re

# Business labels for the existing report contract, never question/member-specific rules.
FIELD_LABELS = {
    'GS_BALANCE': ('GS balance', 'GS balances', 'general savings balance', 'general savings balances', 'general savings'),
    'CS_BALANCE': ('CS balance', 'CS balances', 'compulsory savings balance', 'compulsory savings balances', 'compulsory savings'),
    'OUTSTANDINGP': ('outstanding principal', 'principal outstanding'),
    'Outstanding(P+I)': ('total outstanding', 'outstanding including interest', 'outstanding incl. interest'),
    'DUEAMOUNT': ('due amount', 'due amounts', 'overdue amount', 'overdue amounts'),
    'DUE_DAYS': ('overdue days', 'due days', 'days overdue'),
    'DUEINSTNO_U': ('overdue installment count', 'overdue installment counts', 'due installment count'),
    'INSTALLMENT_AMOUNT': ('installment amount', 'installment amounts'),
    'PAYMENTFREQUENCY': ('payment frequency', 'payment frequencies'),
    'PRODUCTNAME': ('product name', 'product names', 'loan product', 'loan products'),
    'DISBURSE_AMOUNT': ('disbursed amount', 'disbursed amounts', 'disbursement amount', 'disbursement amounts'),
    'DISBURSEDATE': ('disbursement date', 'disbursement dates'),
    'MATURITYDATE': ('maturity date', 'maturity dates'),
    'BRANCHNAME': ('branch name', 'branch names'),
    'BRANCHCODE': ('branch code', 'branch codes'),
    'LOANCYCLE': ('loan cycle', 'loan cycles'),
    'REALIZABLE': ('realizable amount', 'realizable amounts'),
    'REALIZED': ('realized amount', 'realized amounts'),
}


def normalized(label):
    return re.sub(r'[\s_]+', ' ', label.strip()).casefold()


def explicit_fields(query, approved, scope, rows):
    """Return fields only when the entire projection and scope are understood."""
    match = re.fullmatch(r'\s*(?:show|display|list|give|retrieve)\s+(?:me\s+)?(?:only\s+)?(.+?)\s*[.!?]?\s*', query, re.I)
    if not match:
        return None
    expression = match.group(1).rstrip('.!?').strip()
    parts = re.split(r'\s+(?=for\b|in branch\b|as of\b)', expression, maxsplit=1, flags=re.I)
    fields = parts[0]
    suffix = parts[1] if len(parts) > 1 else ''
    if suffix:
        if scope:
            pattern = (r'for member\s+' + re.escape(scope['member_id']) + r'\s+in branch\s+'
                       + re.escape(scope['branch_code']) + r'\s+as of\s+' + re.escape(scope['report_date']))
            if not re.fullmatch(pattern, suffix, re.I):
                return None
        else:
            branch = re.fullmatch(r'(?:for|in) branch\s+([A-Za-z0-9_-]+)\s+as of\s+\d{4}-\d{2}-\d{2}', suffix, re.I)
            if not branch or not rows or any(row.get('BRANCHCODE') != branch.group(1) for row in rows):
                return None
    vocabulary = {normalized(column): column for column in approved}
    for column, labels in FIELD_LABELS.items():
        if column in approved:
            vocabulary.update({normalized(label): column for label in labels})
    fields = re.sub(r',\s*(?:and\b|&)\s*', ', ', fields, flags=re.I)
    fragments = re.split(r'\s*,\s*|\s+and\s+|\s*&\s*', fields, flags=re.I)
    selected = []
    for fragment in fragments:
        if not fragment.strip():  # Oxford comma before "and".
            continue
        column = vocabulary.get(normalized(fragment))
        if column is None:
            return None
        if column not in selected:
            selected.append(column)
    return selected or None
