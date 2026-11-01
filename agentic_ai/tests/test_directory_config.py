import json
import os
from unittest.mock import patch
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings, Client
from agentic_ai import directory_config as config
from agentic_ai.directory_sync import configuration
from agentic_ai.models import LoanDirectoryConnection, LoanAccessEvent, LoanBranch
from .test_directory_sync import payload

TOKEN = 'test-only-directory-token-at-least-32-bytes'
URL = 'http://192.168.1.139:8005/api/reference/branches'


class DirectoryConfigurationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('integration-admin')
        self.client.force_login(self.admin)

    def post(self, **kwargs):
        body = dict(action='save_directory', url=URL, allow_http=True, token=TOKEN, version=config.public_status()['version'])
        body.update(kwargs)
        return self.client.post('/agentic/setup/', json.dumps(body), content_type='application/json')

    def test_save_encrypts_and_immediately_drives_sync(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        row = LoanDirectoryConnection.objects.get(pk=1)
        self.assertNotIn(TOKEN, row.encrypted_token)
        self.assertEqual(configuration(), (URL, TOKEN))
        self.assertNotIn(TOKEN, response.content.decode())
        event = LoanAccessEvent.objects.get(action='directory_configured')
        self.assertNotIn(TOKEN, json.dumps(event.before) + json.dumps(event.after))
        self.assertFalse(LoanBranch.objects.exists())
        response = self.client.get('/agentic/setup/')
        self.assertContains(response, URL)
        self.assertNotContains(response, TOKEN)
        self.assertNotContains(response, row.encrypted_token)
        self.assertContains(response, 'Token configured')
        response = self.client.get('/agentic/data-access/directory/')
        self.assertContains(response, 'An endpoint is configured.')

    def test_blank_retains_token_and_stale_save_rejected(self):
        first = self.post().json()['version']
        self.assertEqual(self.post(token='').status_code, 200)
        self.assertEqual(configuration()[1], TOKEN)
        response = self.post(version=first, token='replacement-token-at-least-32-bytes')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(configuration()[1], TOKEN)

    def test_new_origin_requires_explicit_token(self):
        self.post()
        self.assertEqual(self.post(url='http://192.168.1.140:8005/api/reference/branches', token='').status_code, 400)
        self.assertEqual(configuration(), (URL, TOKEN))
        self.assertEqual(self.post(url='https://directory.example/api/reference/branches', token=TOKEN, allow_http=False).status_code, 200)

    def test_private_http_opt_in_and_token_validation(self):
        for overrides in [{'allow_http':False}, {'token':'short'}, {'token':'x'*33+'\n'+'y'}, {'url':'http://example.com/api'}, {'url':'http://127.0.0.1/api'}, {'allow_http':'1'}]:
            with self.subTest(overrides=overrides):
                self.assertEqual(self.post(**overrides).status_code, 400)
        self.assertFalse(LoanDirectoryConnection.objects.exists())

    def test_permissions_and_csrf(self):
        self.admin.is_superuser = False
        self.admin.save()
        self.assertEqual(self.post().status_code, 403)
        self.admin.is_superuser = True
        self.admin.save()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post('/agentic/setup/', json.dumps({'action':'save_directory'}), content_type='application/json').status_code, 403)
        self.assertFalse(LoanDirectoryConnection.objects.exists())

    @patch('agentic_ai.directory_sync.fetch_snapshot', return_value=payload()['rows'])
    def test_check_only_counts_no_import(self, fetch):
        response = self.client.post('/agentic/setup/', json.dumps({'action':'check_directory'}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('1 branches across 1 zones', response.json()['message'])
        self.assertFalse(LoanBranch.objects.exists())
        self.assertFalse(LoanDirectoryConnection.objects.exists())

    @patch('agentic_ai.directory_sync.fetch_snapshot', side_effect=ValueError('Directory API rejected the request.'))
    def test_failed_check_safe_message(self, fetch):
        response = self.client.post('/agentic/setup/', json.dumps({'action':'check_directory'}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('rejected', response.json()['error'])

    @patch.dict(os.environ, {'LOAN_DIRECTORY_URL':URL, 'LOAN_DIRECTORY_ALLOW_HTTP':'1', 'LOAN_DIRECTORY_TOKEN':TOKEN})
    def test_environment_fallback_and_admin_precedence(self):
        self.assertEqual(configuration(), (URL, TOKEN))
        alternate = 'https://directory.example/api/reference/branches'
        self.assertEqual(self.post(url=alternate, token=TOKEN, allow_http=False).status_code, 200)
        self.assertEqual(configuration(), (alternate, TOKEN))

    def test_failed_audit_rolls_back_configuration(self):
        with patch('agentic_ai.access_admin.record_event', side_effect=RuntimeError('audit down')):
            self.assertEqual(self.post().status_code, 502)
        self.assertFalse(LoanDirectoryConnection.objects.exists())

    def test_key_rotation_and_token_recovery(self):
        self.post()
        old_key = settings.SECRET_KEY
        with override_settings(SECRET_KEY='new-deployment-secret', SECRET_KEY_FALLBACKS=[old_key]):
            self.assertEqual(configuration(), (URL, TOKEN))
        with override_settings(SECRET_KEY='new-deployment-secret', SECRET_KEY_FALLBACKS=[]):
            with self.assertRaises(ValueError):
                configuration()
            self.client.force_login(self.admin)
            self.assertEqual(self.post(token=TOKEN).status_code, 200)
            self.assertEqual(configuration(), (URL, TOKEN))
