import json
from unittest.mock import patch
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.core.cache import cache
from agentic_ai.models import AgentTool, AgentRunLog, AgentApprovalRequest
from agentic_ai.workspace import workspace_context
from . import test_setup


class WorkspaceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('workspace-user')
        self.user.groups.add(Group.objects.create(name='staff'))
        self.tool = AgentTool.objects.create(name='document-search', display_name='Knowledge search',
            category='Knowledge', example_question='Find the onboarding guide.',
            description='Search approved documents.', allowed_roles=['staff'])

    @patch('agentic_ai.workspace.gateway_enabled', return_value=True)
    def test_only_authorized_registered_capabilities_are_visible(self, enabled):
        AgentTool.objects.create(name='secret-tool', description='SECRET DESCRIPTION', allowed_roles=['finance'])
        AgentTool.objects.create(name='disabled-tool', allowed_roles=['staff'], is_active=False)
        AgentTool.objects.create(name='unassigned-tool', allowed_roles=[])
        context = workspace_context(self.user)
        self.assertEqual(context['capability_count'], 1)
        self.assertEqual(context['capabilities'][0]['title'], 'Knowledge search')
        self.assertEqual(context['capabilities'][0]['category'], 'Knowledge')
        self.assertNotIn('SECRET', str(context))
        self.user.is_active = False
        self.assertEqual(workspace_context(self.user)['capabilities'], [])

    @patch('agentic_ai.workspace.gateway_enabled', return_value=True)
    def test_personal_panels_do_not_expose_another_users_history_or_payloads(self, enabled):
        other = User.objects.create_user('other-user')
        AgentRunLog.objects.create(user=other, user_query='SECRET QUESTION', final_answer='SECRET ANSWER')
        AgentApprovalRequest.objects.create(user=other, tool_name='SECRET APPROVAL', action_type='test')
        AgentRunLog.objects.create(user=self.user, user_query='My request', final_answer='PRIVATE REPORT VALUE')
        AgentApprovalRequest.objects.create(user=self.user, tool_name=self.tool.name, action_type='test', request_payload={'secret':'PRIVATE PAYLOAD'})
        context = workspace_context(self.user)
        self.assertEqual(len(context['recent_activity']), 1)
        self.assertEqual(context['pending_approval_count'], 1)
        for private in ('SECRET', 'PRIVATE REPORT VALUE', 'PRIVATE PAYLOAD'):
            self.assertNotIn(private, str(context))

    @patch('agentic_ai.workspace.gateway_enabled', return_value=True)
    def test_large_catalog_is_data_driven_and_role_scoped(self, enabled):
        AgentTool.objects.bulk_create([AgentTool(name=f'capability-{i}', category=f'Department {i % 8}',
            allowed_roles=['staff'], display_name=f'Service {i}') for i in range(120)])
        self.client.force_login(self.user)
        response = self.client.get('/agentic/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['capability_count'], 121)
        self.assertContains(response, 'Search capabilities')
        self.assertContains(response, 'My recent activity')
        self.assertContains(response, 'My pending approvals')

    @patch('agentic_ai.workspace.gateway_enabled', return_value=True)
    def test_catalog_serialization_does_not_execute_admin_supplied_markup(self, enabled):
        self.tool.display_name = '</script><img src=x onerror=alert(1)>'
        self.tool.save()
        self.client.force_login(self.user)
        response = self.client.get('/agentic/')
        self.assertNotContains(response, '</script><img src=x onerror=alert(1)>')
        self.assertContains(response, '\\u003C/script\\u003E')


class WorkspaceSetupTests(TestCase):
    setUp = test_setup.GuidedSetupTests.setUp
    post = test_setup.GuidedSetupTests.post
    tool = test_setup.GuidedSetupTests.tool
    # Exercise metadata persistence through the real permission-protected setup endpoint.
    @patch("agentic_ai.setup_views._save_settings")
    def test_workspace_metadata_is_saved_and_preserved_when_older_clients_omit_it(self, save):
        self.post('discover')
        self.post('check_model')
        item=self.tool(display_name='Aging report', category='Finance', example_question='Show my branch report.')
        self.assertEqual(self.post('save_tools', tools=[item]).status_code, 200)
        record=AgentTool.objects.get()
        self.assertEqual((record.display_name, record.category, record.example_question),
                         ('Aging report', 'Finance', 'Show my branch report.'))
        self.assertEqual(self.post('save_tools', tools=[self.tool()]).status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.category, 'Finance')

    def test_invalid_metadata_does_not_modify_roles_or_activation(self):
        self.post('discover')
        self.post('check_model')
        response=self.post('save_tools', tools=[self.tool(category='x'*81)])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AgentTool.objects.exists())
