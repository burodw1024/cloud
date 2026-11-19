"""Admin-owned reporting configuration; secret fields are encrypted at rest."""
import base64
import hashlib
import json
import re
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from django import forms
from django.conf import settings
from django.db import transaction
from .models import ReportingConnection

from .report_catalog import DESIGNS, TEMPLATES, satellite_tiles, valid_tile_url


def _cipher(secret):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(('buro-reporting-v1:' + secret).encode()).digest()))


def credentials(record):
    if not record or not record.encrypted_credentials:
        return {}
    for secret in [settings.SECRET_KEY, *getattr(settings, 'SECRET_KEY_FALLBACKS', [])]:
        try:
            return json.loads(_cipher(secret).decrypt(record.encrypted_credentials.encode()))
        except (InvalidToken, ValueError):
            pass
    raise ValueError('Re-enter reporting passwords; their encryption key is unavailable.')


def current():
    record = ReportingConnection.objects.filter(pk=1).first()
    if not record:
        return dict(getattr(settings, 'AGENTIC_SUPERSET', {}))
    config = dict(record.configuration)
    secret = credentials(record)
    config['PASSWORD'] = secret.get('password', '')
    if config.get('SNAPSHOT_DB'):
        config['SNAPSHOT_DB'] = {**config['SNAPSHOT_DB'], 'password': secret.get('database_password', '')}
    return config


def report_server_url(value, allow_http=False):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.port == 0
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or '\\' in value or any(ord(char) < 33 or ord(char) == 127 for char in value)):
            raise ValueError
    except ValueError:
        raise forms.ValidationError('Enter a Report Server web portal URL without credentials, a query string or a fragment.') from None
    if parsed.scheme == 'http' and not allow_http:
        raise forms.ValidationError('Use HTTPS or explicitly allow HTTP for the trusted internal Report Server.')
    return value.rstrip('/')


def workspace_settings(user):
    record = ReportingConnection.objects.filter(pk=1).first()
    config = record.configuration if record else getattr(settings, 'AGENTIC_SUPERSET', {})
    try:
        public_url = origin(config.get('PUBLIC_URL') or config.get('URL',''), config.get('ALLOW_HTTP',False)) if config.get('URL') else ''
    except (forms.ValidationError,ValueError):
        public_url = ''
    default = config.get('DEFAULT_TEMPLATE', 'comparison')
    powerbi_url = ''
    if config.get('POWERBI_PORTAL_URL'):
        try:
            powerbi_url = report_server_url(config['POWERBI_PORTAL_URL'], config.get('POWERBI_ALLOW_HTTP', False))
        except forms.ValidationError:
            pass
    can_export_powerbi = (config.get('POWERBI_MODE') == 'manual' and user.is_active
                          and user.has_perm('agentic_ai.export_powerbi_snapshot'))
    return {'configured': bool(public_url), 'url': public_url,
            'default_template': default if default in DESIGNS else 'comparison',
            'can_publish': user.is_staff and user.has_perm('agentic_ai.publish_dashboard'),
            'templates': TEMPLATES, 'map_imagery_configured': bool(satellite_tiles(config)),
            'powerbi': {'enabled': can_export_powerbi, 'portal_url': powerbi_url if can_export_powerbi else ''}}


def origin(value, allow_http):
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
        raise forms.ValidationError('Enter a service origin such as https://superset.example.com, without a path or password.')
    if parsed.scheme == 'http' and not allow_http:
        raise forms.ValidationError('Use HTTPS, or explicitly allow HTTP for your trusted internal service.')
    return value.rstrip('/')


