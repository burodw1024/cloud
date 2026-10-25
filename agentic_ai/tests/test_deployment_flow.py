import io
import json
from pathlib import Path
import subprocess
import tempfile
import shutil
from unittest import skipUnless
from unittest.mock import patch
import urllib.parse
from django.contrib.auth import authenticate, get_user_model, login
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseRedirect
from django.middleware.csrf import get_token
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import path
from agentic_ai.api import agentic_query_api
from deploy.verify_report_flow import verify, inspect_result, SameOriginRedirect
from deploy.verify_imported_images import config_digest
from deploy import push_gitea_source


def login_view(request):
    if request.method == 'POST':
        user = authenticate(request, username=request.POST.get('username'), password=request.POST.get('password'))
        if user:
            login(request, user)
            return HttpResponseRedirect('/agentic/')
    return HttpResponse('<input name="csrfmiddlewaretoken" value="' + get_token(request) + '">')


@login_required(login_url='/login/')
def assistant_view(request):
    return HttpResponse('<input name="csrfmiddlewaretoken" value="' + get_token(request) + '">')


urlpatterns = [path('login/', login_view), path('agentic/', assistant_view),
               path('agentic/api/query/', agentic_query_api)]


class MemoryResponse(io.BytesIO):
    def __init__(self, response, url):
        super().__init__(response.content)
        self.status = response.status_code
        self.url = url

    def geturl(self):
        return self.url


class DjangoOpener:
    def __init__(self):
        self.client = Client(enforce_csrf_checks=True)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        parsed = urllib.parse.urlsplit(request.full_url)
        path = parsed.path + ('?' + parsed.query if parsed.query else '')
        headers = {'HTTP_' + key.upper().replace('-', '_'): value for key, value in request.header_items()
                   if key.lower() != 'content-type'}
        response = self.client.generic(request.get_method(), path, data=request.data or b'',
            content_type=request.get_header('Content-type', 'text/html'), secure=True, **headers)
        url = request.full_url
        if response.status_code == 302:
            url = 'https://testserver' + response['Location']
            response = self.client.get(response['Location'], secure=True)
        return MemoryResponse(response, url)


@override_settings(ROOT_URLCONF=__name__)
class ReportFlowTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user('tester', password='synthetic-test-only')

    def test_login_csrf_and_one_actual_api_dispatch(self):
        result = {'ok': True, 'tool_executed': True, 'tool': 'loan-due-report', 'request_id': 'synthetic-id',
                  'answer': 'A completed report.', 'raw': {'structuredContent': {
                      'columns': ['DUEAMOUNT'], 'rows': [{'DUEAMOUNT': '7.50'}], 'row_count': 1}}}
        opener = DjangoOpener()
        with patch('agentic_ai.api.execute_query', return_value=result) as execute:
            status, body = verify(opener, 'https://testserver', 'tester', 'synthetic-test-only', 'A report question')
        self.assertTrue(inspect_result(status, body)['verified'])
        execute.assert_called_once()
        self.assertEqual(len([r for r in opener.calls if r.full_url.endswith('/api/query/')]), 1)
        self.assertTrue(opener.calls[-1].get_header('X-csrftoken'))
        self.assertTrue(opener.calls[-1].get_header('Idempotency-key'))

    def test_failed_login_dispatches_no_report(self):
        opener = DjangoOpener()
        with patch('agentic_ai.api.execute_query') as execute, self.assertRaises(ValueError):
            verify(opener, 'https://testserver', 'tester', 'wrong', 'Question')
        execute.assert_not_called()

    def test_timeout_approval_and_clarification_never_count_as_success(self):
        for status, body in [(504, {'ok': False}), (202, {'approval_required': True}),
                             (200, {'ok': True, 'tool_executed': False, 'answer': 'Please clarify'})]:
            with self.subTest(status=status), self.assertRaises(ValueError):
                inspect_result(status, body)

    def test_cross_origin_redirect_is_rejected(self):
        from urllib.request import Request
        with self.assertRaises(ValueError):
            SameOriginRedirect().redirect_request(Request('https://testserver/login/'), None, 302, '', {}, 'http://other.example/')


class ImportedImageTests(SimpleTestCase):
    def test_oci_index_selects_amd64_and_verifies_config(self):
        documents = {'index': {'manifests': [
            {'platform': {'os': 'linux', 'architecture': 'arm64'}, 'digest': 'arm'},
            {'platform': {'os': 'linux', 'architecture': 'amd64'}, 'digest': 'x86'}]},
            'x86': {'config': {'digest': 'sha256:expected'}}}
        self.assertEqual(config_digest('index', lambda key: json.dumps(documents[key])), 'sha256:expected')
        with self.assertRaises(ValueError):
            config_digest('index', lambda key: '{"manifests": []}')


@skipUnless(shutil.which("git"), "Git mirroring is tested on the admin host; runtime image does not require Git")
class GiteaPushTests(SimpleTestCase):
    def test_real_non_force_push_preserves_main_and_verifies_release_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, remote = root/'source', root/'remote.git'
            def run(*args):
                return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL, text=True).strip()
            run('init', '--initial-branch=main', str(source))
            run('init', '--bare', '--initial-branch=main', str(remote))
            run('-C', str(source), 'config', 'user.name', 'Synthetic test')
            run('-C', str(source), 'config', 'user.email', 'synthetic@example.invalid')
            (source/'test.txt').write_text('reviewed source')
            run('-C', str(source), 'add', 'test.txt')
            run('-C', str(source), 'commit', '-m', 'test')
            revision = run('-C', str(source), 'rev-parse', 'HEAD')
            with patch.object(push_gitea_source, 'URL', str(remote)):
                push_gitea_source.push_source(source, revision)
            self.assertEqual(run('--git-dir', str(remote), 'rev-parse', 'refs/heads/agentic/releases/' + revision), revision)
            self.assertEqual(run('--git-dir', str(remote), 'for-each-ref', '--format=%(refname)', 'refs/heads/main'), '')


class PortablePackageTests(SimpleTestCase):
    def test_one_file_package_validates_tree_and_supports_offline_description(self):
        from deploy.package_portable import render
        tree = 'a' * 40
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary)/'deploy.sh'
            script.write_text(render(tree))
            syntax = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            result = subprocess.run(['bash', str(script), '--describe'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(tree, result.stdout)
        with self.assertRaises(ValueError):
            render("$(touch /tmp/unreviewed)")
