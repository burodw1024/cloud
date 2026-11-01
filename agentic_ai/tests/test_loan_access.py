from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt
from asgiref.sync import sync_to_async, async_to_sync
from django.contrib.auth.models import User, Group, Permission
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client
from django.utils import timezone
from mcp.types import Tool, CallToolResult, TextContent

from agentic_ai import mcp_gateway as gateway
from agentic_ai.api import _can_replay_result
from agentic_ai.access_admin import import_branches
from agentic_ai.loan_access import snapshot, authorize, current_access, validate_response, LoanAccessDenied
from agentic_ai.loan_scope_backend import TOKEN_FIELD, verify_loan_access_token, authorization_receipt
from agentic_ai.models import LoanZone, LoanBranch, LoanAccessPolicy, LoanAccessEvent, AgentTool, AgentRunLog
from .test_native_loan_query import SCHEMA, ARGS, QUERY, envelope

KEY = 'test-only-dedicated-signing-key-with-256-bit-length'
NAME = 'loan-due-report'


def scoped_tool():
    schema = deepcopy(SCHEMA)
    schema['properties'][TOKEN_FIELD] = {'type': 'string'}
    schema['required'].append(TOKEN_FIELD)
    return Tool(name=NAME, inputSchema=schema)


class LoanAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('branch-user')
        self.zone = LoanZone.objects.create(code='07', name='Zone seven')
        self.other_zone = LoanZone.objects.create(code='08', name='Zone eight')
        self.branch = LoanBranch.objects.create(code='0253', name='Branch A', zone=self.zone)
        self.second = LoanBranch.objects.create(code='0254', name='Branch B', zone=self.zone)
        self.other = LoanBranch.objects.create(code='0301', name='Branch C', zone=self.other_zone)
        self.policy = LoanAccessPolicy.objects.create(user=self.user, mode='SELECTED')
        self.policy.branches.add(self.branch)
        self.user.groups.add(Group.objects.create(name='loan_users'))
        self.record = AgentTool.objects.create(name=NAME, allowed_roles=['loan_users'])
        self.key_patch = patch.dict(os.environ, {'LOAN_ACCESS_SIGNING_KEY': KEY})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)

    def grant(self, args=None, tool=None):
        return authorize(self.user, NAME, args if args is not None else ARGS, tool or scoped_tool())

    def test_missing_policy_none_and_superuser_without_explicit_grant_are_denied(self):
        self.policy.delete()
        self.user.is_superuser = True
        self.user.save()
        with self.assertRaises(LoanAccessDenied):
            snapshot(self.user)
        LoanAccessPolicy.objects.create(user=self.user, mode='NONE')
        with self.assertRaises(LoanAccessDenied):
            self.grant()

    def test_selected_zones_and_branches_union_and_future_branch_resolution(self):
        self.policy.zones.add(self.other_zone)
        self.assertEqual([r['code'] for r in snapshot(self.user)['directory']], ['0253', '0301'])
        LoanBranch.objects.create(code='0302', name='New branch', zone=self.other_zone)
        self.assertEqual(self.grant({'report_date': '2026-09-30'}) .receipt['branch_codes'], ['0253', '0301', '0302'])

    def test_all_access_is_explicit_and_excludes_paused_directory(self):
        self.policy.mode = 'ALL'
        self.policy.save()
        self.second.is_active = False
        self.second.save()
        self.other_zone.is_active = False
        self.other_zone.save()
        self.assertEqual(self.grant({'report_date': '2026-09-30'}).receipt['branch_codes'], ['0253'])

    def test_requested_branch_zone_and_intersection_cannot_expand_scope(self):
        with self.assertRaises(LoanAccessDenied):
            self.grant({**ARGS, 'branch_code': '0254'})
        with self.assertRaises(LoanAccessDenied):
            self.grant({'report_date': '2026-09-30', 'zone_code': '08'})
        with self.assertRaises(LoanAccessDenied):
            self.grant({**ARGS, 'zone_code': '08'})
        grant = self.grant({'report_date': '2026-09-30', 'zone_code': '07'})
        self.assertEqual(grant.receipt['branch_codes'], ['0253'])

    def test_unconfigured_backend_or_signing_never_falls_back_to_unscoped_execution(self):
        with self.assertRaises(LoanAccessDenied):
            self.grant(tool=Tool(name=NAME, inputSchema=SCHEMA))
        with patch.dict(os.environ, {'LOAN_ACCESS_SIGNING_KEY': ''}), self.assertRaises(LoanAccessDenied):
            self.grant()

    def test_tokens_bind_identity_tool_query_and_branch_scope(self):
        grant = self.grant()
        claims = verify_loan_access_token(grant.token, KEY, NAME, {**ARGS, TOKEN_FIELD: grant.token})
        self.assertEqual(claims['sub'], str(self.user.pk))
        self.assertEqual(claims['branches'], ['0253'])
        self.assertEqual(authorization_receipt(claims), grant.receipt)
        for tool, params, key in [(NAME, {**ARGS, 'branch_code': '0254'}, KEY),
                                  ('another-tool', ARGS, KEY), (NAME, ARGS, KEY + 'wrong')]:
            with self.assertRaises((ValueError, jwt.InvalidTokenError)):
                verify_loan_access_token(grant.token, key, tool, params)
        claims['exp'] = int(time.time()) - 1
        with self.assertRaises(jwt.ExpiredSignatureError):
            verify_loan_access_token(jwt.encode(claims, KEY, algorithm='HS256'), KEY, NAME, ARGS)

    def test_token_never_outlives_policy_expiry(self):
        self.policy.expires_at = timezone.now() + timedelta(seconds=30)
        self.policy.save()
        grant = self.grant()
        claims = verify_loan_access_token(grant.token, KEY, NAME, ARGS)
        self.assertEqual(claims['exp'], int(self.policy.expires_at.timestamp()))

    def test_model_cannot_supply_authorization(self):
        for key in (TOKEN_FIELD, 'auth.branch_codes', 'authorized_branches'):
            with self.assertRaises(LoanAccessDenied):
                self.grant({**ARGS, key: 'forged'})
        for extra in ({'branch_code': 253}, {'branch_code': ''}, {'zone_code': 7}, {'branchCode': '0254'}):
            with self.assertRaises(LoanAccessDenied):
                self.grant({**ARGS, **extra})

    def test_legacy_member_filter_token_binds_actual_backend_arguments(self):
        args = {'branchCode': '0253', 'reportDate': '2026-09-30', 'memberId': '0001'}
        backend = {key: value for key, value in args.items() if key != 'memberId'}
        grant = authorize(self.user, NAME, args, scoped_tool(), backend)
        self.assertEqual(verify_loan_access_token(grant.token, KEY, NAME, backend)['query'], backend)

    def test_receipt_missing_tampered_or_outside_branch_is_rejected(self):
        grant = self.grant()
        data = envelope()
        with self.assertRaises(LoanAccessDenied):
            validate_response({'structuredContent': data}, grant)
        data['authorization'] = grant.receipt
        validate_response({'structuredContent': data}, grant)
        for mutation in ('subject', 'request_id', 'scope_version'):
            bad = deepcopy(data)
            bad['authorization'][mutation] = 'wrong'
            with self.assertRaises(LoanAccessDenied):
                validate_response({'structuredContent': bad}, grant)
        data['data'][0]['BRANCHCODE'] = '0254'
        with self.assertRaises(LoanAccessDenied):
            validate_response({'structuredContent': data}, grant)
        data['data'][0]['BRANCHCODE'] = '0253'
        data[TOKEN_FIELD] = grant.token
        with self.assertRaises(LoanAccessDenied):
            validate_response({'structuredContent': data}, grant)

    def test_sharing_rechecks_scope_after_gateway_connection_before_send(self):
        from agentic_ai import whatsapp
        source = {'tool': NAME, 'plan': {'intent': 'mcp_tool_call'}, 'loan_access': self.grant().access}
        name = 'send-whatsapp-text'
        AgentTool.objects.create(name=name, allowed_roles=['loan_users'])
        @asynccontextmanager
        async def transport(*a, **kw):
            yield None, None, None
        session = AsyncMock()
        session.__aenter__.return_value = session
        async def revoke():
            await sync_to_async(LoanAccessPolicy.objects.filter(pk=self.policy.pk).update)(mode='NONE')
        session.initialize.side_effect = revoke
        session.list_tools.return_value = SimpleNamespace(tools=[Tool(name=name, inputSchema={'type': 'object'})], nextCursor=None)
        with patch('mcp.client.streamable_http.streamablehttp_client', transport), patch('mcp.ClientSession', return_value=session):
            result = async_to_sync(whatsapp.dispatch)({'url': 'https://gateway.example/mcp', 'token': '', 'timeout': 2},
                         name, '8801700000000', 'A report', self.user, source)
        self.assertEqual(result['http_status'], 403)
        session.call_tool.assert_not_awaited()

    def test_revocation_expiry_directory_moves_and_inactive_account_invalidate_access(self):
        metadata = self.grant().access
        self.assertTrue(current_access(self.user, metadata))
        self.policy.branches.clear()
        self.assertFalse(current_access(self.user, metadata))
        self.policy.branches.add(self.branch)
        self.policy.expires_at = timezone.now() - timedelta(seconds=1)
        self.policy.save()
        self.assertFalse(current_access(self.user, metadata))
        self.policy.expires_at = None
        self.policy.save()
        metadata = self.grant().access
        self.branch.zone = self.other_zone
        self.branch.save()
        self.assertFalse(current_access(self.user, metadata))
        metadata = self.grant().access
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertFalse(current_access(self.user, metadata))

    def test_loan_replay_without_scope_metadata_is_denied(self):
        report = {'tool': NAME, 'plan': {'intent': 'mcp_tool_call'}}
        self.assertFalse(_can_replay_result(self.user, report))
        report['loan_access'] = self.grant().access
        self.assertTrue(_can_replay_result(self.user, report))
        self.policy.mode = 'NONE'
        self.policy.save()
        self.assertFalse(_can_replay_result(self.user, report))

    def test_custom_scoped_tools_and_integration_readiness_are_enforced(self):
        self.record.name = 'custom-loan-report'
        self.record.requires_loan_scope = True
        self.record.save()
        result = gateway._decision(self.user, self.record.name, ARGS, Tool(name=self.record.name, inputSchema=SCHEMA))
        self.assertEqual(result['code'], 'loan_access_unavailable')
        self.assertEqual(result['http_status'], 503)
        self.assertFalse(_can_replay_result(self.user, {'tool': self.record.name, 'plan': {'intent': 'mcp_tool_call'}}))

    def test_explicit_no_access_cannot_be_bypassed_by_model_or_admin_role(self):
        self.user.is_superuser = True
        self.user.save()
        self.policy.mode = 'NONE'
        self.policy.save()
        result = gateway._decision(self.user, NAME, ARGS, scoped_tool())
        self.assertEqual(result['code'], 'loan_access_denied')

    def run_gateway(self, args=None, revoke=False, missing_receipt=False):
        args = args or ARGS
        @asynccontextmanager
        async def transport(*a, **kw):
            yield None, None, None
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(tools=[scoped_tool()], nextCursor=None)
        async def call(name, arguments):
            claims = verify_loan_access_token(arguments[TOKEN_FIELD], KEY, name, arguments)
            data = envelope(args)
            if not missing_receipt:
                data['authorization'] = authorization_receipt(claims)
            if revoke:
                await sync_to_async(LoanAccessPolicy.objects.filter(pk=self.policy.pk).update)(mode='NONE')
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(data))])
        session.call_tool.side_effect = call
        config = {'url': 'https://gateway.example/mcp', 'token': '', 'timeout': 2,
                  'request_timeout': 10}
        with patch('mcp.client.streamable_http.streamablehttp_client', transport), \
             patch('mcp.ClientSession', return_value=session), \
             patch.object(gateway, 'load_config', return_value=config), \
             patch.object(gateway, '_select_tool', new=AsyncMock(return_value=({}, (NAME, args)))) as model:
            result = gateway.run_mcp_query(self.user, QUERY.replace('0253', args.get('branch_code', '0253')))
        return result, session, model

    def test_real_gateway_authorizes_signs_validates_and_never_logs_token(self):
        result, session, model = self.run_gateway()
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['tool_executed'])
        self.assertIn('loan_access', result)
        self.assertNotIn(TOKEN_FIELD, model.call_args.args[2][0].inputSchema['properties'])
        token = session.call_tool.call_args.kwargs['arguments'][TOKEN_FIELD]
        self.assertNotIn(token, json.dumps(result))
        log = AgentRunLog.objects.get()
        self.assertNotIn(token, str(log.tool_input) + str(log.final_answer) + str(log.error_message))

    def test_real_gateway_denies_out_of_scope_before_tool_call_and_records_attempt(self):
        result, session, _ = self.run_gateway({**ARGS, 'branch_code': '0254'})
        self.assertEqual(result['code'], 'loan_access_denied')
        session.call_tool.assert_not_awaited()
        self.assertEqual(AgentRunLog.objects.get().status, 'blocked')

    def test_real_gateway_rechecks_access_after_query_and_requires_backend_receipt(self):
        result, _, _ = self.run_gateway(revoke=True)
        self.assertEqual(result['code'], 'loan_scope_unverified')
        self.assertNotIn('200.5', json.dumps(result))
        self.policy.mode = 'SELECTED'
        self.policy.save()
        result, _, _ = self.run_gateway(missing_receipt=True)
        self.assertEqual(result['code'], 'loan_scope_unverified')


class AccessAdministrationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('access-admin', password=None)
        self.user = User.objects.create_user('branch-user')
        self.zone = LoanZone.objects.create(code='07', name='Zone seven')
        self.branch = LoanBranch.objects.create(code='0253', name='Branch A', zone=self.zone)
        self.client.force_login(self.admin)

    def data(self, **changes):
        data = {'user': self.user.pk, 'mode': 'SELECTED', 'branches': [self.branch.pk],
                'zones': [], 'expires_at': '', 'reason': 'Approved by business owner', 'version': 'new'}
        data.update(changes)
        return data

    def test_save_grant_and_revoke_are_audited_and_optimistic_lock_prevents_overwrite(self):
        response = self.client.post('/agentic/data-access/', self.data())
        self.assertEqual(response.status_code, 302)
        policy = LoanAccessPolicy.objects.get(user=self.user)
        self.assertEqual(policy.branches.get(), self.branch)
        self.assertEqual(LoanAccessEvent.objects.get().actor, self.admin)
        stale = self.client.post('/agentic/data-access/', self.data(mode='NONE', branches=[]))
        self.assertContains(stale, 'changed in another session')
        self.assertEqual(LoanAccessEvent.objects.count(), 1)
        response = self.client.post('/agentic/data-access/', self.data(mode='NONE', branches=[], version=policy.updated_at.isoformat()))
        self.assertEqual(response.status_code, 302)
        policy.refresh_from_db()
        self.assertEqual(policy.mode, 'NONE')
        self.assertEqual(LoanAccessEvent.objects.count(), 2)

    def test_staff_and_custom_permission_required_and_all_access_separately_controlled(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/agentic/data-access/').status_code, 302)
        staff = User.objects.create_user('delegated', is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get('/agentic/data-access/').status_code, 403)
        staff.user_permissions.add(Permission.objects.get(codename='manage_loan_access'))
        self.assertEqual(self.client.get('/agentic/data-access/').status_code, 200)
        denied = self.client.post('/agentic/data-access/', self.data(mode='ALL', branches=[]))
        self.assertEqual(denied.status_code, 200)
        self.assertFalse(LoanAccessPolicy.objects.exists())
        self.assertEqual(self.client.get('/agentic/data-access/directory/').status_code, 403)
        self.assertEqual(self.client.get('/agentic/setup/').status_code, 403)
        self_denied = self.client.post('/agentic/data-access/', self.data(user=staff.pk))
        self.assertContains(self_denied, 'cannot change their own access')

    def test_csrf_and_grant_shape_validation(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post('/agentic/data-access/', self.data()).status_code, 403)
        for data in (self.data(branches=[]), self.data(mode='ALL'), self.data(reason='x')):
            response = self.client.post('/agentic/data-access/', data)
            self.assertTrue(response.context['form'].errors)
        self.assertFalse(LoanAccessPolicy.objects.exists())

    def test_audit_failure_rolls_back_policy(self):
        with patch('agentic_ai.access_admin.record_event', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.client.post('/agentic/data-access/', self.data())
        self.assertFalse(LoanAccessPolicy.objects.exists())

    def test_existing_disabled_user_selector_uses_hidden_subject_and_all_access_is_explicit(self):
        policy = LoanAccessPolicy.objects.create(user=self.user, mode='NONE')
        page = self.client.get(f'/agentic/data-access/?user={self.user.pk}')
        self.assertContains(page, f'name="subject_id" value="{self.user.pk}"')
        data = self.data(mode='ALL', branches=[], version=policy.updated_at.isoformat(), subject_id=self.user.pk)
        data.pop('user')  # Browsers omit disabled form controls.
        self.assertEqual(self.client.post('/agentic/data-access/', data).status_code, 302)
        policy.refresh_from_db()
        self.assertEqual(policy.mode, 'ALL')
        self.assertFalse(policy.branches.exists())

    def test_audit_identity_survives_account_deletion(self):
        self.client.post('/agentic/data-access/', self.data())
        self.user.delete()
        event = LoanAccessEvent.objects.get()
        self.assertIsNone(event.subject)
        self.assertEqual(event.subject_label, 'branch-user')

    def test_invalid_form_after_success_does_not_report_saved_again(self):
        response = self.client.post('/agentic/data-access/directory/?saved=1', {
            'action': 'zone', 'zone-code': '07', 'zone-name': '', 'zone-reason': 'Approved edit'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['zone_form'].errors)
        self.assertEqual(response.context['notice'], '')

    def test_csv_import_validates_atomically_and_preserves_leading_zeros(self):
        def file(body):
            return SimpleUploadedFile('branches.csv', ('zone_code,zone_name,branch_code,branch_name,active\n' + body).encode())
        self.assertEqual(import_branches(file('07,Zone seven,0007,New branch,1\n'), self.admin, 'Approved directory import'), 1)
        self.assertTrue(LoanBranch.objects.filter(code='0007').exists())
        before_count, event_count = LoanBranch.objects.count(), LoanAccessEvent.objects.count()
        with self.assertRaises(ValueError):
            import_branches(file('08,Zone eight,0301,New branch,1\n08,Zone eight,0301,Duplicate,1\n'), self.admin, 'Approved directory import')
        self.assertEqual(LoanBranch.objects.count(), before_count)
        self.assertEqual(LoanAccessEvent.objects.count(), event_count)
        self.assertFalse(LoanZone.objects.filter(code='08').exists())
        with self.assertRaises(ValueError):
            import_branches(file(''.join(f'07,Zone seven,{i:04},Branch,1\n' for i in range(5001))), self.admin, 'Approved directory import')
        self.assertEqual(LoanBranch.objects.count(), before_count)

    def test_directory_changes_are_audited_and_html_is_escaped(self):
        response = self.client.post('/agentic/data-access/directory/', {
            'action': 'zone', 'zone-code': '07', 'zone-name': '<script>not executable</script>',
            'zone-is_active': 'on', 'zone-reason': 'Approved zone name change'}, follow=True)
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>not executable</script>')
        event = LoanAccessEvent.objects.get()
        self.assertEqual(event.before['name'], 'Zone seven')
        self.assertEqual(event.after['name'], '<script>not executable</script>')
