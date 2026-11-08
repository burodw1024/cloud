from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth.models import Group, User
from django.test import TestCase, Client
from django.utils import timezone

from agentic_ai.api import _can_replay_result
from agentic_ai.loan_access import snapshot
from agentic_ai.mcp_gateway import _allowed, _decision
from agentic_ai.models import AgentTool, BusinessModule, LoanAccessEvent, LoanAccessPolicy, LoanBranch, LoanZone, UserToolAccess
from agentic_ai.permissions import has_tool_permission
from agentic_ai.user_access import version

URL = '/agentic/user-access/'


class UserAccessConsoleTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser('root', password='test')
        self.user = User.objects.create_user('rahim')
        group = Group.objects.create(name='branch_staff')
        self.user.groups.add(group)
        self.tool = AgentTool.objects.create(name='report', display_name='Loan report', allowed_roles=['branch_staff'], requires_loan_scope=True)
        self.other = AgentTool.objects.create(name='policy_tool', allowed_roles=['branch_staff'])
        self.zone = LoanZone.objects.create(code='01', name='Zone one')
        self.branch = LoanBranch.objects.create(code='0012', name='Example branch', zone=self.zone)
        self.client.force_login(self.root)

    def payload(self, **kw):
        return {'user': self.user.pk, 'tools': [self.tool.pk], 'mode': 'SELECTED',
                'branches': [self.branch.pk], 'version': version(self.user),
                'reason': 'Branch reporting responsibility', 'action': 'save', **kw}

    def test_only_root_can_open_or_save_and_csrf_is_required(self):
        staff = User.objects.create_user('staff', is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(self.client.post(URL, self.payload()).status_code, 403)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.root)
        self.assertEqual(client.post(URL, self.payload()).status_code, 403)
        self.assertFalse(UserToolAccess.objects.exists())

    def test_save_explicit_tools_and_branch_atomically_overrides_group_access(self):
        self.assertTrue(_allowed(self.other, self.user))
        response = self.client.post(URL, self.payload())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(_allowed(self.tool, self.user))
        self.assertFalse(_allowed(self.other, self.user))
        self.assertEqual(snapshot(self.user)['directory'][0]['code'], '0012')
        self.assertEqual(LoanAccessEvent.objects.get().action, 'user_access_saved')
        self.assertFalse(has_tool_permission(self.user, {'name': 'policy_tool', 'allowed_roles': ['branch_staff']}))
        self.assertFalse(_can_replay_result(self.user, {'tool': 'policy_tool', 'plan': {'intent': 'mcp_tool_call'}}))
        self.assertEqual(_decision(self.user, 'policy_tool', {})['code'], 'permission_denied')

    def test_preview_does_not_save(self):
        response = self.client.post(URL, self.payload(action='preview', reason=''))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '0012')
        self.assertContains(response, 'Branch restricted')
        self.assertFalse(UserToolAccess.objects.exists())
        self.assertFalse(LoanAccessEvent.objects.exists())

    def test_missing_financial_scope_invalid_tool_and_stale_save_are_rejected(self):
        for data in [self.payload(mode='NONE'), self.payload(tools=['99999']), self.payload(version='old')]:
            self.assertEqual(self.client.post(URL, data).status_code, 200)
            self.assertFalse(UserToolAccess.objects.exists())

    def test_revoke_ignores_old_selections_but_requires_reason_and_version(self):
        self.client.post(URL, self.payload())
        old = self.payload(action='revoke', reason='')
        self.client.post(URL, old)
        self.assertTrue(_allowed(self.tool, self.user))
        self.tool.is_active = False
        self.tool.save()
        self.assertEqual(self.client.post(URL, self.payload(action='revoke')).status_code, 302)
        self.assertFalse(UserToolAccess.objects.get(user=self.user).tools.exists())
        self.assertEqual(LoanAccessPolicy.objects.get(user=self.user).mode, 'NONE')
        self.assertFalse(_allowed(self.other, self.user))

    def test_expiry_and_paused_modules_block_individual_grants(self):
        policy = UserToolAccess.objects.create(user=self.user)
        policy.tools.add(self.other)
        self.assertTrue(_allowed(self.other, self.user))
        policy.expires_at = timezone.now() - timedelta(seconds=1)
        policy.save()
        self.assertFalse(_allowed(self.other, self.user))
        policy.expires_at = None
        policy.save()
        module = BusinessModule.objects.create(name='Paused', slug='paused', is_active=False)
        self.other.module = module
        self.other.save()
        self.assertFalse(_allowed(self.other, self.user))

    def test_zone_scope_and_fixed_endpoint_full_scope(self):
        extra = LoanBranch.objects.create(code='0013', name='Other branch', zone=self.zone)
        self.tool.loan_access_mode = 'FIXED'
        self.tool.fixed_branch_codes = ['0012', '0013']
        self.tool.save()
        self.client.post(URL, self.payload())
        self.assertFalse(UserToolAccess.objects.exists())
        response = self.client.post(URL, self.payload(zones=[self.zone.pk], branches=[]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(snapshot(self.user)['directory']), 2)

    def test_audit_failure_rolls_back_both_grants(self):
        with patch('agentic_ai.user_access.record_event', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.client.post(URL, self.payload())
        self.assertFalse(UserToolAccess.objects.exists())
        self.assertFalse(LoanAccessPolicy.objects.exists())

    def test_root_account_cannot_be_targeted(self):
        self.assertEqual(self.client.post(URL, self.payload(user=self.root.pk)).status_code, 404)

    def test_page_shows_user_and_legacy_explanation(self):
        response = self.client.get(URL, {'user': self.user.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Group access')
        self.assertContains(response, '0012')
