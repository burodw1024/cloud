"""One real HTTPS request through login, CSRF and the agent API; never retry it."""
import argparse
import getpass
from html.parser import HTMLParser
import http.cookiejar
import json
import re
from pathlib import Path
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAX_BYTES = 4 * 1024 * 1024


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl)[:2] != urllib.parse.urlsplit(request.full_url)[:2]:
            raise ValueError('Cross-origin redirect refused')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class CSRFParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'input' and values.get('name') == 'csrfmiddlewaretoken':
            self.token = values.get('value')


def csrf_token(body):
    parser = CSRFParser()
    parser.feed(body.decode())
    if not parser.token:
        raise ValueError('The login page did not provide a CSRF token')
    return parser.token


def inspect_result(status, result):
    """Require actual tool execution, structured table and a durable request ID."""
    if not isinstance(result, dict):
        raise ValueError('The report response is not an object')
    data = result.get('raw', {}).get('structuredContent', {})
    if not (status == 200 and result.get('ok') is True
            and result.get('tool_executed') is True
            and result.get('tool') == 'loan-due-report'
            and isinstance(result.get('request_id'), str) and result['request_id']
            and isinstance(result.get('answer'), str) and result['answer'].strip()
            and not result.get('clarification_required') and not result.get('approval_required')
            and not result.get('audit_incomplete') and not result.get('outcome_unknown')
            and isinstance(data, dict) and isinstance(data.get('rows'), list)):
        raise ValueError('The report did not complete successfully; inspect the private result and audit log')
    rows = data['rows']
    # A legitimate zero-row report has no columns; record that fact explicitly.
    if rows and not (isinstance(data.get('columns'), list) and data['columns']
                     and all(isinstance(row, dict) and all(c in row for c in data['columns']) for row in rows)):
        raise ValueError('The report response is missing its human-readable table contract')
    return {'verified': True, 'http_status': status, 'tool': result['tool'],
            'request_id': result['request_id'], 'displayed_rows': len(rows),
            'report_rows': data.get('row_count'), 'columns': data.get('columns', []),
            'empty_report': not rows, 'human_readable_answer': True}


def verify(opener, base_url, username, password, query):
    def request(path, data=None, headers=None, timeout=30):
        req = urllib.request.Request(base_url + path, data=data, headers=headers or {})
        try:
            response = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError('Response exceeded verification size limit')
            return response.status, response.geturl(), body

    status, _, body = request('/login/?next=/agentic/')
    if status != 200:
        raise ValueError('HTTPS login page is unavailable')
    token = csrf_token(body)
    form = urllib.parse.urlencode({'username': username, 'password': password,
                                  'csrfmiddlewaretoken': token, 'next': '/agentic/'}).encode()
    status, location, body = request('/login/?next=/agentic/', form,
                                   {'Content-Type': 'application/x-www-form-urlencoded',
                                    'Referer': base_url + '/login/', 'Origin': base_url})
    password = None
    if status != 200 or urllib.parse.urlsplit(location).path != '/agentic/':
        raise ValueError('Login failed or the employee cannot open the assistant')
    # Get an authoritative token from a new form after Django rotates it on login.
    status, location, body = request('/agentic/')
    token = csrf_token(body)
    if status != 200 or urllib.parse.urlsplit(location).path != '/agentic/':
        raise ValueError('Assistant page could not be verified')
    status, _, body = request('/agentic/api/query/', json.dumps({'query': query}).encode(),
        {'Content-Type': 'application/json', 'X-CSRFToken': token,
         'Referer': base_url + '/agentic/', 'Origin': base_url,
         'Idempotency-Key': 'deploy-' + uuid.uuid4().hex}, timeout=1920)
    # No automatic retry: the database tool may still be running after an error.
    return status, json.loads(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--cert', required=True, type=Path)
    parser.add_argument('--stage', required=True, type=Path)
    args = parser.parse_args()
    if urllib.parse.urlsplit(args.url).scheme != 'https':
        parser.error('HTTPS is required')
    username = input('Existing Django username for the report test [root_123]: ').strip() or 'root_123'
    password = getpass.getpass('Django password (hidden, not saved): ')
    query = input('Report question [loan due report, branch 0253, 2026-09-30]: ').strip()
    query = query or 'Show the loan due report for branch 0253 as of 2026-09-30.'
    jar = http.cookiejar.CookieJar()
    context = ssl.create_default_context(cafile=str(args.cert))
    opener = urllib.request.build_opener(SameOriginRedirect(),
        urllib.request.HTTPSHandler(context=context), urllib.request.HTTPCookieProcessor(jar))
    started = time.monotonic()
    summary = {'verified': False}
    try:
        status, result = verify(opener, args.url.rstrip('/'), username, password, query)
        private = args.stage / 'report-response.private.json'
        private.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        private.chmod(0o600)
        summary['http_status'] = status
        if isinstance(result, dict):
            for key in ('request_id', 'code'):
                value = result.get(key)
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
                    summary[key] = value
        summary = inspect_result(status, result)
    except Exception as exc:
        # Do not print remote response bodies, financial records or credentials.
        summary.update(error_type=type(exc).__name__)
        print('Report verification FAILED. No retry was sent. Inspect the private result and AgentRunLog.')
    finally:
        password = None
        jar.clear()
    summary['elapsed_seconds'] = round(time.monotonic() - started, 2)
    (args.stage / 'report-verification.json').write_text(json.dumps(summary, indent=2))
    print('Report verification:', json.dumps(summary))
    return 0 if summary['verified'] else 2


if __name__ == '__main__':
    sys.exit(main())
