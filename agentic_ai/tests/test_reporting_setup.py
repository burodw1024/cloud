from unittest.mock import patch
from django.contrib.auth.models import User, Permission
from django.test import TestCase, override_settings
from django.core.cache import cache
from agentic_ai.models import ReportingConnection, LoanAccessEvent
from agentic_ai.reporting_config import ReportingForm, save, current, initial, workspace_settings
from agentic_ai.report_catalog import satellite_tiles
from agentic_ai.superset_publisher import specification, PublishError, configuration
from .test_dashboard_studio import REPORT


class ReportingSetupTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin=User.objects.create_superuser('setup',password=None)
        self.client.force_login(self.admin)

    def values(self,**changes):
        values={'service_url':'https://superset.example.com','public_url':'https://reports.example.com',
            'username':'publisher','password':'service-secret','database_id':3,'database_host':'analytics.internal',
            'database_port':5432,'database_name':'analytics','database_user':'writer','database_password':'database-secret',
            'database_sslmode':'verify-full','schema':'agentic','user_mappings':'24123=27',
            'private_access_verified':True,'default_template':'comparison','version':'new'}
        values.update(changes)
        return values

    def persist(self,**changes):
        form=ReportingForm(self.values(**changes))
        self.assertTrue(form.is_valid(),form.errors)
        save(self.admin,form.cleaned_data)

    def test_save_encrypts_passwords_and_public_form_never_reveals_them(self):
        self.persist()
        record=ReportingConnection.objects.get()
        self.assertNotIn('secret',str(record.configuration))
        self.assertNotIn('service-secret',record.encrypted_credentials)
        self.assertEqual(current()['PASSWORD'],'service-secret')
        self.assertEqual(current()['SNAPSHOT_DB']['password'],'database-secret')
        page=self.client.get('/agentic/reporting-settings/')
        self.assertEqual(page.status_code,200)
        self.assertNotContains(page,'service-secret')
        self.assertNotContains(page,'database-secret')
        self.assertEqual(page['Cache-Control'],'no-store')
        self.assertEqual(LoanAccessEvent.objects.get().action,'reporting_configured')

    def test_blank_passwords_retain_stale_edits_and_new_destinations_are_rejected(self):
        self.persist()
        version=initial()['version']
        self.persist(password='',database_password='',version=version)
        self.assertEqual(current()['PASSWORD'],'service-secret')
        form=ReportingForm(self.values(password='',database_password='',service_url='https://another.example.com',version=initial()['version']))
        self.assertTrue(form.is_valid())
        with self.assertRaises(ValueError):save(self.admin,form.cleaned_data)
        stale=ReportingForm(self.values(version='new'));self.assertTrue(stale.is_valid())
        with self.assertRaises(ValueError):save(self.admin,stale.cleaned_data)

    def test_mapping_duplicates_and_insecure_origins_are_rejected(self):
        for change in [{'user_mappings':'24123=27\n24124=27'}, {'service_url':'http://reports.example.com'},
                       {'service_url':'https://user:secret@example.com'}, {'schema':'schema;DROP TABLE x'}]:
            self.assertFalse(ReportingForm(self.values(**change)).is_valid(),change)

    def test_permission_csrf_and_save_route(self):
        ordinary=User.objects.create_user('ordinary',is_staff=True)
        self.client.force_login(ordinary)
        self.assertEqual(self.client.get('/agentic/reporting-settings/').status_code,403)
        self.client.force_login(self.admin)
        response=self.client.post('/agentic/reporting-settings/',{'action':'save',**self.values()})
        self.assertEqual(response.status_code,302,response.context and response.context['form'].errors)
        self.assertTrue(ReportingConnection.objects.exists())

    def test_audit_failure_rolls_back_configuration(self):
        with patch('agentic_ai.access_admin.record_event',side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):self.persist()
        self.assertFalse(ReportingConnection.objects.exists())

    def test_current_user_mapping_is_resolved_without_manual_django_primary_key(self):
        self.persist()
        user=User.objects.create_user('24123')
        config=configuration(user)
        self.assertEqual(config['OWNER_ID'],27)
        self.assertEqual(config['PUBLIC_URL'],'https://reports.example.com')
        self.assertEqual(config['SNAPSHOT_DB']['sslmode'],'verify-full')

    def test_fixed_templates_preserve_values_and_table_never_adds_charts(self):
        table=specification(REPORT,'Report',template='table')
        self.assertEqual(table['metrics'],[])
        self.assertEqual(table['charts'],['Source table'])
        for choice in ['comparison','management']:
            spec=specification(REPORT,'Report',template=choice)
            self.assertEqual(spec['template'],choice)
            self.assertNotIn('SUM',str(spec))
        with self.assertRaises(PublishError):specification(REPORT,'Report',template='arbitrary-sql')

    @override_settings(AGENTIC_SATELLITE_TILE_URL='https://tiles.internal/{z}/{x}/{y}.png',
                       AGENTIC_SATELLITE_TILE_ATTRIBUTION='Deployment imagery')
    def test_saved_map_background_overrides_deployment_and_preserves_other_settings(self):
        self.persist(map_background='custom', map_tile_url='/tiles/{z}/{x}/{y}.jpg',
                     map_tile_attribution='Licensed imagery')
        self.assertEqual(satellite_tiles(current()), {
            'tile_url': '/tiles/{z}/{x}/{y}.jpg', 'tile_attribution': 'Licensed imagery',
        })
        self.assertTrue(workspace_settings(self.admin)['map_imagery_configured'])
        self.assertContains(self.client.get('/agentic/reporting-settings/'), '/tiles/{z}/{x}/{y}.jpg')
        self.persist(password='', database_password='', version=initial()['version'])
        self.assertEqual(initial()['map_background'], 'custom')
        self.persist(map_background='grid', password='', database_password='', version=initial()['version'])
        self.assertEqual(satellite_tiles(current()), {})
        self.assertNotIn('MAP_TILE_URL', current())
        self.assertEqual(current()['PASSWORD'], 'service-secret')
        self.persist(map_background='deployment', version=initial()['version'])
        self.assertEqual(satellite_tiles(current())['tile_attribution'], 'Deployment imagery')

    def test_browser_tile_urls_reject_embedded_credentials_and_mixed_content(self):
        for url in ('//tiles.internal/{z}/{x}/{y}', 'https://user:pass@tiles.internal/{z}/{x}/{y}',
                    'https://tiles.internal/{z}/{x}/{y}?key=secret',
                    'http://tiles.internal/{z}/{x}/{y}', '/tiles/{z}/{x}', '/\\tiles/{z}/{x}/{y}'):
            with self.subTest(url=url):
                form = ReportingForm(self.values(map_background='custom', map_tile_url=url,
                                                 map_tile_attribution='Provider'))
                self.assertFalse(form.is_valid())
                self.assertIn('map_tile_url', form.errors)

    def test_direct_postgres_staging_binds_values_and_uses_generated_identifiers(self):
        from unittest.mock import MagicMock
        from agentic_ai.models import DashboardExport
        from agentic_ai.superset_publisher import stage_postgres
        report={**REPORT,'rows':[{'BRANCHCODE':"0007'; DROP TABLE users;--",'GS_BALANCE':'12','CS_BALANCE':None}]}
        export=DashboardExport.objects.create(user=self.admin,title='Report',specification=specification(report,'Report'))
        with patch('psycopg.connect') as connect:
            connection=connect.return_value.__enter__.return_value
            stage_postgres(export,report,{'SNAPSHOT_DB':{'host':'analytics.internal'},'SCHEMA':'agentic'})
            cursor=connection.cursor.return_value.__enter__.return_value
            sql,values=cursor.executemany.call_args.args
            self.assertNotIn('DROP TABLE',sql.as_string())
            self.assertEqual(values[0][2],"0007'; DROP TABLE users;--")
            self.assertIn('%s',sql.as_string())

    @patch('agentic_ai.reporting_admin.SupersetClient')
    @patch('psycopg.connect')
    def test_saved_connection_test_is_read_only(self,connect,client):
        self.persist()
        cursor=connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value=('analytics',True)
        result=self.client.post('/agentic/reporting-settings/',{'action':'check'})
        self.assertContains(result,'checks passed')
        client.return_value.login.assert_called_once()
        self.assertEqual(client.return_value.call.call_args.args,('GET','database/3'))
        self.assertTrue(cursor.execute.call_args.args[0].startswith('SELECT '))

    def test_disabling_service_does_not_reuse_old_password_for_a_later_destination(self):
        self.persist()
        self.persist(service_url='',public_url='',password='',database_password='',version=initial()['version'])
        self.assertEqual(current()['PASSWORD'],'')
        self.persist(service_url='https://new.example.com',public_url='https://new.example.com',password='',database_password='',version=initial()['version'])
        self.assertEqual(current()['PASSWORD'],'')

    def test_reentered_passwords_recover_after_encryption_key_change(self):
        from django.test import override_settings
        self.persist()
        with override_settings(SECRET_KEY='new-test-key',SECRET_KEY_FALLBACKS=[]):
            self.persist(version=initial()['version'])
            self.assertEqual(current()['PASSWORD'],'service-secret')
