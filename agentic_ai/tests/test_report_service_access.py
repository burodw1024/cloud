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
