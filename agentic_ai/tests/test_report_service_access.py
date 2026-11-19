from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.urls import reverse

from agentic_ai.models import LoanAccessEvent


class ReportServiceAccessTests(TestCase):
    def test_superuser_grants_group_and_direct_user_without_overwriting_other_permissions(self):
        root = User.objects.create_superuser('service-root')
        user = User.objects.create_user('report-user', is_staff=True)
        group = Group.objects.create(name='Report operators')
        user.groups.add(group)
        unrelated = Permission.objects.get(codename='view_agentrunlog')
        group.permissions.add(unrelated)
        url = reverse('agentic_ai:service_access')
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(root)
        response = self.client.post(url, {'kind': 'group', 'name': group.name,
            'permissions': ['publish_dashboard', 'share_report_gmail'], 'reason': 'Approved reporting role'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(group.permissions.filter(pk=unrelated.pk).exists())
        response = self.client.get(url, {'kind': 'user', 'name': user.username})
        self.assertContains(response, 'Inherited from a role')
        response = self.client.post(url, {'kind': 'user', 'name': user.username,
            'permissions': ['export_powerbi_snapshot'], 'reason': 'Approved workbook access'})
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(pk=user.pk)
        self.assertTrue(user.has_perm('agentic_ai.share_report_gmail'))
        self.assertFalse(user.has_perm('agentic_ai.share_report_viber'))
        self.assertTrue(user.has_perm('agentic_ai.export_powerbi_snapshot'))
        self.assertEqual(LoanAccessEvent.objects.filter(action='report_service_access').count(), 2)

    def test_user_access_shows_and_saves_selected_employee_service_grants(self):
        root = User.objects.create_superuser('inline-root')
        employee = User.objects.create_user('inline-employee')
        delegated = User.objects.create_user('inline-delegate', is_staff=True)
        delegated.user_permissions.add(Permission.objects.get(codename='manage_loan_access'))
        editor = reverse('agentic_ai:data_access') + f'?user={employee.pk}'
        self.client.force_login(delegated)
        self.assertNotContains(self.client.get(editor), 'Save report service grants')
        self.client.force_login(root)
        self.assertContains(self.client.get(editor), 'Report services for inline-employee')
        self.assertContains(self.client.get(editor), 'Send through Viber')
        reply = self.client.post(reverse('agentic_ai:service_access'), {
            'kind': 'user', 'name': employee.username, 'return_to': 'user_access',
            'permissions': ['share_report_whatsapp', 'share_report_viber'],
            'reason': 'Approved delivery channels',
        })
        self.assertRedirects(reply, editor + '&services_saved=1#service-grants')
        employee.refresh_from_db()
        self.assertTrue(employee.has_perm('agentic_ai.share_report_viber'))
        self.assertFalse(employee.has_perm('agentic_ai.share_report_gmail'))
        self.assertFalse(hasattr(employee, 'loan_access_policy'))
