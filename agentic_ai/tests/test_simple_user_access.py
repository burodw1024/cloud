from unittest.mock import AsyncMock, patch

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from mcp.types import Tool

from agentic_ai.models import AgentTool, LoanAccessEvent, LoanAccessPolicy, UserToolAccess
from agentic_ai.permissions import has_tool_permission
from .test_loan_access import AccessAdministrationTests
from .test_setup import GuidedSetupTests
from .test_native_loan_query import SCHEMA


class SimpleUserAccessTests(TestCase):
    setUp = AccessAdministrationTests.setUp
    data = AccessAdministrationTests.data

    def test_one_save_grants_tools_and_branch_without_groups_and_audits_both(self):
        tool = AgentTool.objects.create(name='loan-due-report', loan_access_mode='UI', allowed_roles=['loan_users'])
        data = self.data(tool_access='SELECTED', tools=[tool.pk], tool_version='new')
        response = self.client.post('/agentic/data-access/', data)
        self.assertEqual(response.status_code, 302, response.context and response.context['form'].errors)
        self.assertFalse(self.user.groups.exists())
        self.assertTrue(has_tool_permission(self.user, {'name': tool.name, 'allowed_roles': ['loan_users']}))
        self.assertEqual(LoanAccessPolicy.objects.get(user=self.user).branches.get(), self.branch)
        self.assertEqual(LoanAccessEvent.objects.get().after['tool_access']['tools'], [tool.name])
        access = UserToolAccess.objects.get(user=self.user)
        policy = LoanAccessPolicy.objects.get(user=self.user)
        response = self.client.post('/agentic/data-access/', self.data(
            tool_access='SELECTED', tools=[], tool_version=access.updated_at.isoformat(),
            version=policy.updated_at.isoformat()))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(has_tool_permission(self.user, {'name': tool.name}))

    def test_stale_tool_selection_cannot_overwrite_and_audit_failure_rolls_back(self):
        tool = AgentTool.objects.create(name='loan-due-report')
        access = UserToolAccess.objects.create(user=self.user)
        response = self.client.post('/agentic/data-access/', self.data(
            tool_access='SELECTED', tools=[tool.pk], tool_version='new'))
        self.assertContains(response, 'changed in another session')
        self.assertFalse(LoanAccessPolicy.objects.exists())
        with patch('agentic_ai.access_admin.record_event', side_effect=RuntimeError('audit down')):
            with self.assertRaises(RuntimeError):
                self.client.post('/agentic/data-access/', self.data(
                    tool_access='SELECTED', tools=[tool.pk], tool_version=access.updated_at.isoformat()))
        self.assertFalse(access.tools.exists())
        self.assertFalse(LoanAccessPolicy.objects.exists())

    def test_scope_only_admin_cannot_change_tools_using_forged_fields(self):
        staff = User.objects.create_user('scope-admin', is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename='manage_loan_access'))
        self.client.force_login(staff)
        tool = AgentTool.objects.create(name='loan-due-report')
        response = self.client.post('/agentic/data-access/', self.data(
            tool_access='SELECTED', tools=[tool.pk], tool_version='new'))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(UserToolAccess.objects.exists())

    def test_application_only_page_does_not_claim_signing_is_required(self):
        AgentTool.objects.create(name='loan-due-report', loan_access_mode='UI')
        response = self.client.get(f'/agentic/data-access/?user={self.user.pk}')
        self.assertContains(response, 'Allowed tools by module')
        self.assertNotContains(response, 'Some tools use legacy signed access')


class ApplicationSetupTests(TestCase):
    setUp = GuidedSetupTests.setUp
    post = GuidedSetupTests.post
    tool = GuidedSetupTests.tool

    @patch('agentic_ai.setup_views._save_settings')
    def test_user_access_modes_save_and_survive_rediscovery(self, save):
        native = Tool(name='loan-due-report', inputSchema=SCHEMA)
        with patch('agentic_ai.setup_views.discover_gateway', new=AsyncMock(return_value=[native])):
            for mode in ('UI', 'APP'):
                self.post('discover'); self.post('check_model')
                response = self.post('save_tools', tools=[dict(name=native.name, enabled=True,
                    roles=['manager'], requires_approval=False, requires_loan_scope=False,
                    loan_access_mode=mode, fixed_branch_codes=[])])
                self.assertEqual(response.status_code, 200, response.content)
                record = AgentTool.objects.get(name=native.name)
                self.assertEqual(record.loan_access_mode, mode)
                self.assertTrue(record.requires_loan_scope)
                self.assertEqual(self.post('discover').json()['tools'][0]['loan_access_mode'], mode)

    def test_incompatible_schema_is_rejected_without_changing_existing_mode(self):
        self.post('discover'); self.post('check_model')
        response = self.post('save_tools', tools=[self.tool(loan_access_mode='UI', fixed_branch_codes=[])])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AgentTool.objects.exists())
