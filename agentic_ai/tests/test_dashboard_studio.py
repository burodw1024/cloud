import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from contextlib import nullcontext
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings

from agentic_ai import dashboard_studio as studio
from agentic_ai import superset_publisher as publisher
from agentic_ai.models import DashboardExport


REPORT = {'columns': ['BRANCHCODE', 'GS_BALANCE', 'CS_BALANCE'],
          'rows': [{'BRANCHCODE': '0007', 'GS_BALANCE': '120.25', 'CS_BALANCE': '50'},
                   {'BRANCHCODE': '0008', 'GS_BALANCE': '-10', 'CS_BALANCE': None}],
          'report_date': '2026-09-30', 'truncated': True}


class DesignTests(SimpleTestCase):
    def test_automatic_design_preserves_codes_and_avoids_invented_totals(self):
        spec = publisher.specification(REPORT, ' Savings ')
        self.assertEqual(spec['dimension'], 'BRANCHCODE')
        self.assertEqual(spec['metrics'], ['GS_BALANCE', 'CS_BALANCE'])
        self.assertEqual(spec['title'], 'Savings')
        self.assertTrue(spec['truncated'])
        self.assertNotIn('SUM', json.dumps(spec))
        self.assertIn('No automatic refresh', spec['notice'])

    def test_invalid_measures_and_dimensions_rejected(self):
        for metrics in [['BRANCHCODE'], ['GS_BALANCE', 'GS_BALANCE'], ['not_a_field']]:
            with self.assertRaises(publisher.PublishError):
                publisher.specification(REPORT, 'Report', metrics=metrics)
        with self.assertRaises(publisher.PublishError):
            publisher.specification(REPORT, 'Report', dimension='user SQL')

    def test_non_numeric_and_non_finite_data_not_charted(self):
        for value in ['NaN', 'Infinity', True, '1e100', '<script>']:
            report = {'columns': ['VALUE'], 'rows': [{'VALUE': value}]}
            self.assertEqual(publisher.specification(report, 'Report')['metrics'], [])
        self.assertEqual(publisher.number('-120.20'), Decimal('-120.20'))

    def test_numeric_only_report_has_row_number_labels(self):
        spec = publisher.specification({'columns': ['GS_BALANCE'], 'rows': [{'GS_BALANCE': '12'}]}, 'Report')
        self.assertIsNone(spec['dimension'])
        explicit = publisher.specification(REPORT, 'Report', dimension='', metrics=[])
        self.assertIsNone(explicit['dimension'])
        self.assertEqual(explicit['charts'], ['Source table'])

    def test_empty_and_oversized_reports_rejected(self):
        for rows in [[], REPORT['rows'] * 51]:
            with self.assertRaises(publisher.PublishError):
                publisher.specification({**REPORT, 'rows': rows}, 'Report')


class StudioTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_superuser(username='publisher', password='password')
        self.client.force_login(self.user)
        self.url = '/agentic/api/dashboards/'
        self.saved = {'user_id': self.user.pk, 'tool': 'report', 'plan': {}, 'report': REPORT}
        cache.set('agentic:summary:report-token', self.saved, 1800)
        session = self.client.session
        session['dashboard_report_token'] = 'report-token'
        session.save()
        self.config = {'URL': 'https://superset.internal', 'DATABASE_ALIAS': 'analytics',
                       'DATABASE_ID': 4, 'SCHEMA': 'agentic', 'OWNER_ID': 27}
        self.access = patch.object(studio, '_can_replay_result', return_value=True)
        self.access.start()
        self.addCleanup(self.access.stop)
        self.config_patch = patch.object(studio, 'configuration', return_value=self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def post(self, body, client=None):
        return (client or self.client).post(self.url, json.dumps(body), content_type='application/json')

    def preview(self):
        reply = self.post({'action': 'preview', 'title': 'Savings'})
        self.assertEqual(reply.status_code, 200, reply.content)
        return reply.json()['export_id']

    def test_preview_uses_only_server_held_report(self):
        identifier = self.preview()
        export = DashboardExport.objects.get(pk=identifier)
        self.assertEqual(export.specification['metrics'], ['GS_BALANCE', 'CS_BALANCE'])
        self.assertNotIn('rows', export.source)
        self.assertEqual(export.source['report_token'], 'report-token')
        self.assertEqual(self.post({'action': 'preview', 'rows': REPORT['rows']}).status_code, 400)

    def test_permission_and_csrf_enforced(self):
        self.assertEqual(self.post({'action': 'preview'}, Client()).status_code, 403)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(self.post({'action': 'preview'}, csrf_client).status_code, 403)
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(self.post({'action': 'preview'}).status_code, 403)

    def test_other_user_cannot_read_or_publish_preview(self):
        identifier = self.preview()
        other = get_user_model().objects.create_superuser(username='other', password='password')
        self.client.force_login(other)
        self.assertEqual(self.post({'action': 'publish', 'export_id': identifier}).status_code, 404)
        self.assertEqual(self.post({'action': 'preview'}).status_code, 400)

    def test_expired_or_revoked_source_rejected(self):
        identifier = self.preview()
        with patch.object(studio, '_can_replay_result', return_value=False):
            self.assertEqual(self.post({'action': 'publish', 'export_id': identifier}).status_code, 403)
            self.assertEqual(self.post({'action': 'preview'}).status_code, 403)
        cache.delete('agentic:summary:report-token')
        self.assertEqual(self.post({'action': 'publish', 'export_id': identifier}).status_code, 400)

    @patch.object(studio, 'publish')
    def test_publish_once_with_durable_duplicate_protection(self, publish):
        identifier = self.preview()
        def complete(export, report, config, authorize):
            self.assertTrue(authorize())
            self.assertEqual(report, REPORT)
            export.status = 'ready'; export.dashboard_id = 99
            export.save()
        publish.side_effect = complete
        result = self.post({'action': 'publish', 'export_id': identifier})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['url'].endswith('/99/'))
        self.assertEqual(self.post({'action': 'publish', 'export_id': identifier}).status_code, 409)
        publish.assert_called_once()

    @patch.object(studio, 'publish', side_effect=RuntimeError('password=private'))
    def test_partial_failure_is_journaled_and_not_retried(self, publish):
        identifier = self.preview()
        reply = self.post({'action': 'publish', 'export_id': identifier})
        self.assertEqual(reply.status_code, 502)
        self.assertNotIn('private', reply.content.decode())
        self.assertEqual(DashboardExport.objects.get(pk=identifier).status, 'failed')
        self.assertEqual(self.post({'action': 'publish', 'export_id': identifier}).status_code, 409)
        publish.assert_called_once()

    @patch.object(studio, 'remove')
    def test_cleanup_allowed_after_access_revocation(self, remove):
        identifier = self.preview()
        DashboardExport.objects.filter(pk=identifier).update(status='failed')
        with patch.object(studio, '_can_replay_result', return_value=False):
            self.assertEqual(self.post({'action': 'delete', 'export_id': identifier}).status_code, 200)
        remove.assert_called_once()

    def test_open_rechecks_current_access(self):
        identifier = self.preview()
        DashboardExport.objects.filter(pk=identifier).update(status='ready', dashboard_id=99)
        with patch.object(studio, '_can_replay_result', return_value=False):
            self.assertEqual(self.post({'action': 'open', 'export_id': identifier}).status_code, 403)

    def test_destination_drift_blocked(self):
        identifier = self.preview()
        export = DashboardExport.objects.get(pk=identifier)
        export.source['binding'] = {**studio.binding(self.config), 'DATABASE_ID': 100}
        export.save()
        self.assertEqual(self.post({'action': 'publish', 'export_id': identifier}).status_code, 400)

    def test_studio_renders_and_is_not_cached(self):
        reply = self.client.get('/agentic/analytics/')
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply['Cache-Control'], 'no-store')
        self.assertContains(reply, 'Analytics studio')


class PublisherTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='owner')
        self.export = DashboardExport.objects.create(user=self.user, title='Savings',
                          specification=publisher.specification(REPORT, 'Savings'))
        self.config = {'URL': 'https://superset.internal', 'DATABASE_ALIAS': 'snapshots',
                       'DATABASE_ID': 4, 'SCHEMA': 'agentic', 'OWNER_ID': 27}

    @patch.object(publisher, 'stage')
    @patch.object(publisher, 'SupersetClient')
    def test_rest_contract_layout_and_query_validation(self, client_class, stage):
        client = client_class.return_value
        next_id = iter([10, 20, 31, 32, 33])
        def call(method, path, payload=None):
            if method == 'POST' and path == 'chart/data':
                self.assertEqual(payload['datasource'], {'id': 10, 'type': 'table'})
                return {'result': [{'data': [{'value': 1}], 'status': 'success'}]}
            if method == 'POST':
                return {'id': next(next_id)}
            if method == 'GET' and path.startswith('chart/'):
                return {'result': {'params': json.dumps({'datasource': '10__table'})}}
            return {'result': {}}
        client.call.side_effect = call
        publisher.publish(self.export, REPORT, self.config, lambda: True)
        self.export.refresh_from_db()
        self.assertEqual(self.export.status, 'ready')
        self.assertEqual(self.export.chart_ids, [31, 32, 33])
        posts = [args.args for args in client.call.call_args_list if args.args[0] == 'POST']
        dashboard = next(args[2] for args in posts if args[1] == 'dashboard/')
        self.assertFalse(dashboard['published'])
        self.assertEqual(dashboard['roles'], [])
        self.assertEqual(dashboard['owners'], [27])
        chart = next(args[2] for args in posts if args[1] == 'chart/' and args[2]['viz_type'] != 'table')
        self.assertEqual(json.loads(chart['params'])['metrics'][0]['aggregate'], 'MAX')
        put = next(args.args[2] for args in client.call.call_args_list if args.args[:2] == ('PUT', 'dashboard/20'))
        layout = json.loads(put['position_json'])
        self.assertEqual(layout['CHART-31']['meta']['chartId'], 31)
        self.assertIn('limited', layout['NOTICE']['meta']['code'])
        client.close.assert_called_once()

    @patch.object(publisher, 'stage')
    @patch.object(publisher, 'SupersetClient')
    def test_revocation_before_staging_stops_publication(self, client, stage):
        with self.assertRaises(publisher.PublishError):
            publisher.publish(self.export, REPORT, self.config, lambda: False)
        stage.assert_not_called()
        client.return_value.close.assert_called_once()

    @patch.object(publisher, 'stage')
    @patch.object(publisher, 'SupersetClient')
    def test_failed_chart_query_does_not_report_ready(self, client_class, stage):
        client = client_class.return_value
        client.call.side_effect = lambda method, path, payload=None: (
            {'result': [{'error': 'query failed'}]} if path == 'chart/data' else
            {'id': 10} if method == 'POST' else {'result': {}})
        with self.assertRaises(publisher.PublishError):
            publisher.publish(self.export, REPORT, self.config, lambda: True)
        self.export.refresh_from_db()
        self.assertNotEqual(self.export.status, 'ready')
        self.assertEqual(self.export.chart_ids, [10])

    @patch.object(publisher, 'SupersetClient')
    def test_cleanup_removes_remote_objects_in_order(self, client_class):
        self.export.dashboard_id = 20; self.export.chart_ids = [31, 32]; self.export.dataset_id = 10
        publisher.remove(self.export, self.config)
        calls = [entry.args for entry in client_class.return_value.call.call_args_list]
        self.assertEqual(calls, [('DELETE', 'dashboard/20'), ('DELETE', 'chart/31'),
                                 ('DELETE', 'chart/32'), ('DELETE', 'dataset/10')])
        self.export.refresh_from_db()
        self.assertEqual(self.export.status, 'deleted')


    def test_staging_parameterizes_values_and_preserves_codes(self):
        connection = MagicMock()
        connection.vendor = 'postgresql'
        connection.ops.quote_name.side_effect = lambda value: '"' + value + '"'
        cursor = connection.cursor.return_value.__enter__.return_value
        report = {**REPORT, 'rows': [dict(REPORT['rows'][0], BRANCHCODE="0007'; DROP TABLE users;--")]}
        with patch.object(publisher, 'connections', {'snapshots': connection}), patch.object(
                publisher.transaction, 'atomic', return_value=nullcontext()):
            table = publisher.stage(self.export, report, self.config)
        self.assertRegex(table, r'^snapshot_[a-f0-9]{32}$')
        sql, values = cursor.executemany.call_args.args
        self.assertNotIn('DROP', sql)
        self.assertEqual(values[0][2], "0007'; DROP TABLE users;--")
        self.assertEqual(values[0][3], Decimal('120.25'))
        self.assertIn('%s', sql)

    def test_cleanup_command_dry_run_then_applies_revocation(self):
        self.export.status = 'ready'
        self.export.source = {'binding': studio.binding(self.config)}
        self.export.save()
        command_module = 'agentic_ai.management.commands.cleanup_dashboard_exports'
        with patch(command_module + '.current_authorization', return_value=False), patch(
                command_module + '.configuration', return_value=self.config), patch(command_module + '.remove') as remove:
            call_command('cleanup_dashboard_exports', stdout=StringIO())
            remove.assert_not_called()
            call_command('cleanup_dashboard_exports', apply=True, stdout=StringIO())
            remove.assert_called_once()


