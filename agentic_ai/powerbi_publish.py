"""Publish an authorized snapshot as a paginated Report Server report."""

import base64
import json
import os
import re
import subprocess
from urllib.parse import quote, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from .api import _can_replay_result
from .models import ReportRun
from .reporting_config import workspace_settings

RDL = 'http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition'
SOAP = 'http://schemas.xmlsoap.org/soap/envelope/'
RS = 'http://schemas.microsoft.com/sqlserver/reporting/2010/03/01/ReportServer'
INVALID_XML = re.compile(r'[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]')


class PublicationError(Exception):
    pass


def publishing_config(user):
    if not user.is_active or not user.has_perm('agentic_ai.publish_powerbi_report'):
        return None
    portal = workspace_settings(user)['powerbi']['portal_url']
    account = os.environ.get('AGENTIC_POWERBI_PUBLISH_USER', '')
    password = os.environ.get('AGENTIC_POWERBI_PUBLISH_PASSWORD', '')
    folder = os.environ.get('AGENTIC_POWERBI_PUBLISH_FOLDER', '/BURO Reports')
    if not portal or not account or not password or not re.fullmatch(r'/[\w -]+(?:/[\w -]+)*', folder):
        return None
    return portal, account, password, folder


def child(parent, tag, value=None, **attributes):
    node = ET.SubElement(parent, tag, attributes)
    if value is not None:
        node.text = str(value)
    return node


def textbox(parent, name, value):
    box = child(parent, 'Textbox', Name=name)
    child(box, 'CanGrow', 'true')
    paragraph = child(child(box, 'Paragraphs'), 'Paragraph')
    run = child(child(paragraph, 'TextRuns'), 'TextRun')
    child(run, 'Value', value)
    child(run, 'Style')
    child(paragraph, 'Style')
    child(box, 'Style')
    return box


def literal(value):
    # Report definitions interpret a leading equals sign as an expression.
    return '="' + INVALID_XML.sub('', str(value)).replace('"', '""') + '"'


def detail_report(report):
    columns = report['columns']
    if not columns or len(columns) > 50:
        raise PublicationError('The Detail table supports between 1 and 50 columns.')
    fields = [f'Column{i}' for i in range(len(columns))]
    xml_query = ET.Element('Query')
    inline = child(child(xml_query, 'XmlData'), 'Rows')
    for row in report['rows']:
        item = child(inline, 'Row')
        for field, column in zip(fields, columns):
            value = row.get(column)
            child(item, field, INVALID_XML.sub('', str(value)) if value is not None else '')
    child(xml_query, 'ElementPath', 'Rows/Row')
    root = ET.Element('Report', xmlns=RDL)
    source = child(child(root, 'DataSources'), 'DataSource', Name='Snapshot')
    connection = child(source, 'ConnectionProperties')
    child(connection, 'DataProvider', 'XML')
    child(connection, 'ConnectString', '')
    child(connection, 'IntegratedSecurity', 'true')
    dataset = child(child(root, 'DataSets'), 'DataSet', Name='SnapshotRows')
    query = child(dataset, 'Query')
    child(query, 'DataSourceName', 'Snapshot')
    child(query, 'CommandText', ET.tostring(xml_query, encoding='unicode'))
    definitions = child(dataset, 'Fields')
    for field in fields:
        child(child(definitions, 'Field', Name=field), 'DataField', field)

    section = child(child(root, 'ReportSections'), 'ReportSection')
    body = child(section, 'Body')
    items = child(body, 'ReportItems')
    table = child(items, 'Tablix', Name='DetailTable')
    table_body = child(table, 'TablixBody')
    widths = child(table_body, 'TablixColumns')
    for _ in fields:
        child(child(widths, 'TablixColumn'), 'Width', '1.5in')
    rows = child(table_body, 'TablixRows')
    for prefix, values in [('Header', map(literal, columns)),
                           ('Detail', (f'=Fields!{field}.Value' for field in fields))]:
        row = child(rows, 'TablixRow')
        child(row, 'Height', '0.25in')
        cells = child(row, 'TablixCells')
        for i, value in enumerate(values):
            contents = child(child(cells, 'TablixCell'), 'CellContents')
            textbox(contents, f'{prefix}{i}', value)
    members = child(child(table, 'TablixColumnHierarchy'), 'TablixMembers')
    for _ in fields:
        child(members, 'TablixMember')
    members = child(child(table, 'TablixRowHierarchy'), 'TablixMembers')
    header = child(members, 'TablixMember')
    child(header, 'KeepWithGroup', 'After')
    child(header, 'RepeatOnNewPage', 'true')
    child(child(members, 'TablixMember'), 'Group', Name='Details')
    child(table, 'DataSetName', 'SnapshotRows')
    child(body, 'Height', '0.5in')
    width = len(columns) * 1.5
    child(section, 'Width', f'{width}in')
    page = child(section, 'Page')
    child(page, 'PageWidth', f'{max(8.5, width + 1)}in')
    child(page, 'PageHeight', '11in')
    for side in ('Left', 'Right', 'Top', 'Bottom'):
        child(page, f'{side}Margin', '0.5in')
    return ET.tostring(root, encoding='utf-8', xml_declaration=False)


