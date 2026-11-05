"""Idempotent employee provisioning. Never reset passwords or existing groups."""
import json
from urllib.parse import urlsplit

import requests
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from .models import EmployeeDirectoryEntry, EmployeeSyncSettings, UserProfile

INITIAL_PASSWORD = 'BBBB1234$$#'
FIELDS = ('EMPLOYEEID', 'EMPLOYEECODE', 'EMPLOYEENAME', 'BRANCH', 'EMPSTATUS')


def normalize(payload):
    rows = payload.get('rows', payload.get('data')) if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows or len(rows) > 100000:
        raise ValueError('Expected a nonempty JSON array (or rows/data array), up to 100000 employees.')
    result, ids, codes = [], set(), set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or any(key not in row for key in FIELDS):
            raise ValueError(f'Row {index}: all five employee columns are required.')
        values = {}
        for key, limit in zip(FIELDS, (128, 150, 255, 32, 64)):
            value = row[key]
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError(f'Row {index}: {key} must be a string or integer.')
            value = str(value).strip()
            if (not value and key != 'BRANCH') or len(value) > limit or any(ord(c) < 32 for c in value):
                raise ValueError(f'Row {index}: invalid {key}.')
            values[key] = value
        if values['EMPLOYEEID'] in ids or values['EMPLOYEECODE'] in codes:
            raise ValueError('Duplicate employee ID or code. No changes applied.')
        ids.add(values['EMPLOYEEID'])
        codes.add(values['EMPLOYEECODE'])
        result.append(values)
    return result


def fetch(config):
    config.full_clean()
    if not config.url or urlsplit(config.url).scheme not in ('http', 'https'):
        raise ValueError('Configure the employee API URL first.')
    try:
        with requests.Session() as client:
            client.trust_env = False
            with client.get(config.url, headers={'Accept': 'application/json'}, timeout=(5, 60), stream=True, allow_redirects=False) as response:
                if response.status_code != 200 or response.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                    raise ValueError('Employee API must return HTTP 200 with application/json.')
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 32_000_000:
                        raise ValueError('Employee response exceeds 32 MB.')
                return normalize(json.loads(body.decode('utf-8')))
    except (requests.RequestException, UnicodeError, json.JSONDecodeError):
        raise ValueError('Unable to fetch employee JSON. Check the URL, connectivity and TLS.') from None


def sync_employees():
    config = EmployeeSyncSettings.objects.get(pk=1)
    rows = fetch(config)
    if not config.default_group_id:
        raise ValueError('Choose the default employee group first.')
    active = {v.strip().casefold() for v in config.active_statuses.split(',') if v.strip()}
    created = updated = protected = 0
    User = get_user_model()
    with transaction.atomic():
        # Serialize sync runs and protect against settings changing during fetch.
        locked = EmployeeSyncSettings.objects.select_for_update().get(pk=1)
        if any(getattr(locked, field) != getattr(config, field) for field in ('url', 'default_group_id', 'active_statuses')):
            raise ValueError('Settings changed during fetch. Run sync again.')
        for row in rows:
            entry = EmployeeDirectoryEntry.objects.select_for_update().filter(employee_id=row['EMPLOYEEID']).first()
            user = entry.user if entry else User.objects.select_for_update().filter(username=row['EMPLOYEECODE']).first()
            if user and (user.is_superuser or user.is_staff or user.username == 'root_123'):
                protected += 1
                continue
            if not entry and user and EmployeeDirectoryEntry.objects.filter(user=user).exists():
                raise ValueError('Employee code belongs to a different employee ID. No changes applied.')
            if entry and User.objects.filter(username=row['EMPLOYEECODE']).exclude(pk=user.pk).exists():
                raise ValueError('Employee code conflicts with an existing account. No changes applied.')
            is_new = user is None
            user = user or User()
            user.username = row['EMPLOYEECODE']
            name = row['EMPLOYEENAME'].split(' ', 1)
            user.first_name, user.last_name = name[0][:150], name[1][:150] if len(name) > 1 else ''
            user.is_active = row['EMPSTATUS'].casefold() in active
            if is_new:
                user.set_password(INITIAL_PASSWORD)
            user.save()
            if is_new:
                user.groups.add(config.default_group)
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.branch_code = row['BRANCH']
            profile.save(update_fields=['branch_code'])
            entry = entry or EmployeeDirectoryEntry(user=user)
            entry.employee_id, entry.employee_code = row['EMPLOYEEID'], row['EMPLOYEECODE']
            entry.employee_name, entry.branch, entry.status = row['EMPLOYEENAME'], row['BRANCH'], row['EMPSTATUS']
            entry.synced_at = timezone.now()
            entry.full_clean()
            entry.save()
            created += int(is_new)
            updated += int(not is_new)
        result = f'Created {created}; updated {updated}; protected admin accounts {protected}.'
        locked.last_sync_at, locked.last_result = timezone.now(), result
        locked.save(update_fields=['last_sync_at', 'last_result'])
    return result
