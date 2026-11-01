"""Admin-managed directory connection with encrypted credential storage."""
import base64
import hashlib
import os
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction
from .models import LoanDirectoryConnection

DEFAULT_URL = 'http://192.168.1.139:8005/api/reference/branches'


def _cipher(secret):
    key = hashlib.sha256(('buro-directory-token-v1:' + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def decrypt(value):
    for secret in [settings.SECRET_KEY, *getattr(settings, 'SECRET_KEY_FALLBACKS', [])]:
        try:
            return _cipher(secret).decrypt(value.encode()).decode()
        except (InvalidToken, UnicodeError):
            continue
    raise ValueError('The saved directory credential cannot be decrypted. Re-enter the NiFi token in Integration settings.')


def current():
    connection = LoanDirectoryConnection.objects.filter(pk=1).first()
    if connection:
        return {'url': connection.url, 'allow_http': connection.allow_http,
                'token': decrypt(connection.encrypted_token), 'version': connection.updated_at.isoformat(), 'source': 'admin'}
    return {'url': os.environ.get('LOAN_DIRECTORY_URL', '').strip(),
            'allow_http': os.environ.get('LOAN_DIRECTORY_ALLOW_HTTP') == '1',
            'token': os.environ.get('LOAN_DIRECTORY_TOKEN', ''), 'version': 'new', 'source': 'environment'}


def public_status():
    connection = LoanDirectoryConnection.objects.filter(pk=1).first()
    # Render configuration metadata only; do not decrypt or include the token.
    return {'url': connection.url if connection else os.environ.get('LOAN_DIRECTORY_URL', '').strip(),
            'allow_http': connection.allow_http if connection else os.environ.get('LOAN_DIRECTORY_ALLOW_HTTP') == '1',
            'token_saved': bool(connection.encrypted_token) if connection else bool(os.environ.get('LOAN_DIRECTORY_TOKEN')),
            'version': connection.updated_at.isoformat() if connection else 'new',
            'source': 'admin' if connection else 'environment'}


def save(actor, url, allow_http, token, version):
    from .directory_sync import validate_configuration
    from .access_admin import record_event
    if not isinstance(url, str) or not isinstance(token, str) or type(allow_http) is not bool or len(url) > 2048 or len(token) > 4096:
        raise ValueError('Provide a valid directory URL, HTTP choice and API token.')
    url, token = url.strip(), token.strip()
    with transaction.atomic():
        connection = LoanDirectoryConnection.objects.select_for_update().filter(pk=1).first()
        actual = connection.updated_at.isoformat() if connection else 'new'
        if version != actual:
            raise ValueError('Directory settings changed in another session. Refresh before saving.')
        old = public_status()
        old_url = old['url']
        if not token:
            if old_url and (urlsplit(old_url).scheme, urlsplit(old_url).netloc) != (urlsplit(url).scheme, urlsplit(url).netloc):
                raise ValueError('Re-enter the NiFi token when changing the API origin or HTTP/HTTPS scheme.')
            token = decrypt(connection.encrypted_token) if connection else os.environ.get('LOAN_DIRECTORY_TOKEN', '')
        validate_configuration(url, token, allow_http)
        connection = connection or LoanDirectoryConnection(pk=1)
        connection.url, connection.allow_http = url, allow_http
        connection.encrypted_token = _cipher(settings.SECRET_KEY).encrypt(token.encode()).decode()
        connection.full_clean()
        connection.save()
        record_event(actor, None, 'directory_configured', 'Directory API configuration updated in Integration settings.',
                     {key: old[key] for key in ('url', 'allow_http', 'token_saved', 'source')},
                     {'url': url, 'allow_http': allow_http, 'token_saved': True, 'source': 'admin'})
    return connection.updated_at.isoformat()
