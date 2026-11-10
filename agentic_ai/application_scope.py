"""App-owned authorization for trusted native loan query endpoints.

The application derives the scope from its database, never model/user input.
NiFi receives either the application branch list or a checked native branch_code.
The backend must apply that filter before aggregation and ranking.
"""
from dataclasses import dataclass

SCOPE_FIELD = 'app_branch_codes'


@dataclass
class ApplicationLoanGrant:
    access: dict
    branches: list
    query: dict
    native_branch_filter: bool = False


def schema_supported(schema):
    props = schema.get('properties', {})
    field = props.get(SCOPE_FIELD, {})
    scope_supported = (SCOPE_FIELD not in props and SCOPE_FIELD not in schema.get('required', [])
                       or field.get('type') == 'array'
                       and field.get('items', {}).get('type') == 'string'
                       and SCOPE_FIELD in schema.get('required', []))
    return (schema.get('type') == 'object' and scope_supported
            and props.get('report_date', {}).get('type') == 'string'
            and props.get('branch_code', {}).get('type') == 'string'
            and 'loan_access_token' not in schema.get('required', []))


def grant(access, name, arguments, tool, backend_arguments=None):
    from .loan_access import LoanAccessDenied, LoanAccessUnavailable
    from .loan_scope_backend import canonical
    from .native_loan_query import supports_native_query
    if not schema_supported(tool.inputSchema) or not supports_native_query(tool, {name}):
        raise LoanAccessUnavailable('Application-managed reporting requires the native loan query contract with branch_code and report_date, without a required loan_access_token.')
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
    native_branch_filter = SCOPE_FIELD not in tool.inputSchema.get('properties', {})
    if native_branch_filter:
        # A single native branch filter cannot represent a partial multi-branch
        # policy. Never dispatch a broad request and filter its top-N afterwards.
        if len(branches) != 1:
            raise LoanAccessDenied('Choose one permitted branch for this report. This processor accepts branch_code, not a multi-branch access list.')
        query['branch_code'] = branches[0]
    return ApplicationLoanGrant({'subject': access['subject'], 'version': access['version'],
        'mode': 'APP', 'tool': name, 'branches': branches}, branches, query, native_branch_filter)


def validate_response(raw, grant):
    from .native_loan_query import _envelope
    from .loan_access import LoanAccessDenied
    data = _envelope(raw)
    scope = data.get('scope')
    if (data.get('success') is not True or not isinstance(scope, dict)
            or (not grant.native_branch_filter and scope.get('authorization_mode') != 'APPLICATION_SCOPE')
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
