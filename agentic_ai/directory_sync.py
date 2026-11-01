"""Authenticated, bounded directory snapshots; no financial data or access grants."""
import hashlib
import ipaddress
import json
import os
import re
from urllib.parse import urlsplit

import requests
from django.core import signing
from django.db import transaction
from .models import LoanZone, LoanBranch

MAX_BYTES = 4_000_000
CODE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z')
FIELDS = tuple(p + s for p in ('DIVISION', 'ZONE', 'AREA', 'BRANCH') for s in ('CODE', 'NAME'))


def configuration():
    url = os.environ.get('LOAN_DIRECTORY_URL', '').strip()
    token = os.environ.get('LOAN_DIRECTORY_TOKEN', '')
    parsed = urlsplit(url)
    valid = parsed.scheme == 'https'
    if parsed.scheme == 'http' and os.environ.get('LOAN_DIRECTORY_ALLOW_HTTP') == '1':
        try:
            address = ipaddress.ip_address(parsed.hostname or '')
            valid = any(address in ipaddress.ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
        except ValueError:
            valid = False
    if not valid or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or len(token.encode()) < 32:
        raise ValueError('Configure LOAN_DIRECTORY_URL and a dedicated LOAN_DIRECTORY_TOKEN (at least 32 bytes). Use HTTPS; private HTTP requires an explicit deployment opt-in.')
    return url, token


def normalize(payload):
    if not isinstance(payload, dict) or payload.get('success') is not True or payload.get('complete') is not True or type(payload.get('version')) is not int or payload['version'] != 1 or payload.get('source') != 'DW_GC_BRANCHLIST2':
        raise ValueError('The directory API did not return a complete version 1 snapshot.')
    rows = payload.get('rows')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000 or type(payload.get('row_count')) is not int or payload['row_count'] != len(rows):
        raise ValueError('Invalid directory row count. No changes were applied.')
    branches, zones, areas, divisions = {}, {}, {}, {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise ValueError(f'Row {index}: expected all eight directory columns.')
        row = {key: ('' if value is None else value.strip()) if isinstance(value, (str, type(None))) else value for key, value in row.items()}
        for prefix in ('DIVISION', 'ZONE', 'AREA', 'BRANCH'):
            code, name = row[prefix + 'CODE'], row[prefix + 'NAME']
            optional = prefix in {'DIVISION', 'AREA'} and code == name == ''
            if not optional and (not isinstance(code, str) or not CODE.fullmatch(code) or not isinstance(name, str) or not name or len(name) > 150 or any(ord(c) < 32 for c in name)):
                raise ValueError(f'Row {index}: invalid {prefix.lower()} code/name. Codes must be exact strings of up to 32 characters; nothing is truncated.')
        for mapping, key, value in (
            (branches, row['BRANCHCODE'], row),
            (zones, row['ZONECODE'], (row['ZONENAME'], row['DIVISIONCODE'], row['DIVISIONNAME'])),
            (areas, row['AREACODE'], (row['AREANAME'], row['ZONECODE'])),
            (divisions, row['DIVISIONCODE'], row['DIVISIONNAME']),
        ):
            if key and key in mapping and mapping[key] != value:
                raise ValueError(f'Row {index}: conflicting directory codes or hierarchy.')
            if key:
                mapping[key] = value
    # DISTINCT in Oracle should already remove exact duplicates. Reject duplicates
    # here so counts cannot disguise an incomplete or malformed snapshot.
    if len(branches) != len(rows):
        raise ValueError('Duplicate branch codes in directory snapshot.')
    return sorted(branches.values(), key=lambda row: row['BRANCHCODE'])


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON fields in directory response.')
        result[key] = value
    return result


def fetch_snapshot():
    url, token = configuration()
    try:
        with requests.Session() as client:
            # Never send the private directory token through an ambient proxy.
            client.trust_env = False
            with client.get(url, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, timeout=(5, 30), stream=True, allow_redirects=False, verify=os.environ.get('LOAN_DIRECTORY_CA_BUNDLE') or True) as response:
                if response.status_code != 200 or response.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                    raise ValueError('Directory API rejected the request or returned an invalid content type.')
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise ValueError('Directory response exceeded 4 MB. No changes were applied.')
                payload = json.loads(body.decode('utf-8'), object_pairs_hook=_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON number.')))
    except (requests.RequestException, UnicodeError, json.JSONDecodeError):
        raise ValueError('Unable to retrieve the directory. Check NiFi connectivity, TLS and the dedicated API credential.') from None
    return normalize(payload)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def catalog():
    return {'zones': list(LoanZone.objects.order_by('code').values()), 'branches': list(LoanBranch.objects.order_by('code').values())}


def preview(actor, activate_new=False):
    rows = fetch_snapshot()
    state = catalog()
    old = {r['code']: r for r in state['branches']}
    changes = [r for r in rows if r['BRANCHCODE'] not in old or old[r['BRANCHCODE']]['name'] != r['BRANCHNAME'] or old[r['BRANCHCODE']]['zone_id'] != next((z['id'] for z in state['zones'] if z['code'] == r['ZONECODE']), None)]
    ticket = signing.dumps({'actor': actor.pk, 'source': digest(rows), 'catalog': digest(state), 'activate': bool(activate_new)}, salt='loan-directory-preview', compress=True)
    return {'ticket': ticket, 'count': len(rows), 'new_count': sum(r['BRANCHCODE'] not in old for r in rows), 'zone_count': len({r['ZONECODE'] for r in rows}), 'sample': changes[:20], 'activate': bool(activate_new)}


def apply_preview(actor, ticket, reason):
    from .access_admin import directory_state, record_event
    if not 8 <= len(reason.strip()) <= 500:
        raise ValueError('Provide a business justification of 8–500 characters.')
    try:
        expected = signing.loads(ticket, salt='loan-directory-preview', max_age=600)
    except signing.BadSignature:
        raise ValueError('Preview expired or invalid. Fetch a new preview.') from None
    if expected['actor'] != actor.pk:
        raise ValueError('Fetch a preview using your own administrator account.')
    rows = fetch_snapshot()
    if digest(rows) != expected['source']:
        raise ValueError('Oracle directory changed. Fetch a new preview before applying.')
    with transaction.atomic():
        # Lock existing catalog rows on databases supporting row locks. SQLite
        # serializes writes; a racing update raises and rolls back the transaction.
        list(LoanZone.objects.select_for_update().order_by('pk'))
        list(LoanBranch.objects.select_for_update().order_by('pk'))
        if digest(catalog()) != expected['catalog']:
            raise ValueError('Local directory changed. Fetch a new preview before applying.')
        for row in rows:
            zone = LoanZone.objects.filter(code=row['ZONECODE']).first()
            before = directory_state(zone) if zone else {}
            zone = zone or LoanZone(code=row['ZONECODE'], is_active=expected['activate'])
            zone.name, zone.division_code, zone.division_name = row['ZONENAME'], row['DIVISIONCODE'], row['DIVISIONNAME']
            zone.full_clean()
            if directory_state(zone) != before:
                zone.save()
                record_event(actor, None, 'zone_synced', reason, before, directory_state(zone))
            branch = LoanBranch.objects.filter(code=row['BRANCHCODE']).first()
            before = directory_state(branch) if branch else {}
            branch = branch or LoanBranch(code=row['BRANCHCODE'], is_active=expected['activate'])
            branch.name, branch.zone = row['BRANCHNAME'], zone
            branch.area_code, branch.area_name = row['AREACODE'], row['AREANAME']
            branch.full_clean()
            if directory_state(branch) != before:
                branch.save()
                record_event(actor, None, 'branch_synced', reason, before, directory_state(branch))
        record_event(actor, None, 'directory_synced', reason, {}, {'source': 'DW_GC_BRANCHLIST2', 'sha256': expected['source'], 'rows': len(rows), 'activate_new': expected['activate']})
    return len(rows)
