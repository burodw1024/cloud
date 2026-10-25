"""Native operation extension for the existing loan-due-report REST/MCP tool.

No virtual ranking parameters: advertise them only after NiFi/MCP declares them.
"""

RANK_OPERATION = 'rank_overdue_members'
INPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'branchCode': {'type': 'string'},
        'reportDate': {'type': 'string'},
        'operation': {'type': 'string', 'enum': ['report', RANK_OPERATION], 'default': 'report'},
        'zoneCode': {'type': 'string', 'minLength': 1, 'maxLength': 64},
        'zoneName': {'type': 'string', 'minLength': 1, 'maxLength': 150},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 10},
    },
    'allOf': [{
        'if': {'properties': {'operation': {'const': RANK_OPERATION}}, 'required': ['operation']},
        'then': {'required': ['reportDate', 'limit'],
                 'properties': {'reportDate': {'format': 'date'}},
                 'oneOf': [{'required': ['zoneCode'], 'not': {'required': ['zoneName']}},
                           {'required': ['zoneName'], 'not': {'required': ['zoneCode']}}],
                 'not': {'required': ['branchCode']}},
        'else': {'not': {'anyOf': [{'required': [key]} for key in ('zoneCode', 'zoneName', 'limit')]}},
    }],
    'additionalProperties': False,
}
DESCRIPTION = (
    'Retrieve the loan report with branchCode/reportDate (operation omitted or report). '
    'For zone-wide top overdue member ranking use operation=rank_overdue_members, '
    'explicit reportDate, zoneCode OR exact zoneName, and limit (default 10, maximum 100). '
    'Rank by combined overdue amount per branch/member across the authorized zone. '
    'The application validates database-ranked results or computes exact Decimal totals '
    'from a complete, authorized loan-grain snapshot before limiting. '
    'Never substitute ordinary report rows for a ranking. No per-branch top-N or other ranking metric.'
)


def supports_ranking(tool, report_names):
    if tool.name not in report_names:
        return False
    properties = tool.inputSchema.get('properties', {})
    return (tool.inputSchema.get('type') == 'object'
            and properties.get('operation', {}).get('type') == 'string'
            and RANK_OPERATION in properties.get('operation', {}).get('enum', [])
            and properties.get('reportDate', {}).get('type') == 'string'
            and properties.get('limit', {}).get('type') == 'integer'
            and any(properties.get(key, {}).get('type') == 'string' for key in ('zoneCode', 'zoneName')))


def prepare_tools(tools, report_names):
    """Improve native operation discovery; never invent unsupported backend inputs."""
    return [tool.model_copy(update={'description': (tool.description or '') + ' ' + DESCRIPTION})
            if supports_ranking(tool, report_names) else tool for tool in tools]


def ranking_candidates(tools, report_names):
    # Prefer the existing loan report. Keep explicitly registered legacy tools working.
    from .zone_ranking import TOOL_NAME
    native = [tool for tool in tools if supports_ranking(tool, report_names)]
    return native or [tool for tool in tools if tool.name == TOOL_NAME]


def is_ranking_call(name, arguments, tools, report_names):
    from .zone_ranking import TOOL_NAME
    if name == TOOL_NAME:
        return True
    tool = next((tool for tool in tools if tool.name == name), None)
    return (tool is not None and supports_ranking(tool, report_names)
            and arguments.get('operation') == RANK_OPERATION)


def validate_ranking_call(name, arguments, tools, report_names, query):
    from .mcp_gateway import _validator
    from .zone_ranking import TOOL_NAME, validate_request
    tool = next(tool for tool in tools if tool.name == name)
    _validator(tool.inputSchema).validate(arguments)
    canonical = dict(arguments)
    if name != TOOL_NAME:
        if not supports_ranking(tool, report_names) or canonical.pop('operation', None) != RANK_OPERATION:
            raise ValueError('Backend does not declare the ranking operation')
    validate_request(canonical, query)
    return canonical
