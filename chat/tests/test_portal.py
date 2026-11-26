from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from chat.models import EmployeeDirectoryEntry, UserProfile


class PortalTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_prelogin_pages_keep_sign_in_and_return_to_workspace(self):
        User.objects.create_user('employee', password='test-password')
        client = Client(enforce_csrf_checks=True)
        portal = client.get(reverse('portal'))
        self.assertContains(portal, 'Less routine work.')
        self.assertContains(portal, 'insight-login.webp')

        login = reverse('login')
        workspace = reverse('agentic_ai:query_page')
        response = client.get(login, {'next': workspace})
        self.assertContains(response, 'for="id_username"')
        self.assertContains(response, f'name="next" value="{workspace}"')
        credentials = {'username': 'employee', 'password': 'test-password', 'next': workspace}
        self.assertEqual(client.post(login, credentials).status_code, 403)
        credentials['csrfmiddlewaretoken'] = client.cookies['csrftoken'].value
        self.assertRedirects(client.post(login, credentials), workspace, fetch_redirect_response=False)
        self.assertNotContains(client.get(reverse('portal')), 'id="portal-hero-title"')

    def test_account_menu_and_csrf_protected_logout(self):
        user = User.objects.create_user('employee', first_name='Django', last_name='Name')
        EmployeeDirectoryEntry.objects.create(
            user=user, employee_id='1', employee_code='1001', employee_name='Directory Name',
            designation='Branch Manager', status='Regular',
        )
        UserProfile.objects.update_or_create(user=user, defaults={'mobile_number': '01700123456'})
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        for route in ('portal', 'index', 'agentic_ai:query_page'):
            response = client.get(reverse(route))
            for value in ('Directory Name', 'Branch Manager', '01700123456', 'action="/logout/"'):
                self.assertContains(response, value)
            self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(client.get(reverse('logout')).status_code, 405)
        self.assertEqual(client.post(reverse('logout')).status_code, 403)
        csrf_token = client.cookies['csrftoken'].value
        self.assertRedirects(client.post(reverse('logout'), {'csrfmiddlewaretoken': csrf_token}), '/')
        self.assertNotContains(client.get(reverse('portal')), '01700123456')

    @patch('chat.views_settings.get_available_ollama_models', return_value=[])
    def test_admin_link_settings_validate_and_refresh_portal(self, _models):
        settings_url = reverse('admin_tools_settings')
        portal_url = reverse('portal')
        user = User.objects.create_user('viewer')
        self.client.force_login(user)
        self.assertEqual(self.client.get(settings_url).status_code, 403)
        self.assertNotContains(self.client.get(portal_url), 'http://gitea:3000')
        root = User.objects.create_superuser('root_123')
        self.client.force_login(root)
        self.assertContains(self.client.get(settings_url), 'ContextForge URL')
        self.assertRedirects(self.client.post(settings_url, {'PORTAL_GITEA_URL': 'http://gitea:3000'}), settings_url)
        self.assertContains(self.client.get(portal_url), 'href="http://gitea:3000"')
        self.assertRedirects(self.client.post(settings_url, {'PORTAL_GITEA_URL': 'javascript:alert(1)'}), settings_url)
        self.assertContains(self.client.get(portal_url), 'href="http://gitea:3000"')
        self.assertRedirects(self.client.post(settings_url, {'PORTAL_GITEA_URL': ''}), settings_url)
        self.assertNotContains(self.client.get(portal_url), 'http://gitea:3000')

    def test_policy_and_messaging_grants_follow_selected_user(self):
        user = User.objects.create_user('viewer')
        root = User.objects.create_superuser('root')
        grants = [f'share_report_{channel}' for channel in ('whatsapp', 'gmail', 'outlook', 'viber', 'sms')]
        self.client.force_login(user)
        self.assertNotContains(self.client.get(reverse('portal')), 'href="/policy-ai/"')
        self.assertEqual(self.client.get(reverse('openai_policy:chat')).status_code, 403)

        self.client.force_login(root)
        response = self.client.post(reverse('agentic_ai:service_access'), {
            'kind': 'user', 'name': user.username, 'permissions': ['use_policy_ai', *grants],
            'reason': 'Approved assistant and messaging access',
        })
        self.assertEqual(response.status_code, 302)
        employee = User.objects.get(pk=user.pk)
        self.assertTrue(employee.has_perms(f'agentic_ai.{grant}' for grant in grants))

        self.client.force_login(user)
        self.assertContains(self.client.get(reverse('portal')), 'href="/policy-ai/"')
        self.assertContains(self.client.get(reverse('openai_policy:chat')), 'Log out')
        self.assertContains(self.client.get(reverse('portal_docs')), 'Documentation')

        self.client.force_login(root)
        response = self.client.post(reverse('agentic_ai:service_access'), {
            'kind': 'user', 'name': user.username, 'permissions': [],
            'reason': 'Employee changed roles',
        })
        self.assertEqual(response.status_code, 302)
        employee = User.objects.get(pk=user.pk)
        self.assertFalse(any(employee.has_perm(f'agentic_ai.{grant}') for grant in grants))
        self.assertFalse(employee.has_perm('openai_policy.use_policy_ai'))