class ReportingForm(forms.Form):
    service_url = forms.URLField(required=False, label='Superset API address', help_text='The address reachable from the Django server. Leave blank to keep only local report templates.')
    public_url = forms.URLField(required=False, label='Superset browser address', help_text='The address employees can open. Blank uses the API address.')
    allow_http = forms.BooleanField(required=False, label='Allow HTTP for this trusted internal Superset service')
    username = forms.CharField(required=False, max_length=150, label='Superset publishing service username')
    password = forms.CharField(required=False, max_length=4096, widget=forms.PasswordInput(render_value=False), label='Service password', help_text='Leave blank to retain the saved password.')
    database_id = forms.IntegerField(required=False, min_value=1, label='Superset analytics database ID', help_text='Use the ID of the dedicated analytics database already registered in Superset.')
    database_host = forms.CharField(required=False, max_length=253, label='Analytics PostgreSQL host')
    database_port = forms.IntegerField(initial=5432, min_value=1, max_value=65535, label='PostgreSQL port')
    database_name = forms.CharField(required=False, max_length=128, label='Analytics database name')
    database_user = forms.CharField(required=False, max_length=128, label='Snapshot writer username')
    database_password = forms.CharField(required=False, max_length=4096, widget=forms.PasswordInput(render_value=False), label='Snapshot writer password', help_text='Leave blank to retain. Superset must read the same database/schema with its own account.')
    database_sslmode = forms.ChoiceField(initial='verify-full', choices=[('verify-full', 'Verify TLS certificate and hostname'), ('require', 'Require encrypted PostgreSQL connection')], label='PostgreSQL TLS')
    schema = forms.RegexField(r'^[a-z_][a-z0-9_]{0,62}$', initial='agentic', label='Snapshot schema')
    user_mappings = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows':4}), label='Employee → Superset user ID', help_text='One per line: employee_code=Superset_user_ID. Each employee needs a different Superset account.')
    private_access_verified = forms.BooleanField(required=False, label='I verified that unrelated Superset users cannot access these report snapshots')
    default_template = forms.ChoiceField(choices=TEMPLATES, initial='comparison', label='Default report template')
    map_background = forms.ChoiceField(required=False, initial='deployment', label='Map background', choices=[
        ('deployment', 'Use deployment settings (grid if unset)'),
        ('grid', 'Offline coordinate grid'),
        ('custom', 'Approved satellite imagery'),
    ])
    map_tile_url = forms.CharField(required=False, max_length=1500, label='Satellite XYZ tile URL',
        help_text='Use a browser-accessible HTTPS URL or a Superset-relative path such as /tiles/satellite/{z}/{x}/{y}.jpg. '
                  'For paid or authenticated imagery, use an approved internal proxy that holds the provider credentials.')
    map_tile_attribution = forms.CharField(required=False, max_length=300, label='Imagery attribution',
        help_text='Enter the provider’s required attribution as plain text.')
    powerbi_mode = forms.ChoiceField(required=False, initial='disabled', label='Power BI handoff', choices=[
        ('disabled', 'Disabled'), ('manual', 'Enable authorized workbook downloads'),
    ], help_text='Grant the Power BI snapshot export permission to the approved users or groups.')
    powerbi_portal_url = forms.URLField(required=False, max_length=500, label='Power BI Report Server portal URL',
        help_text='Optional until the server is installed. Enter the browser portal, for example https://powerbi.internal/reports.')
    powerbi_allow_http = forms.BooleanField(required=False, label='Allow HTTP for this trusted internal Power BI Report Server')
    version = forms.CharField(widget=forms.HiddenInput)

    def clean(self):
        data = super().clean()
        url = data.get('service_url')
        if url:
            data['service_url'] = origin(url, data.get('allow_http'))
        if data.get('public_url'):
            data['public_url'] = origin(data['public_url'], data.get('allow_http'))
        if data.get('map_background') == 'custom':
            tile_url = data.get('map_tile_url', '')
            if not valid_tile_url(tile_url):
                self.add_error('map_tile_url', 'Enter an HTTP(S) or same-origin XYZ URL containing {z}, {x} and {y}, without credentials, query strings or fragments.')
            elif tile_url.startswith('http:') and (data.get('public_url') or data.get('service_url') or '').startswith('https:'):
                self.add_error('map_tile_url', 'Use HTTPS or a same-origin path when Superset is served over HTTPS.')
            if not data.get('map_tile_attribution'):
                self.add_error('map_tile_attribution', 'Provide the attribution required by the imagery provider.')
        if data.get('powerbi_portal_url'):
            try:
                data['powerbi_portal_url'] = report_server_url(data['powerbi_portal_url'], data.get('powerbi_allow_http'))
            except forms.ValidationError as exc:
                self.add_error('powerbi_portal_url', exc)
        mappings = {}
        for line in data.get('user_mappings', '').splitlines():
            if not line.strip(): continue
            match = re.fullmatch(r'([^=\s]{1,150})\s*=\s*([1-9][0-9]{0,9})', line.strip())
            if not match or match[1] in mappings:
                raise forms.ValidationError('Use one unique employee_code=Superset_user_ID entry per line.')
            mappings[match[1]] = int(match[2])
        if len(set(mappings.values())) != len(mappings):
            raise forms.ValidationError('Each employee needs a different Superset user ID.')
        data['mappings'] = mappings
        host = data.get('database_host', '')
        if host and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,253}', host):
            self.add_error('database_host', 'Enter a hostname or IP address, without a URL or connection parameters.')
        return data


def initial():
    record = ReportingConnection.objects.filter(pk=1).first()
    cfg = record.configuration if record else getattr(settings, 'AGENTIC_SUPERSET', {})
    db = cfg.get('SNAPSHOT_DB', {})
    return {'service_url':cfg.get('URL',''), 'public_url':cfg.get('PUBLIC_URL',''),
            'allow_http':cfg.get('ALLOW_HTTP',False), 'username':cfg.get('USERNAME',''),
            'database_id':cfg.get('DATABASE_ID'), 'database_host':db.get('host',''),
            'database_port':db.get('port',5432), 'database_name':db.get('dbname',''),
            'database_user':db.get('user',''), 'database_sslmode':db.get('sslmode','verify-full'),
            'schema':cfg.get('SCHEMA','agentic'), 'private_access_verified':cfg.get('PRIVATE_ACCESS_VERIFIED',False),
            'user_mappings':'\n'.join(f'{k}={v}' for k,v in cfg.get('USERNAMES',{}).items()),
            'default_template':cfg.get('DEFAULT_TEMPLATE','comparison'),
            'map_background':cfg.get('MAP_BACKGROUND', 'deployment'),
            'map_tile_url':cfg.get('MAP_TILE_URL', ''), 'map_tile_attribution':cfg.get('MAP_TILE_ATTRIBUTION', ''),
            'powerbi_mode':cfg.get('POWERBI_MODE', 'disabled'), 'powerbi_portal_url':cfg.get('POWERBI_PORTAL_URL', ''),
            'powerbi_allow_http':cfg.get('POWERBI_ALLOW_HTTP', False),
            'version':record.updated_at.isoformat() if record else 'new'}


