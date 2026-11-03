"""Private, immutable report snapshots and Superset REST publication.

Only generated identifiers reach SQL. Superset credentials and database bindings
are supplied by the deployment administrator, never by a browser or model.
"""
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.db import connections, transaction

MAX_ROWS = 100
FIELD_LABELS = {
    'BRANCHCODE': 'Branch', 'ZONECODE': 'Zone', 'AREACODE': 'Area',
    'DIVISIONCODE': 'Division', 'GS_BALANCE': 'General savings',
    'CS_BALANCE': 'Compulsory savings', 'DUEAMOUNT': 'Due amount',
    'OUTSTANDINGP': 'Outstanding principal', 'OUTSTANDING_PI': 'Outstanding including interest',
    'DUE_DAYS': 'Days due', 'LOAN_COUNT': 'Loan count', 'PRODUCTNAME': 'Product',
}


def field_label(column):
    return FIELD_LABELS.get(column, column.replace('_', ' ').title())


class PublishError(Exception):
    pass


def configuration(user):
    config = dict(getattr(settings, 'AGENTIC_SUPERSET', {}))
    parsed = urlsplit(config.get('URL', ''))
    if (parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
        raise PublishError('Configure a Superset origin URL in AGENTIC_SUPERSET.')
    if parsed.scheme != 'https' and not config.get('ALLOW_HTTP', False):
        raise PublishError('Superset requires HTTPS unless internal HTTP is explicitly enabled.')
    owner = config.get('USER_IDS', {}).get(str(user.pk))
    if type(owner) is not int or owner <= 0:
        raise PublishError('Map your account to a dedicated Superset user before exporting.')
    if list(config['USER_IDS'].values()).count(owner) != 1:
        raise PublishError('Each publishing account needs its own Superset user mapping.')
    if (not config.get('USERNAME') or not config.get('PASSWORD')
            or type(config.get('DATABASE_ID')) is not int or config['DATABASE_ID'] <= 0
            or not config.get('PRIVATE_ACCESS_VERIFIED')):
        raise PublishError('Complete Superset credentials, database and private-access verification.')
    alias = config.get('DATABASE_ALIAS', 'superset_snapshots')
    if alias == 'default' or alias not in settings.DATABASES:
        raise PublishError('Configure a separate PostgreSQL snapshot database alias.')
    if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', config.get('SCHEMA', 'agentic')):
        raise PublishError('Invalid snapshot schema configuration.')
    return {**config, 'URL': config['URL'].rstrip('/'), 'OWNER_ID': owner,
            'DATABASE_ALIAS': alias, 'SCHEMA': config.get('SCHEMA', 'agentic')}


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() and abs(parsed) < Decimal('1e24') else None
    except InvalidOperation:
        return None


def specification(report, title, dimension=None, metrics=None):
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 150:
        raise PublishError('Choose a title of 1–150 characters.')
    rows, columns = report.get('rows'), report.get('columns')
    if (not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS
            or not isinstance(columns, list) or len(columns) > 50
            or any(not isinstance(c, str) or not c or len(c) > 100 for c in columns)
            or any(not isinstance(row, dict) for row in rows)):
        raise PublishError('This report cannot be exported as a dashboard snapshot.')
    # Codes and dates remain dimensions even when represented by digits.
    measures = [c for c in columns if not any(s in c.upper() for s in
                ('CODE', 'DATE', 'RANK', 'FREQUENCY', 'NAME'))
                and any(row.get(c) is not None for row in rows)
                and all(row.get(c) is None or number(row[c]) is not None for row in rows)]
    dimensions = [c for c in columns if c not in measures]
    if dimension is None:
        dimension = next((c for c in dimensions if 'CODE' in c.upper()),
                         dimensions[0] if dimensions else None)
    elif dimension == '':
        dimension = None  # Explicit row-number selection in the studio.
    metrics = measures[:3] if metrics is None else metrics
    if (not isinstance(metrics, list) or len(metrics) > 4
            or any(not isinstance(m, str) or m not in measures for m in metrics)
            or len(set(metrics)) != len(metrics)):
        raise PublishError('Select up to four numeric report fields, or none for a table.')
    if dimension is not None and dimension not in dimensions:
        raise PublishError('Select a dimension from the report.')
    # Table and per-row bars preserve report granularity. No invented SUM/KPI,
    # percentage or historical trend from a potentially limited snapshot.
    return {'title': title.strip(), 'dimension': dimension, 'metrics': metrics,
            'labels': {c: field_label(c) for c in columns},
            'dimensions': dimensions, 'measures': measures, 'rows': len(rows),
            'report_date': report.get('report_date'), 'truncated': bool(report.get('truncated')),
            'mode': 'snapshot', 'charts': ['Source table'] + [f'{m} by displayed row' for m in metrics],
            'notice': 'Immutable snapshot of displayed rows (maximum 100). No automatic refresh. '
                      'Values are shown per row; totals and trends are not inferred.'}


def stage(export, report, config):
    connection = connections[config['DATABASE_ALIAS']]
    if connection.vendor != 'postgresql':
        raise PublishError('Snapshot staging requires PostgreSQL.')
    table = 'snapshot_' + export.id.hex
    export.table_name = table
    export.save(update_fields=['table_name'])
    quoted = connection.ops.quote_name
    target = f'{quoted(config["SCHEMA"])}.{quoted(table)}'
    measures = set(export.specification['measures'])
    # c0, c1… avoid identifiers supplied by upstream tools. Preserve labels in metadata.
    columns = report['columns']
    definitions = ['"row_no" INTEGER NOT NULL', '"row_label" TEXT NOT NULL'] + [
        f'{quoted("c" + str(i))} {"NUMERIC" if c in measures else "TEXT"}' for i, c in enumerate(columns)]
    with transaction.atomic(using=config['DATABASE_ALIAS']):
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE TABLE {target} ({", ".join(definitions)})')
            values = []
            for i, row in enumerate(report['rows'], 1):
                value = row.get(export.specification['dimension'])
                label = str(value if value is not None else 'Row')[:200]
                values.append([i, f'{label} · {i}'] + [number(row.get(c)) if c in measures
                              else None if row.get(c) is None else str(row[c])[:2000] for c in columns])
            cursor.executemany(f'INSERT INTO {target} VALUES ({", ".join(["%s"] * (len(columns) + 2))})', values)
    return table


class SupersetClient:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.verify = config.get('CA_BUNDLE', True)
        if not self.session.verify:
            raise PublishError('TLS certificate verification cannot be disabled.')

    def call(self, method, path, payload=None):
        try:
            response = self.session.request(method, self.config['URL'] + '/api/v1/' + path,
                json=payload, timeout=(5, 30), allow_redirects=False)
            if not 200 <= response.status_code < 300:
                raise PublishError(f'Superset rejected an operation (HTTP {response.status_code}).')
            return response.json() if response.content else {}
        except (requests.RequestException, ValueError):
            raise PublishError('Superset response unavailable. Check export records before retrying.') from None

    def login(self):
        result = self.call('POST', 'security/login', {'username': self.config['USERNAME'],
            'password': self.config['PASSWORD'], 'provider': 'db', 'refresh': False})
        token = result.get('access_token')
        if not isinstance(token, str) or not token:
            raise PublishError('Superset login returned no access token.')
        self.session.headers['Authorization'] = 'Bearer ' + token
        csrf = self.call('GET', 'security/csrf_token/').get('result')
        if not isinstance(csrf, str) or not csrf:
            raise PublishError('Superset returned no CSRF token.')
        self.session.headers.update({'X-CSRFToken': csrf, 'Referer': self.config['URL'] + '/'})

    def close(self):
        self.session.close()


def created_id(response):
    identifier = response.get('id')
    if type(identifier) is not int or identifier <= 0:
        raise PublishError('Superset returned an invalid object identifier; review the export.')
    return identifier


def publish(export, report, config, authorize):
    client = SupersetClient(config)
    try:
        client.login()
        if not authorize():
            raise PublishError('Report access changed before publication.')
        stage(export, report, config)
        export.dataset_id = created_id(client.call('POST', 'dataset/', {
            'database': config['DATABASE_ID'], 'schema': config['SCHEMA'],
            'table_name': export.table_name, 'owners': [config['OWNER_ID']]}))
        export.save(update_fields=['dataset_id'])
        dataset_info = client.call('GET', f'dataset/{export.dataset_id}').get('result', {})
        labels = {'row_no': 'Displayed row', 'row_label': 'Report row'}
        labels.update({'c' + str(i): field_label(label) for i, label in enumerate(report['columns'])})
        dataset_columns = dataset_info.get('columns', [])
        if dataset_columns:
            client.call('PUT', f'dataset/{export.dataset_id}', {'columns': [
                {'id': col['id'], 'column_name': col['column_name'],
                 'verbose_name': labels.get(col['column_name'], col['column_name'])}
                for col in dataset_columns]})
        # Unpublished, owner-only dashboard. No guest token or public roles.
        export.dashboard_id = created_id(client.call('POST', 'dashboard/', {
            'dashboard_title': export.title, 'published': False,
            'owners': [config['OWNER_ID']], 'roles': []}))
        export.save(update_fields=['dashboard_id'])
        dataset = f'{export.dataset_id}__table'
        table_columns = ['row_label'] + ['c' + str(i) for i in range(len(report['columns']))]
        chart_specs = [('Source table', 'table', {'query_mode': 'raw', 'all_columns': table_columns,
                        'order_by_cols': [json.dumps(['row_no', True])], 'row_limit': MAX_ROWS})]
        for metric in export.specification['metrics']:
            column = 'c' + str(report['columns'].index(metric))
            chart_specs.append((field_label(metric) + ' · displayed rows', 'echarts_timeseries_bar', {
                'x_axis': 'row_label', 'metrics': [{'expressionType': 'SIMPLE',
                    'column': {'column_name': column}, 'aggregate': 'MAX',
                    'label': field_label(metric), 'optionName': 'metric_' + column}],
                'groupby': [], 'row_limit': MAX_ROWS, 'orientation': 'horizontal',
                'show_legend': False, 'show_value': True, 'color_scheme': 'supersetColors',
                'truncate_metric': False}))
        for title, viz_type, options in chart_specs:
            if not authorize():
                raise PublishError('Report access changed during publication; remove the export.')
            chart_id = created_id(client.call('POST', 'chart/', {
                'slice_name': title, 'viz_type': viz_type, 'datasource_id': export.dataset_id,
                'datasource_type': 'table', 'owners': [config['OWNER_ID']],
                'dashboards': [export.dashboard_id],
                'description': export.specification['notice'],
                'params': json.dumps({'datasource': dataset, 'viz_type': viz_type,
                    'adhoc_filters': [], **options})}))
            export.chart_ids = export.chart_ids + [chart_id]
            export.save(update_fields=['chart_ids'])
            query = {'columns': table_columns if viz_type == 'table' else ['row_label'],
                     'metrics': [] if viz_type == 'table' else options['metrics'],
                     'filters': [], 'extras': {}, 'row_limit': MAX_ROWS,
                     'order_desc': False}
            probe = client.call('POST', 'chart/data', {
                'datasource': {'id': export.dataset_id, 'type': 'table'},
                'force': True, 'queries': [query], 'result_format': 'json', 'result_type': 'full'})
            results = probe.get('result')
            if (not isinstance(results, list) or not results or results[0].get('error')
                    or results[0].get('status') == 'failed' or not results[0].get('data')):
                raise PublishError('A chart query failed validation. Review or remove the export.')
        layout = {'DASHBOARD_VERSION_KEY': 'v2',
            'ROOT_ID': {'id': 'ROOT_ID', 'type': 'ROOT', 'children': ['GRID_ID']},
            'GRID_ID': {'id': 'GRID_ID', 'type': 'GRID', 'children': [], 'parents': ['ROOT_ID']}}
        layout['GRID_ID']['children'].append('NOTICE-ROW')
        layout['NOTICE-ROW'] = {'id': 'NOTICE-ROW', 'type': 'ROW', 'children': ['NOTICE'],
                                'parents': ['ROOT_ID', 'GRID_ID'], 'meta': {}}
        scope_notice = export.specification['notice']
        if export.specification['report_date']:
            scope_notice += ' Report date: ' + str(export.specification['report_date'])
        if export.specification['truncated']:
            scope_notice += ' Source result is limited; this is not the complete portfolio.'
        # Only server-authored text and a strictly formatted date enter Markdown.
        safe_notice = re.sub(r'[^A-Za-z0-9 .,;:()—/-]', '', scope_notice)
        layout['NOTICE'] = {'id': 'NOTICE', 'type': 'MARKDOWN', 'children': [],
            'parents': ['ROOT_ID', 'GRID_ID', 'NOTICE-ROW'],
            'meta': {'code': safe_notice, 'width': 12, 'height': 15}}
        for index, chart_id in enumerate(export.chart_ids):
            row_id, component = f'ROW-{index}', f'CHART-{chart_id}'
            layout['GRID_ID']['children'].append(row_id)
            layout[row_id] = {'id': row_id, 'type': 'ROW', 'children': [component],
                              'parents': ['ROOT_ID', 'GRID_ID'], 'meta': {'background': 'BACKGROUND_TRANSPARENT'}}
            layout[component] = {'id': component, 'type': 'CHART', 'children': [],
                'parents': ['ROOT_ID', 'GRID_ID', row_id],
                'meta': {'chartId': chart_id, 'width': 12, 'height': 50}}
        client.call('PUT', f'dashboard/{export.dashboard_id}', {
            'position_json': json.dumps(layout), 'published': False,
            'json_metadata': json.dumps({'refresh_frequency': 0}),
            'owners': [config['OWNER_ID']], 'roles': []})
        # Ensure persisted form data survived API schema normalization.
        for chart_id in export.chart_ids:
            chart = client.call('GET', f'chart/{chart_id}').get('result', {})
            form = json.loads(chart.get('params') or '{}')
            # A table probe confirms dataset access and materialized rows. Full
            # visual compatibility is additionally checked in deployment smoke tests.
            if not form.get('datasource'):
                raise PublishError('Chart verification failed.')
        if not authorize():
            raise PublishError('Report access changed; remove the exported snapshot.')
        export.status = 'ready'
        export.save(update_fields=['status'])
    finally:
        client.close()


def remove(export, config):
    """Idempotent cleanup; keep failure records for uncertain remote outcomes."""
    client = SupersetClient(config)
    try:
        client.login()
        objects = ([('dashboard', export.dashboard_id)] if export.dashboard_id else [])
        objects += [('chart', identifier) for identifier in export.chart_ids]
        objects += [('dataset', export.dataset_id)] if export.dataset_id else []
        for kind, identifier in objects:
            try:
                client.call('DELETE', f'{kind}/{identifier}')
            except PublishError as exc:
                if '(HTTP 404)' not in str(exc):
                    raise
        if export.table_name:
            if not re.fullmatch(r'snapshot_[a-f0-9]{32}', export.table_name):
                raise PublishError('Invalid recorded snapshot table.')
            connection = connections[config['DATABASE_ALIAS']]
            quote = connection.ops.quote_name
            with connection.cursor() as cursor:
                cursor.execute(f'DROP TABLE IF EXISTS {quote(config["SCHEMA"])}.{quote(export.table_name)}')
        export.status = 'deleted'
        export.save(update_fields=['status'])
    finally:
        client.close()
