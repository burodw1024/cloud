import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .dynamic_config import get_setting


SERVICE_LINKS = {
    'PORTAL_CONTEXTFORGE_URL': 'ContextForge',
    'PORTAL_FORGEJO_URL': 'Forgejo',
    'PORTAL_GITEA_URL': 'Gitea',
    'PORTAL_REGISTRY_URL': 'Docker registry',
}


def validate_service_url(value):
    if not value:
        return
    if any(char.isspace() or ord(char) < 32 for char in value) or '\\' in value:
        raise ValidationError('Enter a valid HTTP or HTTPS URL.')
    validator = URLValidator(schemes=['http', 'https'])
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ValidationError('Enter a valid HTTP or HTTPS URL.') from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError('Enter a URL without embedded credentials.')
    # Internal services may use a single DNS label, such as gitea:3000.
    if host and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', host):
        authority = host + '.internal' + (f':{port}' if port is not None else '')
        if parsed.netloc.lower() == host + (f':{port}' if port is not None else ''):
            validator(parsed._replace(netloc=authority).geturl())
            return
    validator(value)


def service_links():
    for key, label in SERVICE_LINKS.items():
        value = get_setting(key)
        if value:
            try:
                validate_service_url(value)
            except ValidationError:
                continue
            yield label, value


@never_cache
@require_safe
def home(request):
    return render(request, 'chat/portal.html', {
        'service_links': list(service_links()) if request.user.is_authenticated else [],
    })


@never_cache
@require_safe
def documentation(request):
    return render(request, 'chat/portal_docs.html')
