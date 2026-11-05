from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth.models import User, Group
from chat.models import EmployeeSyncSettings, EmployeeDirectoryEntry
from chat.employee_sync import normalize, sync_employees, INITIAL_PASSWORD


class EmployeeSyncTests(TestCase):
    def setUp(self):
        self.group = Group.objects.create(name='Default')
        self.config = EmployeeSyncSettings.objects.create(enabled=True, url='https://nifi.example/employees', default_group=self.group)
        self.row = dict(EMPLOYEEID='12', EMPLOYEECODE='0012', EMPLOYEENAME='Employee Name', BRANCH='001', EMPSTATUS='Regular')

    def sync(self, rows=None):
        with patch('chat.employee_sync.fetch', return_value=normalize(rows or [self.row])):
            return sync_employees()

    def test_create_login_and_repeat_preserves_password_and_group(self):
        self.sync()
        user = User.objects.get(username='0012')
        self.assertTrue(user.check_password(INITIAL_PASSWORD))
        self.assertTrue(user.is_active)
        self.assertEqual(list(user.groups.all()), [self.group])
        user.set_password('changed-password')
        user.save()
        custom = Group.objects.create(name='Root assigned')
        user.groups.set([custom])
        self.sync()
        user.refresh_from_db()
        self.assertTrue(user.check_password('changed-password'))
        self.assertEqual(list(user.groups.all()), [custom])
        self.assertEqual(User.objects.count(), 1)

    def test_status_disables_and_reactivates(self):
        self.sync()
        self.row['EMPSTATUS'] = 'Resigned'
        self.sync()
        self.assertFalse(User.objects.get(username='0012').is_active)
        self.row['EMPSTATUS'] = 'Regular'
        self.sync()
        self.assertTrue(User.objects.get(username='0012').is_active)

    def test_existing_user_preserved(self):
        user = User.objects.create_user(username='0012', password='existing')
        self.sync()
        user.refresh_from_db()
        self.assertTrue(user.check_password('existing'))
        self.assertEqual(user.groups.count(), 0)

    def test_protected_root(self):
        user = User.objects.create_superuser(username='0012', password='root-password')
        self.row['EMPSTATUS'] = 'Resigned'
        self.sync()
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(EmployeeDirectoryEntry.objects.count(), 0)

    def test_duplicate_or_bad_payload_rejected(self):
        for payload in ([self.row, self.row], [], [{}]):
            with self.assertRaises(ValueError):
                normalize(payload)

    def test_rename_uses_stable_employee_id(self):
        self.sync()
        self.row['EMPLOYEECODE'] = '0013'
        self.sync()
        self.assertEqual(User.objects.get().username, '0013')

    def test_conflict_rolls_back(self):
        self.sync()
        User.objects.create_user(username='taken')
        self.row['EMPLOYEECODE'] = 'taken'
        with self.assertRaises(ValueError):
            self.sync()
        self.assertEqual(EmployeeDirectoryEntry.objects.get().employee_code, '0012')

    def test_admin_permissions(self):
        from django.test import RequestFactory
        from django.contrib import admin
        from chat.admin import EmployeeSyncSettingsAdmin
        model_admin = EmployeeSyncSettingsAdmin(EmployeeSyncSettings, admin.site)
        request = RequestFactory().get('/')
        request.user = User.objects.create_user(username='staff', is_staff=True)
        self.assertFalse(model_admin.has_change_permission(request))
        request.user = User.objects.create_user(username='root_123', is_staff=True)
        self.assertTrue(model_admin.has_change_permission(request))
        self.client.force_login(request.user)
        response = self.client.get('/admin/chat/employeesyncsettings/1/change/')
        self.assertContains(response, 'Sync now using saved settings')

    def test_fetch_uses_get_and_rejects_redirect(self):
        from chat.employee_sync import fetch
        import json
        with patch('chat.employee_sync.requests.Session') as session:
            response = session.return_value.__enter__.return_value.get.return_value.__enter__.return_value
            response.status_code = 200
            response.headers = {'Content-Type': 'application/json'}
            response.iter_content.return_value = [json.dumps([self.row]).encode()]
            self.assertEqual(fetch(self.config)[0]['EMPLOYEECODE'], '0012')
            self.assertFalse(session.return_value.__enter__.return_value.get.call_args.kwargs['allow_redirects'])
            response.status_code = 302
            with self.assertRaises(ValueError):
                fetch(self.config)

    def test_nifi_location_name_and_site_code_are_separate(self):
        self.row.update(BRANCH='BURO Training Center, Khulna', SITECODE='03',
                        DESIGNATIONNAME='Guard', GENDER='Male', EMP_WORKSPACE='Branch')
        self.sync()
        entry = EmployeeDirectoryEntry.objects.get()
        self.assertEqual(entry.branch, 'BURO Training Center, Khulna')
        self.assertEqual(entry.site_code, '03')
        self.assertEqual(entry.designation, 'Guard')
        self.assertEqual(entry.gender, 'Male')
        self.assertEqual(entry.workspace, 'Branch')
        self.assertEqual(entry.user.profile.branch_code, '03')

    def test_zone_location_is_not_a_branch_or_head_office_grant(self):
        self.row.update(BRANCH='Feni Zone', SITECODE='Z32', EMP_WORKSPACE='Zone')
        self.sync()
        entry = EmployeeDirectoryEntry.objects.get()
        self.assertEqual(entry.site_code, 'Z32')
        self.assertEqual(entry.user.profile.branch_code, '')
        self.assertFalse(entry.user.profile.is_head_office)
        self.assertEqual(entry.user.profile.allowed_branch_codes, '')

    def test_new_feed_repairs_old_branch_name_and_preserves_groups(self):
        self.row['BRANCH'] = 'Jinarpur Branch'
        self.sync()
        custom = Group.objects.create(name='Root assigned')
        user = User.objects.get()
        user.groups.set([custom])
        self.row.update(SITECODE='888', EMP_WORKSPACE='Branch', DESIGNATIONNAME='Assistant Programme Organiser')
        self.sync()
        user.refresh_from_db()
        self.assertEqual(user.profile.branch_code, '888')
        self.assertEqual(list(user.groups.all()), [custom])
        self.assertEqual(User.objects.count(), 1)

    def test_long_branch_name_supported_with_site_code(self):
        self.row.update(BRANCH='A long employee office name exceeding thirty two characters', SITECODE='050', EMP_WORKSPACE='Branch')
        self.sync()
        self.assertEqual(User.objects.get().profile.branch_code, '050')

    def test_invalid_optional_data_rejected(self):
        self.row['SITECODE'] = {'invalid': 'code'}
        with self.assertRaises(ValueError):
            normalize([self.row])
