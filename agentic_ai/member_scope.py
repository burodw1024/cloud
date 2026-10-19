"""Typed member scope for authorized loan reports, independent of question wording."""
from copy import deepcopy
import re

ID_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'


def scoped_tools(tools, report_names):
    """Advertise app filtering only for configured branch/date report tools."""
    prepared = []
    for tool in tools:
        schema = deepcopy(tool.inputSchema)
        properties = schema.get('properties', {})
        if (tool.name not in report_names or schema.get('type') != 'object'
                or not all(properties.get(key, {}).get('type') == 'string'
                           for key in ('branchCode', 'reportDate'))):
            prepared.append(tool)
            continue
        native = 'memberId' in properties
        if not native:
            properties['memberId'] = {
                'type': 'string', 'pattern': ID_PATTERN,
                'description': 'Exact member ID supplied by the user; preserve leading zeros. '
                               'The application filters the returned branch report by this ID.'}
            schema['properties'] = properties
        # A historical member inquiry must have an explicit branch and date.
        schema.setdefault('allOf', []).append({
            'if': {'required': ['memberId']},
            'then': {'required': ['branchCode', 'reportDate']}})
        description = (tool.description or '') + (
            ' Member-specific loan products, disbursement, maturity, outstanding, due, installment '
            'and savings fields can be retrieved by supplying memberId, branchCode and reportDate. '
            'Member rows are verified by the application before display. '
            + ('The backend also declares memberId.' if native else
               'The backend receives branch/date; member filtering happens in the application, not Oracle.'))
        prepared.append(tool.model_copy(update={'inputSchema': schema, 'description': description}))
    return prepared


def member_call(name, arguments, tools, report_names, query):
    """Separate local-only scope from parameters actually supported by NiFi."""
    backend = dict(arguments)
    if 'memberId' not in arguments:
        return backend, None
    if name not in report_names:
        return backend, None
    member = arguments['memberId']
    if not isinstance(member, str) or not re.fullmatch(ID_PATTERN, member):
        raise ValueError('Provide a valid member ID as a string, preserving leading zeros.')
    if not re.search(r'(?<![A-Za-z0-9._-])' + re.escape(member) + r'(?=$|[^A-Za-z0-9._-]|\.(?=$|\s))', query):
        raise ValueError('The member ID must appear exactly in your question; it cannot be inferred.')
    branch, date = arguments.get('branchCode'), arguments.get('reportDate')
    if not isinstance(branch, str) or not branch.strip() or not isinstance(date, str):
        raise ValueError('Provide the branch code and report date for a member inquiry.')
    from datetime import date as date_type
    try:
        if date_type.fromisoformat(date).isoformat() != date:
            raise ValueError
    except ValueError:
        raise ValueError('Provide reportDate in YYYY-MM-DD format.') from None
    for supplied in (branch, date):
        if not re.search(r'(?<![A-Za-z0-9._-])' + re.escape(supplied) + r'(?=$|[^A-Za-z0-9._-]|\.(?=$|\s))', query):
            raise ValueError('Provide the branch and ISO report date explicitly in your question.')
    tool = next(tool for tool in tools if tool.name == name)
    native = 'memberId' in tool.inputSchema.get('properties', {})
    if not native:
        backend.pop('memberId')
    # Validate the outgoing request against the original, unmodified tool schema.
    from .mcp_gateway import _validator
    _validator(tool.inputSchema).validate(backend)
    return backend, {'member_id': member, 'branch_code': branch, 'report_date': date,
                     'mode': 'backend_member_filter' if native else 'application_report_filter'}


def filter_member_rows(rows, scope):
    """Exact comparison only; no substring, prefix or numeric ID coercion."""
    member, branch = scope['member_id'], scope['branch_code']
    if any(not isinstance(row.get('MEMBERID'), str) for row in rows):
        raise ValueError('Member-scoped reports must include string MEMBERID values on every row.')
    if scope['mode'] == 'backend_member_filter' and any(row['MEMBERID'] != member for row in rows):
        raise ValueError('The backend returned rows outside the requested member scope.')
    selected = [row for row in rows if row['MEMBERID'] == member]
    if any(row.get('BRANCHCODE') != branch for row in selected):
        raise ValueError('The report did not verify the requested branch scope.')
    return selected
