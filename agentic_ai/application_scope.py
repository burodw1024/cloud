"""App-owned authorization for a trusted QueryLoanDueAppScope endpoint.

The application derives the scope from its database, never model/user input.
NiFi receives an execution filter; it does not authenticate users in this mode.
"""
from dataclasses import dataclass

SCOPE_FIELD = 'app_branch_codes'


@dataclass
class ApplicationLoanGrant:
    access: dict
    branches: list
    query: dict


def schema_supported(schema):
    props = schema.get('properties', {})
    field = props.get(SCOPE_FIELD, {})
    return (schema.get('type') == 'object' and field.get('type') == 'array'
            and field.get('items', {}).get('type') == 'string'
            and SCOPE_FIELD in schema.get('required', [])
            and props.get('report_date', {}).get('type') == 'string'
            and props.get('branch_code', {}).get('type') == 'string'
            and 'loan_access_token' not in schema.get('required', []))


def grant(access, name, arguments, tool, backend_arguments=None):
    from .loan_access import LoanAccessDenied, LoanAccessUnavailable
    from .loan_scope_backend import canonical
    from .native_loan_query import supports_native_query
    if not schema_supported(tool.inputSchema) or not supports_native_query(tool, {name}):
        raise LoanAccessUnavailable('Application-managed reporting requires the QueryLoanDueAppScope contract with required app_branch_codes. Update the MCP schema and backend together.')
    query = dict(arguments if backend_arguments is None else backend_arguments)
    if canonical(query) != canonical(arguments):
        raise LoanAccessDenied('Application-managed query parameters changed before authorization.')
    if any(k in query for k in ('branchCode', 'zoneCode', 'zoneName')):
        raise LoanAccessDenied('Use the native branch_code and zone_code fields.')
    rows = access['directory']
    if query.get('zone_code'):
        rows = [row for row in rows if row['zone__code'] == query['zone_code']]
    if query.get('branch_code'):
        rows = [row for row in rows if row['code'] == query['branch_code']]
    if not rows:
        raise LoanAccessDenied('The requested branch or zone is outside your assigned loan data access.')
    branches = sorted({row['code'] for row in rows})
    return ApplicationLoanGrant({'subject': access['subject'], 'version': access['version'],
        'mode': 'APP', 'tool': name, 'branches': branches}, branches, query)


def validate_response(raw, grant):
    from .native_loan_query import _envelope
    from .loan_access import LoanAccessDenied
    data = _envelope(raw)
    scope = data.get('scope')
    if (data.get('success') is not True or not isinstance(scope, dict)
            or scope.get('authorization_mode') != 'APPLICATION_SCOPE'
            or type(scope.get('authorized_branch_count')) is not int
            or scope['authorized_branch_count'] != len(grant.branches)
            or any(scope.get(k) != grant.query.get(k) for k in ('branch_code', 'zone_code'))
            or not isinstance(data.get('data'), list)):
        raise LoanAccessDenied('The backend did not confirm the application-supplied query scope.')
    allowed = set(grant.branches)
    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'loan_access_token':
                    raise LoanAccessDenied('The backend returned an authorization credential.')
                if key in {'BRANCHCODE', 'branch_code', 'branchCode'} and item is not None and item not in allowed:
                    raise LoanAccessDenied('The backend returned data outside the application-assigned scope.')
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(data)
    if grant.query.get('level', 'LOAN') == 'LOAN':
        for row in data['data']:
            if not isinstance(row, dict) or row.get('BRANCHCODE') not in allowed:
                raise LoanAccessDenied('Loan rows must confirm their assigned branch.')
