"""Idempotent employee provisioning. Never reset passwords or existing groups."""
import json
import hashlib
import fcntl
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import requests
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.db import transaction, connection, OperationalError, IntegrityError
from django.core.exceptions import ValidationError
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
        for key, limit in zip(FIELDS, (128, 150, 255, 255, 64)):
            value = row[key]
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError(f'Row {index}: {key} must be a string or integer.')
            value = str(value).strip()
            if (not value and key != 'BRANCH') or len(value) > limit or any(ord(c) < 32 for c in value):
                raise ValueError(f'Row {index}: invalid {key}.')
            values[key] = value
        for key, limit in (('SITECODE', 32), ('DESIGNATIONNAME', 255), ('GENDER', 64), ('EMP_WORKSPACE', 64)):
            value = row.get(key, '')
            if value is None:
                value = ''
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError(f'Row {index}: {key} must be a string or integer.')
            value = str(value).strip()
            if len(value) > limit or any(ord(c) < 32 for c in value):
                raise ValueError(f'Row {index}: invalid {key}.')
            values[key] = value
        # Legacy five-column feeds used BRANCH as the code. Never truncate it.
        if 'SITECODE' not in row and len(values['BRANCH']) > 32:
            raise ValueError(f'Row {index}: provide SITECODE when BRANCH contains a location name.')
        values['_legacy_branch_code'] = 'SITECODE' not in row
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


class SyncBusy(ValueError):
    pass


@contextmanager
def sync_lock():
    # Both on-prem containers share the SQLite directory. OS locks survive
    # neither process exit nor pod termination, so crashes cannot leave a lease.
    database = str(connection.settings_dict['NAME'])
    if connection.vendor == 'sqlite' and database != ':memory:' and not database.startswith('file:memory'):
        path = Path(database).resolve().parent / '.employee-sync.lock'
    else:
        path = Path(tempfile.gettempdir()) / ('employee-sync-' + hashlib.sha256(database.encode()).hexdigest()[:16] + '.lock')
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncBusy('Employee sync is already running. Check Last result for progress.') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def preflight(rows):
    """Reject known identity conflicts before committing any employees."""
    users = dict(get_user_model().objects.values_list('username', 'pk'))
    entries = {eid: uid for eid, uid in EmployeeDirectoryEntry.objects.values_list('employee_id', 'user_id')}
    linked_users = set(entries.values())
    for row in rows:
        uid = entries.get(row['EMPLOYEEID'])
        target = users.get(row['EMPLOYEECODE'])
        if (uid and target and uid != target) or (not uid and target in linked_users):
            raise ValueError('Employee identity conflicts with an existing account. No employees were changed.')
    return users


def _sync_row(row, config, active, password_hash):
    User = get_user_model()
    with transaction.atomic():
        entry = EmployeeDirectoryEntry.objects.select_for_update().filter(employee_id=row['EMPLOYEEID']).first()
        user = entry.user if entry else User.objects.select_for_update().filter(username=row['EMPLOYEECODE']).first()
        if user and (user.is_superuser or user.is_staff or user.username == 'root_123'):
            return 'protected'
        if not entry and user and EmployeeDirectoryEntry.objects.filter(user=user).exists():
            raise ValueError('Employee code belongs to a different employee ID. Earlier employees may have synced; correct the conflict and retry.')
        if entry and User.objects.filter(username=row['EMPLOYEECODE']).exclude(pk=user.pk).exists():
            raise ValueError('Employee code conflicts with an existing account. Earlier employees may have synced; correct the conflict and retry.')
        is_new = user is None
        user = user or User()
        user.username = row['EMPLOYEECODE']
        name = row['EMPLOYEENAME'].split(' ', 1)
        user.first_name, user.last_name = name[0][:150], name[1][:150] if len(name) > 1 else ''
        user.is_active = row['EMPSTATUS'].casefold() in active
        if is_new:
            if password_hash is None:
                raise ValueError('Accounts changed during sync. Retry to safely create the employee.')
            user.password = password_hash
        user.save()
        if is_new:
            user.groups.add(config.default_group)
        profile, _ = UserProfile.objects.get_or_create(user=user)
        # Zone/area/head-office location codes are not branch access grants.
        if row['_legacy_branch_code']:
            profile.branch_code = row['BRANCH']
        else:
            profile.branch_code = row['SITECODE'] if row['EMP_WORKSPACE'].casefold() == 'branch' else ''
        profile.save(update_fields=['branch_code'])
        entry = entry or EmployeeDirectoryEntry(user=user)
        entry.employee_id, entry.employee_code = row['EMPLOYEEID'], row['EMPLOYEECODE']
        entry.employee_name, entry.branch, entry.status = row['EMPLOYEENAME'], row['BRANCH'], row['EMPSTATUS']
        entry.site_code = row['SITECODE']
        entry.designation, entry.gender = row['DESIGNATIONNAME'], row['GENDER']
        entry.workspace = row['EMP_WORKSPACE']
        entry.synced_at = timezone.now()
        entry.full_clean()
        entry.save()
        return 'created' if is_new else 'updated'


def sync_employees(progress=None):
    with sync_lock():
        try:
            return _sync_employees(progress)
        except (ValueError, ValidationError, OperationalError, IntegrityError) as exc:
            # Progress is committed independently; never promise whole-feed rollback.
            try:
                EmployeeSyncSettings.objects.filter(pk=1).update(
                    last_result='Sync failed; earlier completed employees remain saved. ' + str(exc)[:500])
            except OperationalError:
                pass
            raise


def _sync_employees(progress):
    config = EmployeeSyncSettings.objects.get(pk=1)
    EmployeeSyncSettings.objects.filter(pk=1).update(sync_requested=False, last_result='Running: fetching employee API...')
    rows = fetch(config)
    if not config.default_group_id:
        raise ValueError('Choose the default employee group first.')
    current = EmployeeSyncSettings.objects.get(pk=1)
    if any(getattr(current, field) != getattr(config, field) for field in ('url', 'default_group_id', 'active_statuses')):
        raise ValueError('Settings changed during fetch. Run sync again.')
    known_users = preflight(rows)
    active = {v.strip().casefold() for v in config.active_statuses.split(',') if v.strip()}
    counts = dict(created=0, updated=0, protected=0)
    total = len(rows)
    for index, row in enumerate(rows, 1):
        # Password hashing is expensive: do it before acquiring SQLite's write lock.
        password_hash = make_password(INITIAL_PASSWORD) if row['EMPLOYEECODE'] not in known_users else None
        outcome = _sync_row(row, config, active, password_hash)
        counts[outcome] += 1
        if index == 1 or index % 25 == 0 or index == total:
            result = f"Running: {index}/{total}; created {counts['created']}; updated {counts['updated']}; protected {counts['protected']}."
            EmployeeSyncSettings.objects.filter(pk=1).update(last_result=result)
            if progress:
                progress(result)
    result = f"Created {counts['created']}; updated {counts['updated']}; protected admin accounts {counts['protected']}. Processed {total}/{total}."
    EmployeeSyncSettings.objects.filter(pk=1).update(last_sync_at=timezone.now(), last_result=result)
    return result
