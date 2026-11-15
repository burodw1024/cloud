"""App-owned authorization for trusted native loan and savings endpoints.

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
    report_kind: str = 'loan'


def savings_schema_supported(tool):
    from .business_question_help import is_savings_report_tool
    schema = tool.inputSchema
    props = schema.get('properties', {})
    operations = props.get('operation', {}).get('enum', [])
    supported = {'period-summary', 'branch-summary', 'zone-summary', 'zone-branches',
                 'branch-product-summary', 'zone-product-summary'}
    return (is_savings_report_tool(tool) and bool(operations)
            and all(isinstance(operation, str) and operation in supported for operation in operations)
            and all(props.get(key, {}).get('type') == 'string' for key in ('branch_code', 'zone_code'))
            and {'operation', 'from_date', 'to_date_exclusive'} <= set(schema.get('required', []))
            and SCOPE_FIELD not in props and SCOPE_FIELD not in schema.get('required', [])
            and 'loan_access_token' not in schema.get('required', []))


def tool_supported(tool):
    from .native_loan_query import supports_native_query
    return (savings_schema_supported(tool)
            or schema_supported(tool.inputSchema) and supports_native_query(tool, {tool.name}))


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
    savings = savings_schema_supported(tool)
    if not tool_supported(tool):
        raise LoanAccessUnavailable('Application-managed reporting requires a supported native loan or savings contract with an exact branch_code filter, without a required loan_access_token.')
    query = dict(arguments if backend_arguments is None else backend_arguments)
    if canonical(query) != canonical(arguments):
        raise LoanAccessDenied('Application-managed query parameters changed before authorization.')
    if any(k in query for k in ('branchCode', 'zoneCode', 'zoneName')):
        raise LoanAccessDenied('Use the native branch_code and zone_code fields.')
    if savings:
        from datetime import date
        from jsonschema import ValidationError
        from .mcp_gateway import _validator
        try:
            _validator(tool.inputSchema).validate(query)
            start, end = (date.fromisoformat(query[key]) for key in ('from_date', 'to_date_exclusive'))
            if (start.isoformat() != query['from_date'] or end.isoformat() != query['to_date_exclusive']
                    or not 0 < (end - start).days <= 366):
                raise ValueError
        except (ValueError, TypeError, KeyError, ValidationError) as exc:
            raise LoanAccessDenied('Use a supported savings operation and an ISO date range of 1 to 366 days.') from exc
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
        'mode': 'APP', 'tool': name, 'branches': branches}, branches, query, native_branch_filter,
        'savings' if savings else 'loan')


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
    if grant.report_kind == 'savings':
        if any(scope.get(key) != grant.query[key] for key in ('from_date', 'to_date_exclusive')):
            raise LoanAccessDenied('The savings backend did not confirm the requested date range.')
    elif grant.query.get('level', 'LOAN') == 'LOAN':
        for row in data['data']:
            if not isinstance(row, dict) or row.get('BRANCHCODE') not in allowed:
                raise LoanAccessDenied('Loan rows must confirm their assigned branch.')