def save(actor, data):
    from .access_admin import record_event
    with transaction.atomic():
        record = ReportingConnection.objects.select_for_update().filter(pk=1).first()
        version = record.updated_at.isoformat() if record else 'new'
        if data['version'] != version:
            raise ValueError('Reporting settings changed in another session. Refresh before saving.')
        try:
            secret = credentials(record)
        except ValueError:
            if ((not data['service_url'] or data.get('password'))
                    and (not data.get('database_host') or data.get('database_password'))):
                secret = {}
            else:
                raise ValueError('Re-enter both reporting passwords to recover the connection after an encryption-key change.') from None
        old = record.configuration if record else getattr(settings, 'AGENTIC_SUPERSET', {})
        if data['service_url'] and old.get('URL') and old.get('URL') != data['service_url'] and not data.get('password'):
            raise ValueError('Re-enter the service password when changing the Superset address.')
        old_db = old.get('SNAPSHOT_DB',{})
        if (old_db and any(old_db.get(k) != data.get(field) for k,field in [('host','database_host'),('port','database_port'),('dbname','database_name'),('user','database_user')]) and not data.get('database_password')):
            raise ValueError('Re-enter the writer password when changing its database destination.')
        secret['password'] = data.get('password') or secret.get('password') or old.get('PASSWORD','')
        secret['database_password'] = data.get('database_password') or secret.get('database_password') or old_db.get('password','')
        if not data['service_url']:
            secret['password'] = ''
        if not data.get('database_host'):
            secret['database_password'] = ''
        config = {'URL':data['service_url'], 'PUBLIC_URL':data.get('public_url') or data['service_url'],
            'ALLOW_HTTP':data.get('allow_http',False), 'USERNAME':data.get('username',''),
            'DATABASE_ID':data.get('database_id'), 'SCHEMA':data['schema'], 'USERNAMES':data['mappings'],
            'PRIVATE_ACCESS_VERIFIED':data.get('private_access_verified',False), 'DEFAULT_TEMPLATE':data['default_template']}
        if data.get('map_background'):
            config['MAP_BACKGROUND'] = data['map_background']
            if data['map_background'] == 'custom':
                config['MAP_TILE_URL'] = data['map_tile_url']
                config['MAP_TILE_ATTRIBUTION'] = data['map_tile_attribution']
        else:
            for key in ('MAP_BACKGROUND', 'MAP_TILE_URL', 'MAP_TILE_ATTRIBUTION'):
                if key in old:
                    config[key] = old[key]
        if data.get('powerbi_mode'):
            config.update(POWERBI_MODE=data['powerbi_mode'], POWERBI_PORTAL_URL=data.get('powerbi_portal_url', ''),
                          POWERBI_ALLOW_HTTP=data.get('powerbi_allow_http', False))
        else:
            for key in ('POWERBI_MODE', 'POWERBI_PORTAL_URL', 'POWERBI_ALLOW_HTTP'):
                if key in old:
                    config[key] = old[key]
        if old.get('CA_BUNDLE'):
            config['CA_BUNDLE'] = old['CA_BUNDLE']
        if old.get('DATABASE_ALIAS'):
            config['DATABASE_ALIAS'] = old['DATABASE_ALIAS']
        if old.get('USER_IDS'):
            config['USER_IDS'] = old['USER_IDS']
        if data.get('database_host'):
            config['SNAPSHOT_DB']={'host':data['database_host'], 'port':data['database_port'],
                'dbname':data.get('database_name',''), 'user':data.get('database_user',''),
                'sslmode':data['database_sslmode'], 'connect_timeout':5, 'options':'-c statement_timeout=30000 -c lock_timeout=5000'}
        record = record or ReportingConnection(pk=1)
        record.configuration = config
        record.encrypted_credentials = _cipher(settings.SECRET_KEY).encrypt(json.dumps(secret).encode()).decode()
        record.save()
        record_event(actor,None,'reporting_configured','Reporting setup saved.',
                     {'configured':bool(old.get('URL'))},{'configured':bool(config['URL']),'template':data['default_template'],
                     'map_background':config.get('MAP_BACKGROUND', 'deployment'),
                     'powerbi_mode':config.get('POWERBI_MODE', 'disabled')})
