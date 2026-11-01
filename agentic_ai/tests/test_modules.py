from unittest.mock import patch

from django.contrib.auth.models import User, Group, Permission
from django.core.cache import cache
from django.test import TestCase, Client

from agentic_ai.api import _can_replay_result
from agentic_ai.mcp_gateway import _authorized_names, _decision
from agentic_ai.models import BusinessModule, AgentTool
from agentic_ai.module_admin import MODULE_PERMISSIONS
from agentic_ai.workspace import workspace_context


class BusinessModuleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user('module-admin', is_staff=True)
        for name in MODULE_PERMISSIONS:
            self.admin.user_permissions.add(Permission.objects.get(codename=name.split('.')[1]))
        self.user = User.objects.create_user('loan-user')
        self.user.groups.add(Group.objects.create(name='loan_users'))
        self.tool = AgentTool.objects.create(name='loan-due-report', display_name='Loan report',
                                            allowed_roles=['loan_users'], requires_approval=True)
        self.client.force_login(self.admin)

    def data(self, **overrides):
        return dict(name='Loan reports', slug='loan-reports', description='Explore your loan data.',
                    starter_questions='Show my loan report.\nShow overdue members.',
                    sort_order='1', is_active='on', tools=[self.tool.pk], **overrides)

    def test_create_and_edit_module_preserves_tool_roles_and_approval(self):
        response = self.client.post('/agentic/modules/', self.data())
        self.assertEqual(response.status_code, 302)
        module = BusinessModule.objects.get(slug='loan-reports')
        self.tool.refresh_from_db()
        self.assertEqual(self.tool.module_id, module.pk)
        self.assertEqual(self.tool.allowed_roles, ['loan_users'])
        self.assertTrue(self.tool.requires_approval)
        response = self.client.get(response.url)
        self.assertContains(response, 'Module saved')
        self.assertNotContains(response, 'id="gateway-token"')
        data = self.data()
        data.update(module_id=module.pk, name='Loan insights', tools=[])
        self.assertEqual(self.client.post('/agentic/modules/', data).status_code, 302)
        self.tool.refresh_from_db()
        self.assertIsNone(self.tool.module_id)

    def test_only_authorized_published_modules_appear_and_pause_blocks_execution_and_replay(self):
        module = BusinessModule.objects.create(name='Loan reports', slug='loans', is_active=True,
                                               starter_questions='My question')
        self.tool.module = module
        self.tool.save()
        BusinessModule.objects.create(name='PRIVATE HR', slug='private-hr', is_active=True)
        with patch('agentic_ai.workspace.gateway_enabled', return_value=True):
            context = workspace_context(self.user, 'loans')
            self.assertEqual(context['business_modules'][0]['name'], 'Loan reports')
            self.assertEqual(context['capability_count'], 1)
            self.assertNotIn('PRIVATE HR', str(context))
            module.is_active = False
            module.save()
            self.assertEqual(workspace_context(self.user)['business_modules'], [])
        self.assertNotIn(self.tool.name, _authorized_names(self.user))
        self.assertEqual(_decision(self.user, self.tool.name, {})['code'], 'permission_denied')
        self.assertFalse(_can_replay_result(self.user, {'tool': self.tool.name,
                                                       'plan': {'intent': 'mcp_tool_call'}}))

    @patch('agentic_ai.workspace.gateway_enabled', return_value=True)
    def test_end_user_page_hides_admin_configuration_and_shows_dynamic_module(self, enabled):
        module = BusinessModule.objects.create(name='Loan reports', slug='loans', is_active=True,
                                               starter_questions='My editable question')
        self.tool.module = module
        self.tool.save()
        self.client.force_login(self.user)
        response = self.client.get('/agentic/?module=loans')
        self.assertContains(response, 'Loan reports')
        self.assertContains(response, 'data-module-question')
        self.assertContains(response, 'My editable question')
        for text in ('Administration', 'Integration settings', 'Business modules',
                     'id="gateway-token"', 'Check model', 'id="whatsapp-recipients"',
                     'WhatsApp tool has not been discovered'):
            self.assertNotContains(response, text)
        self.assertEqual(self.client.get('/agentic/modules/').status_code, 302)
        self.assertEqual(self.client.get('/agentic/setup/').status_code, 302)

    def test_staff_without_module_permissions_is_denied_and_nonstaff_permissions_do_not_bypass_staff_check(self):
        person = User.objects.create_user('plain-staff', is_staff=True)
        self.client.force_login(person)
        self.assertEqual(self.client.get('/agentic/modules/').status_code, 403)
        self.admin.is_staff = False
        self.admin.save()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/agentic/modules/').status_code, 302)

    def test_module_admin_can_manage_modules_without_connection_permissions(self):
        self.assertEqual(self.client.get('/agentic/modules/').status_code, 200)
        self.assertEqual(self.client.get('/agentic/setup/').status_code, 403)

    def test_csrf_required_and_tool_cannot_be_stolen_from_another_module(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post('/agentic/modules/', self.data()).status_code, 403)
        other = BusinessModule.objects.create(name='Other module', slug='other')
        self.tool.module = other
        self.tool.save()
        response = self.client.post('/agentic/modules/', self.data())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        self.assertFalse(BusinessModule.objects.filter(slug='loan-reports').exists())

    def test_invalid_duplicate_module_and_unsafe_starter_markup(self):
        BusinessModule.objects.create(name='Existing', slug='loan-reports')
        response = self.client.post('/agentic/modules/', self.data())
        self.assertTrue(response.context['form'].errors)
        data = self.data()
        data.update(slug='safe', name='<script>bad()</script>', starter_questions='<img src=x onerror=bad()>')
        self.assertEqual(self.client.post('/agentic/modules/', data).status_code, 302)
        page = self.client.get('/agentic/modules/')
        self.assertNotContains(page, '<script>bad()</script>')
        self.assertContains(page, '&lt;script&gt;')