class ConfigurationTests(SimpleTestCase):
    def config(self, **changes):
        return {'URL': 'https://superset.internal', 'USERNAME': 'service', 'PASSWORD': 'secret',
                'DATABASE_ID': 4, 'USER_IDS': {'12': 27}, 'PRIVATE_ACCESS_VERIFIED': True,
                **changes}

    @override_settings(DATABASES={'superset_snapshots': {'ENGINE': 'django.db.backends.postgresql'}})
    def test_private_destination_must_be_explicitly_configured(self):
        with override_settings(AGENTIC_SUPERSET=self.config()):
            self.assertEqual(publisher.configuration(SimpleNamespace(pk=12))['OWNER_ID'], 27)
        for changes in [{'PRIVATE_ACCESS_VERIFIED': False}, {'USER_IDS': {}},
                        {'URL': 'http://superset.internal'}, {'URL': 'https://user:secret@host'},
                        {'DATABASE_ALIAS': 'default'}, {'USER_IDS': {'12': 27, '13': 27}},
                        {'SCHEMA': 'agentic; DROP TABLE users'}]:
            with override_settings(AGENTIC_SUPERSET=self.config(**changes)):
                with self.assertRaises(publisher.PublishError):
                    publisher.configuration(SimpleNamespace(pk=12))

    @patch.object(publisher.requests, 'Session')
    def test_http_client_login_csrf_and_no_redirects(self, session_factory):
        session = session_factory.return_value
        session.headers = {}
        session.request.return_value.status_code = 200
        session.request.return_value.content = b'json'
        session.request.return_value.json.side_effect = [{'access_token': 'token'}, {'result': 'csrf'}]
        client = publisher.SupersetClient(self.config())
        client.login()
        self.assertEqual(session.headers['X-CSRFToken'], 'csrf')
        self.assertEqual(session.headers['Authorization'], 'Bearer token')
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
        session.request.return_value.status_code = 302
        with self.assertRaises(publisher.PublishError):
            client.call('GET', 'dataset/1')

    def test_tls_cannot_be_disabled(self):
        with self.assertRaises(publisher.PublishError):
            publisher.SupersetClient(self.config(CA_BUNDLE=False))
