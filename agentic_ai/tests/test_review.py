from unittest.mock import patch

from django.contrib.auth.models import User, Permission, Group
from django.core.cache import cache
from django.test import TestCase

from agentic_ai.models import AgentRunLog, AgentTool
from agentic_ai.mcp_gateway import _authorized_names


class AttemptReviewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('reviewer', is_staff=True)
        self.user.user_permissions.add(Permission.objects.get(codename='view_agentrunlog'))
        self.client.force_login(self.user)
        self.log = AgentRunLog.objects.create(user=self.user, user_query='A new unsupported loan question',
            final_answer='Cannot answer this request', error_message='unsupported_operation', status='failed')

    def test_reviewer_can_inspect_full_failed_attempt_without_approval_permission(self):
        response = self.client.get('/agentic/dashboard/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.log.user_query)
        self.assertContains(response, self.log.final_answer)
        self.assertContains(response, self.log.error_message)
        self.assertNotContains(response, 'Pending Approvals')
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_search_filters_and_pagination_reach_old_attempts(self):
        AgentRunLog.objects.bulk_create([AgentRunLog(user=self.user, user_query=f'Other question {i}',
                                      status='success') for i in range(55)])
        first = self.client.get('/agentic/dashboard/')
        self.assertEqual(len(first.context['logs']), 50)
        self.assertContains(self.client.get('/agentic/dashboard/?page=2'), self.log.user_query)
        filtered = self.client.get('/agentic/dashboard/', {'q': 'unsupported', 'user': 'reviewer', 'status': 'failed'})
        self.assertEqual(filtered.context['page_obj'].paginator.count, 1)
        self.assertContains(filtered, self.log.user_query)
        api = self.client.get('/agentic/api/logs/?status=failed').json()
        self.assertEqual(api['count'], 1)
        self.assertEqual(api['logs'][0]['error'], 'unsupported_operation')

    def test_regular_users_and_unprivileged_staff_cannot_review_others(self):
        for staff in (False, True):
            other = User.objects.create_user(f'user-{staff}', is_staff=staff)
            self.client.force_login(other)
            for path in ('/agentic/dashboard/', '/agentic/api/logs/'):
                response = self.client.get(path)
                self.assertIn(response.status_code, (302, 403))
                self.assertNotContains(response, self.log.user_query, status_code=response.status_code)

    def test_submitted_markup_is_escaped_in_history(self):
        self.log.user_query = '<script>alert(1)</script>'
        self.log.save()
        response = self.client.get('/agentic/dashboard/')
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, self.log.user_query)

    @patch('agentic_ai.api.execute_query', side_effect=RuntimeError('private backend detail'))
    def test_unexpected_execution_failure_is_retained_for_admin(self, execute):
        response = self.client.post('/agentic/api/query/', {'message': 'A completely new loan question'})
        self.assertEqual(response.status_code, 500)
        attempt = AgentRunLog.objects.get(user_query='A completely new loan question')
        self.assertEqual(attempt.status, 'failed')
        self.assertIn('RuntimeError', attempt.error_message)
        self.assertNotIn('private backend detail', response.content.decode())

    def test_group_assignment_grants_only_registered_active_tool_and_revokes(self):
        borrower = User.objects.create_user('loan-user')
        group = Group.objects.create(name='loan_users')
        tool = AgentTool.objects.create(name='loan-due-report', allowed_roles=['loan_users'])
        self.assertEqual(_authorized_names(borrower), set())
        borrower.groups.add(group)
        self.assertEqual(_authorized_names(borrower), {tool.name})
        borrower.groups.remove(group)
        self.assertEqual(_authorized_names(borrower), set())
        borrower.groups.add(group)
        tool.is_active = False
        tool.save()
        self.assertEqual(_authorized_names(borrower), set())
