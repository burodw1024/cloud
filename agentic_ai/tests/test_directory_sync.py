import copy
import os
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from agentic_ai import directory_sync as sync
from agentic_ai.models import LoanZone, LoanBranch, LoanAccessPolicy, LoanAccessEvent


def payload():
    return {'success': True, 'complete': True, 'version': 1, 'source': 'DW_GC_BRANCHLIST2', 'row_count': 1, 'rows': [{
        'DIVISIONCODE': '01', 'DIVISIONNAME': 'Division', 'ZONECODE': '007', 'ZONENAME': 'Zone',
        'AREACODE': '0004', 'AREANAME': 'Area', 'BRANCHCODE': '00253', 'BRANCHNAME': 'Branch'}]}


class DirectorySyncTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_superuser('directory-admin', password='test')
        self.row = sync.normalize(payload())
        self.fetch = patch.object(sync, 'fetch_snapshot', return_value=self.row)
        self.fetch.start()
        self.addCleanup(self.fetch.stop)

    def apply(self, active=False):
        result = sync.preview(self.actor, active)
        return sync.apply_preview(self.actor, result['ticket'], 'Approved directory refresh')

    def test_default_paused_and_no_policy_created(self):
        self.assertEqual(self.apply(), 1)
        branch = LoanBranch.objects.get(code='00253')
        self.assertFalse(branch.is_active)
        self.assertFalse(branch.zone.is_active)
        self.assertEqual(branch.area_code, '0004')
        self.assertEqual(branch.zone.division_code, '01')
        self.assertFalse(LoanAccessPolicy.objects.exists())
        self.assertEqual(LoanAccessEvent.objects.count(), 3)

    def test_explicit_activate_preserves_paused_and_existing_grant(self):
        self.apply(True)
        branch = LoanBranch.objects.get()
        policy = LoanAccessPolicy.objects.create(user=self.actor, mode='SELECTED')
        policy.zones.add(branch.zone)
        branch.is_active = False
        branch.save()
        self.apply(True)
        branch.refresh_from_db()
        self.assertFalse(branch.is_active)
        self.assertTrue(branch.zone.is_active)
        self.assertEqual(list(policy.zones.values_list('pk', flat=True)), [branch.zone_id])

    def test_stale_source_and_catalog_and_actor_rejected(self):
        result = sync.preview(self.actor)
        other = get_user_model().objects.create_user('other')
        with self.assertRaisesMessage(ValueError, 'own administrator'):
            sync.apply_preview(other, result['ticket'], 'Approved refresh')
        LoanZone.objects.create(code='x', name='Local')
        with self.assertRaisesMessage(ValueError, 'Local directory changed'):
            sync.apply_preview(self.actor, result['ticket'], 'Approved refresh')
        result = sync.preview(self.actor)
        with patch.object(sync, 'fetch_snapshot', return_value=[]):
            with self.assertRaisesMessage(ValueError, 'Oracle directory changed'):
                sync.apply_preview(self.actor, result['ticket'], 'Approved refresh')
        self.assertFalse(LoanBranch.objects.exists())

    def test_apply_atomic_on_audit_failure(self):
        with patch('agentic_ai.access_admin.record_event', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.apply(True)
        self.assertFalse(LoanZone.objects.exists())
        self.assertFalse(LoanBranch.objects.exists())

    def test_missing_rows_are_preserved(self):
        zone = LoanZone.objects.create(code='local', name='Local')
        LoanBranch.objects.create(code='local', name='Local', zone=zone)
        self.apply(True)
        self.assertTrue(LoanBranch.objects.get(code='local').is_active)

    def test_invalid_snapshots(self):
        variants = []
        for field, value in [('complete', False), ('row_count', 2), ('version', True), ('rows', [])]:
            item = payload(); item[field] = value; variants.append(item)
        for field, value in [('BRANCHCODE', 253), ('BRANCHCODE', '1'*33), ('ZONECODE', None), ('AREANAME', ''), ('BRANCHNAME', '')]:
            item = payload(); item['rows'][0][field] = value; variants.append(item)
        item = payload(); item['rows'] *= 2; item['row_count'] = 2; variants.append(item)
        item = payload(); row = copy.deepcopy(item['rows'][0]); row['BRANCHCODE'] = 'other'; row['ZONENAME'] = 'Conflict'; item['rows'].append(row); item['row_count'] = 2; variants.append(item)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                sync.normalize(variant)

    def test_duplicate_names_and_optional_hierarchy_allowed(self):
        item = payload()
        row = copy.deepcopy(item['rows'][0]); row['BRANCHCODE'] = '00254'
        item['rows'].append(row); item['row_count'] = 2
        for row in item['rows']:
            for field in ('AREACODE', 'AREANAME', 'DIVISIONCODE', 'DIVISIONNAME'):
                row[field] = None
        self.assertEqual(len(sync.normalize(item)), 2)

    def test_preview_ui_and_permission(self):
        self.client.force_login(self.actor)
        response = self.client.post('/agentic/data-access/directory/', {'action': 'sync_preview'})
        self.assertContains(response, 'Apply reviewed snapshot')
        self.assertContains(response, '00253')
        self.assertFalse(LoanBranch.objects.exists())
        self.actor.is_superuser = False
        self.actor.save()
        self.assertEqual(self.client.post('/agentic/data-access/directory/', {'action': 'sync_preview'}).status_code, 403)

    @patch.dict(os.environ, {'LOAN_DIRECTORY_TOKEN': 'x'*40}, clear=True)
    def test_connection_rules(self):
        for url in ('http://example.com/api', 'http://127.0.0.1/api', 'https://u:p@example.com/api', 'https://example.com/api?token=x'):
            with patch.dict(os.environ, {'LOAN_DIRECTORY_URL': url, 'LOAN_DIRECTORY_ALLOW_HTTP': '1'}), self.assertRaises(ValueError):
                sync.configuration()
        with patch.dict(os.environ, {'LOAN_DIRECTORY_URL': 'http://192.168.1.139:8001/api/reference/branches', 'LOAN_DIRECTORY_ALLOW_HTTP': '1'}):
            self.assertTrue(sync.configuration()[0].startswith('http://192.'))

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(ValueError):
            sync._object([('complete', True), ('complete', False)])


class DirectoryHttpTests(TestCase):
    @patch.dict(os.environ, {'LOAN_DIRECTORY_URL': 'https://directory.example/api/reference/branches', 'LOAN_DIRECTORY_TOKEN': 'x'*40}, clear=True)
    def test_authenticated_bounded_client_and_invalid_responses(self):
        import json
        import requests
        with patch.object(sync.requests, 'Session') as session:
            client = session.return_value.__enter__.return_value
            response = client.get.return_value.__enter__.return_value
            response.status_code = 200
            response.headers = {'Content-Type': 'application/json; charset=utf-8'}
            response.iter_content.return_value = [json.dumps(payload()).encode()]
            self.assertEqual(sync.fetch_snapshot()[0]['BRANCHCODE'], '00253')
            self.assertFalse(client.trust_env)
            options = client.get.call_args.kwargs
            self.assertEqual(options['headers']['Authorization'], 'Bearer ' + 'x'*40)
            self.assertFalse(options['allow_redirects'])
            self.assertIs(options['verify'], True)
            self.assertEqual(options['timeout'], (5, 30))
            for code in (302, 401, 500):
                response.status_code = code
                with self.assertRaises(ValueError):
                    sync.fetch_snapshot()
            response.status_code = 200
            response.headers = {'Content-Type': 'text/html'}
            with self.assertRaises(ValueError):
                sync.fetch_snapshot()
            response.headers = {'Content-Type': 'application/json'}
            response.iter_content.return_value = [b'x' * (sync.MAX_BYTES + 1)]
            with self.assertRaisesMessage(ValueError, 'exceeded 4 MB'):
                sync.fetch_snapshot()
            response.iter_content.return_value = [b'{"complete":true,"complete":false}']
            with self.assertRaisesMessage(ValueError, 'Duplicate JSON'):
                sync.fetch_snapshot()
            client.get.side_effect = requests.ConnectionError('secret-in-upstream-error')
            with self.assertRaises(ValueError) as caught:
                sync.fetch_snapshot()
            self.assertNotIn('secret-in-upstream-error', str(caught.exception))