def upload(portal, account, password, folder, name, definition):
    endpoint = urlsplit(os.environ.get('AGENTIC_POWERBI_SOAP_URL', ''))
    if not endpoint.netloc:
        origin = urlsplit(portal)
        endpoint = origin._replace(path='/ReportServer/ReportService2010.asmx')
    if (endpoint.scheme not in {'http', 'https'} or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.netloc != urlsplit(portal).netloc or endpoint.scheme != urlsplit(portal).scheme):
        raise PublicationError('Configure a Report Server SOAP URL on the same origin as the portal.')
    envelope = ET.Element('Envelope', xmlns=SOAP)
    request = child(child(envelope, 'Body'), 'CreateCatalogItem', xmlns=RS)
    for tag, value in (
        ('ItemType', 'Report'), ('Name', name), ('Parent', folder), ('Overwrite', 'false'),
        ('Definition', base64.b64encode(definition).decode('ascii')),
    ):
        child(request, tag, value)
    child(request, 'Properties')
    payload = ET.tostring(envelope, encoding='unicode')
    # Keep the password and report definition out of the process command line.
    options = {'url': urlunsplit(endpoint), 'user': f'{account}:{password}', 'data-binary': payload}
    config = ''.join(f'{key} = {json.dumps(value, ensure_ascii=False)}\n' for key, value in options.items())
    action = f'SOAPAction: "{RS}/CreateCatalogItem"'
    try:
        response = subprocess.run(
            ['curl', '-q', '--config', '-', '--ntlm', '--noproxy', '*', '--silent',
             '--connect-timeout', '5', '--max-time', '25', '--output', os.devnull,
             '--write-out', '%{http_code}', '--header', action, '--header', 'Content-Type: text/xml; charset=utf-8'],
            input=config, encoding='utf-8', capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PublicationError('Report Server did not confirm publishing. Check the folder before publishing again.') from None
    if response.returncode or response.stdout != '200':
        if response.stdout in {'401', '403'}:
            raise PublicationError('Report Server denied the publishing account. Check its folder permissions.')
        raise PublicationError('Report Server did not confirm publishing. Check the folder before publishing again.')


def publish_run(user, run, *, before_upload=lambda: True):
    config = publishing_config(user)
    if not config or run.user_id != user.pk or not _can_replay_result(user, run.source):
        raise PublicationError('Power BI publishing is unavailable for this report.')
    if run.powerbi_report_url:
        return
    saved = cache.get('agentic:summary:' + run.snapshot_token)
    if not saved or saved['user_id'] != user.pk:
        raise PublicationError('The snapshot expired. Run the question again to publish it.')
    portal, account, password, folder = config
    name = f'BURO-{run.pk}'
    definition = detail_report(saved['report'])
    if len(definition) > 3_000_000:
        raise PublicationError('The result is too large to publish as a Detail table.')
    if not before_upload():
        raise PublicationError('The worker stopped before publishing. The report was saved.')
    upload(portal, account, password, folder, name, definition)
    run.powerbi_report_url = f'{portal}/report{quote(folder)}/{name}'
    run.save(update_fields=['powerbi_report_url'])


@login_required
@require_POST
@csrf_protect
def powerbi_publish(request, run_id):
    run = get_object_or_404(ReportRun, pk=run_id, user=request.user)
    try:
        publish_run(request.user, run)
    except PublicationError as exc:
        return HttpResponse(str(exc), status=400, content_type='text/plain', headers={'Cache-Control': 'no-store'})
    return redirect('agentic_ai:report_run', run_id=run.pk)
