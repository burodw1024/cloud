"""Deny-by-default, per-user loan scope and short-lived backend authorization."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
import time
import uuid

import jwt
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from .models import AgentTool, LoanAccessPolicy, LoanBranch, LoanZone
from .loan_scope_backend import TOKEN_FIELD, ISSUER, AUDIENCE, canonical, authorization_receipt


class LoanAccessDenied(ValueError):
    pass


class LoanAccessUnavailable(LoanAccessDenied):
    pass


LOCAL_FINANCIAL_TOOLS = {'nifi_tool', 'get_loan_balance', 'get_loan_due_details', 'get_loan_installment', 'get_savings_balance'}


def protected_tools():
    from chat.dynamic_config import get_setting
    from .zone_ranking import TOOL_NAME
    return {name.strip() for name in get_setting('MCP_REPORT_PROJECTION_TOOLS', 'loan-due-report').split(',') if name.strip()} | {TOOL_NAME, 'loan-due-report'} | LOCAL_FINANCIAL_TOOLS


def snapshot(user):
    if not getattr(user, 'is_authenticated', False):
        raise LoanAccessDenied('Sign in to access loan data.')
    if not get_user_model().objects.filter(pk=user.pk, is_active=True).exists():
        raise LoanAccessDenied('This account is inactive.')
    policy = LoanAccessPolicy.objects.filter(user_id=user.pk).first()
    if not policy or policy.mode == 'NONE':
        raise LoanAccessDenied('No loan data access is assigned. Contact your access administrator.')
    if policy.expires_at and policy.expires_at <= timezone.now():
        raise LoanAccessDenied('Your loan data access has expired.')
    branches = LoanBranch.objects.filter(is_active=True, zone__is_active=True)
    if policy.mode == 'SELECTED':
        branches = branches.filter(Q(pk__in=policy.branches.values('pk')) | Q(zone__in=policy.zones.values('pk')))
    elif policy.mode != 'ALL':
        raise LoanAccessDenied('Invalid loan access policy.')
    directory = list(branches.order_by('code').values('code', 'zone__code', 'zone__name'))
    if not directory or len(directory) > 5000:
        raise LoanAccessDenied('No active branches are available, or the authorization scope exceeds 5000 branches.')
    state = {'user': str(user.pk), 'mode': policy.mode, 'updated': policy.updated_at.isoformat(),
             'expires': policy.expires_at.isoformat() if policy.expires_at else None, 'directory': directory}
    version = hashlib.sha256(canonical(state).encode()).hexdigest()
    return {'subject': str(user.pk), 'mode': policy.mode, 'version': version,
            'directory': directory, 'expires_at': policy.expires_at}


def current_access(user, metadata):
    try:
        valid = metadata['subject'] == str(user.pk) and snapshot(user)['version'] == metadata['version']
        if valid and metadata.get('mode') == 'FIXED':
            record = AgentTool.objects.filter(name=metadata.get('tool'), is_active=True).first()
            valid = bool(record and record.loan_access_mode == 'FIXED'
                         and sorted(record.fixed_branch_codes) == metadata.get('branches'))
        return valid
    except (LoanAccessDenied, KeyError, TypeError, AttributeError):
        return False


def own_access_summary(user):
    try:
        access = snapshot(user)
        codes = [row['code'] for row in access['directory']]
        return {'label': 'All active branches and zones' if access['mode'] == 'ALL' else 'Selected branches and zones',
                'count': len(codes), 'codes': ', '.join(codes[:20]), 'more': len(codes) > 20,
                'example_branch': codes[0], 'example_zone': access['directory'][0]['zone__code']}
    except LoanAccessDenied as exc:
        return {'label': str(exc), 'count': 0}


def strip_server_authorization(tools):
    prepared = []
    for tool in tools:
        schema = deepcopy(tool.inputSchema)
        schema.get('properties', {}).pop(TOKEN_FIELD, None)
        if 'required' in schema:
            schema['required'] = [key for key in schema['required'] if key != TOKEN_FIELD]
        prepared.append(tool.model_copy(update={'inputSchema': schema}))
    return prepared


@dataclass
class LoanGrant:
    token: str
    receipt: dict
    access: dict


@dataclass
class FixedLoanGrant:
    access: dict
    branches: list


def authorize(user, name, arguments, tool, backend_arguments=None):
    forbidden = {TOKEN_FIELD, 'auth.branch_codes', 'authorized_branches'}
    if forbidden.intersection(arguments) or forbidden.intersection(backend_arguments or {}):
        raise LoanAccessDenied('Authorization cannot be supplied in a question or tool arguments.')
    for key in ('branch_code', 'branchCode', 'zone_code', 'zoneCode', 'zoneName'):
        if key in arguments and (not isinstance(arguments[key], str) or not arguments[key].strip()
                                 or arguments[key] != arguments[key].strip()):
            raise LoanAccessDenied('Use exact nonempty branch and zone identifiers as strings, preserving leading zeros.')
    if ('branch_code' in arguments and 'branchCode' in arguments) or ('zone_code' in arguments and 'zoneCode' in arguments):
        raise LoanAccessDenied('Use the declared branch and zone parameter contract without duplicate aliases.')
    access = snapshot(user)
    record = AgentTool.objects.filter(name=name).first()
    if record and record.loan_access_mode == 'FIXED':
        record.full_clean()
        schema = getattr(tool, 'inputSchema', {})
        if TOKEN_FIELD in schema.get('required', []):
            raise LoanAccessUnavailable('This backend requires signed access. Select signed mode for this tool.')
        fixed = sorted(record.fixed_branch_codes)
        directory = {row['code']: row for row in access['directory']}
        # A branch filter cannot safely narrow a backend's aggregate access scope.
        if not set(fixed).issubset(directory):
            raise LoanAccessDenied('This tool requires access to every branch in its fixed NiFi scope. Ask your administrator for a narrower endpoint or the appropriate branch/zone grant.')
        for params in (arguments, backend_arguments or {}):
            branch = params.get('branch_code', params.get('branchCode'))
            zone = params.get('zone_code', params.get('zoneCode'))
            zone_name = params.get('zoneName')
            if branch and branch not in fixed:
                raise LoanAccessDenied('The requested branch is outside this tool’s fixed NiFi scope.')
            if zone and not any(directory[code]['zone__code'] == zone for code in fixed):
                raise LoanAccessDenied('The requested zone is outside this tool’s fixed NiFi scope.')
            if zone_name and not any(directory[code]['zone__name'] == zone_name for code in fixed):
                raise LoanAccessDenied('The requested zone is outside this tool’s fixed NiFi scope.')
        return FixedLoanGrant({'subject': access['subject'], 'version': access['version'],
                               'mode': 'FIXED', 'tool': name, 'branches': fixed}, fixed)
    # The backend must explicitly declare the signed-token contract. Never
    # silently send an unrestricted legacy request when integration is missing.
    schema = getattr(tool, 'inputSchema', {})
    if (schema.get('properties', {}).get(TOKEN_FIELD, {}).get('type') != 'string'
            or TOKEN_FIELD not in schema.get('required', [])):
        raise LoanAccessUnavailable(f'User access is assigned, but the MCP tool {name} does not declare the required string loan_access_token. An administrator must complete the ContextForge and NiFi signed-access setup. No report was executed.')
    props = schema.get('properties', {})
    if ('branchCode' in props and 'reportDate' in props and 'report_date' not in props
            and arguments.get('operation') != 'rank_overdue_members'
            and (not arguments.get('branchCode') or not arguments.get('reportDate'))):
        raise LoanAccessDenied('Provide an explicit branch code and report date for this legacy report.')
    requested_branch = arguments.get('branch_code', arguments.get('branchCode'))
    zone_code = arguments.get('zone_code', arguments.get('zoneCode'))
    zone_name = arguments.get('zoneName')
    if zone_name:
        zones = list(LoanZone.objects.filter(name=zone_name, is_active=True).values_list('code', flat=True)[:2])
        if len(zones) != 1:
            raise LoanAccessDenied('Provide an unambiguous authorized zone code.')
        if zone_code and zone_code != zones[0]:
            raise LoanAccessDenied('The requested zone identifiers disagree.')
        zone_code = zones[0]
    rows = access['directory']
    if zone_code:
        rows = [row for row in rows if row['zone__code'] == zone_code]
    if requested_branch:
        rows = [row for row in rows if row['code'] == requested_branch]
    if not rows:
        raise LoanAccessDenied('The requested branch or zone is outside your assigned loan data access.')
    key = os.environ.get('LOAN_ACCESS_SIGNING_KEY', '')
    if len(key.encode()) < 32:
        raise LoanAccessUnavailable('Loan reporting setup is incomplete. Contact your administrator.')
    query = backend_arguments if backend_arguments is not None else arguments
    try:
        canonical(query)
    except (ValueError, TypeError) as exc:
        raise LoanAccessDenied('The structured loan query contains invalid values.') from exc
    now = int(time.time())
    expiry = min(now + 120, int(access['expires_at'].timestamp())) if access['expires_at'] else now + 120
    if expiry <= now + 1:
        raise LoanAccessDenied('Your loan access expires too soon to start a new query.')
    claims = {'v': 1, 'iss': ISSUER, 'aud': AUDIENCE, 'sub': access['subject'],
              'iat': now, 'nbf': now, 'exp': expiry, 'jti': uuid.uuid4().hex,
              'tool': name, 'branches': sorted({row['code'] for row in rows}),
              'query': query, 'scope_version': access['version']}
    token = jwt.encode(claims, key, algorithm='HS256')
    return LoanGrant(token, authorization_receipt(claims),
                     {'subject': access['subject'], 'version': access['version']})


def validate_response(raw, grant):
    if isinstance(grant, FixedLoanGrant):
        # No signed receipt is expected; NiFi must enforce the full configured
        # scope before SQL aggregation. Explicit returned branches are checked.
        def check(value):
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except (ValueError, TypeError):
                    return
                if isinstance(decoded, (dict, list)):
                    check(decoded)
            elif isinstance(value, dict):
                for key, item in value.items():
                    if key in {'BRANCHCODE', 'branchCode', 'branch_code'} and item is not None and item not in grant.branches:
                        raise LoanAccessDenied('The backend returned a branch outside the fixed NiFi scope.')
                    if key == 'authorized_branch_count' and (type(item) is not int or not 0 <= item <= len(grant.branches)):
                        raise LoanAccessDenied('The backend scope exceeds the fixed NiFi scope.')
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
        check(raw)
        return
    if grant.token in json.dumps(raw):
        raise LoanAccessDenied('The backend echoed authorization credentials.')
    # Locate an envelope, never print or persist the signed token or raw data.
    data = raw.get('structuredContent')
    if data is None:
        content = raw.get('content', [])
        if len(content) == 1 and content[0].get('type') == 'text':
            data = content[0].get('text')
    for _ in range(8):
        if isinstance(data, str):
            data = json.loads(data)
        elif isinstance(data, dict):
            if 'authorization' in data:
                if canonical(data['authorization']) != canonical(grant.receipt):
                    raise LoanAccessDenied('The backend did not confirm the authorized scope for this request.')
                break
            if 'structuredContent' in data:
                data = data['structuredContent']
            elif isinstance(data.get('content'), list) and len(data['content']) == 1:
                data = data['content'][0].get('text')
            else:
                data = data.get('data', data.get('result'))
        else:
            raise LoanAccessDenied('The loan response has no authorization receipt.')
    else:
        raise LoanAccessDenied('The loan response has no authorization receipt.')
    if not isinstance(data, dict) or canonical(data.get('authorization')) != canonical(grant.receipt):
        raise LoanAccessDenied('The loan response has no authorization receipt.')
    allowed = set(grant.receipt['branch_codes'])
    count = data.get('scope', {}).get('authorized_branch_count') if isinstance(data.get('scope'), dict) else None
    if count is not None and (type(count) is not int or not 0 <= count <= len(allowed)):
        raise LoanAccessDenied('The backend branch count exceeds the signed authorization.')
    # Grouped cross-branch results rely on the backend constraining source rows
    # before aggregation; any explicit row branch is additionally checked here.
    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'BRANCHCODE', 'branchCode', 'branch_code'} and item is not None and item not in allowed:
                    raise LoanAccessDenied('The backend returned data outside the authorized branches.')
                if key != 'authorization':
                    check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(data)
