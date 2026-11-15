from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import TestCase, SimpleTestCase
from django.utils import timezone

from agentic_ai.models import AgentRunLog
from agentic_ai.mcp_gateway import run_mcp_query
from agentic_ai.performance import stage, summary, trace_rows, percentile
from agentic_ai.performance_dashboard import metrics_for
from .test_live_scope_reconciliation import ApplicationScopeTests


def trace(total=100, backend=80):
    return {'version': 1, 'total_ms': total, 'request_id': 'safe-request-id', 'outcome': 'ok',
            'completed': True, 'dispatched': True, 'model_calls': 1,
            'stages': [{'name': 'backend', 'duration_ms': backend, 'status': 'completed'},
                       {'name': 'selection', 'duration_ms': total-backend, 'status': 'completed'}]}


class TimerTests(SimpleTestCase):
    def test_stage_records_elapsed_time_and_error_without_exception_contents(self):
        state = {}
        with patch('agentic_ai.performance.time.monotonic', side_effect=[1, 1.25]):
            with self.assertRaisesRegex(ValueError, 'secret'):
                with stage(state, 'backend'):
                    raise ValueError('secret credential')
        self.assertEqual(state['_timings'], [{'name': 'backend', 'duration_ms': 250, 'status': 'interrupted'}])
        result = summary(state, 300, 'id', {})
        self.assertEqual(result['stages'][-1]['duration_ms'], 50)
        self.assertNotIn('secret', str(result))

    def test_metrics_percentiles_and_old_logs_are_not_estimated(self):
        rows = [{'performance_summary': trace(n, n*.8), 'status': 'success', 'selected_tool': 'loan'} for n in [100, 200, 300, 400]]
        rows += [{'performance_summary': {}, 'status': 'failed', 'selected_tool': ''}]
        result = metrics_for(rows)
        self.assertEqual(result['measured'], 4)
        self.assertEqual(result['unmeasured'], 1)
        self.assertEqual(result['avg'], 250)
        self.assertEqual(result['p95'], 400)
        self.assertEqual(result['p50'], 200)
        self.assertEqual(result['bottleneck']['name'], 'backend')
        self.assertEqual(result['bottleneck']['share'], 80)
        self.assertIsNone(percentile([], .95))

    def test_malformed_and_nonfinite_traces_are_ignored(self):
        for data in [None, [], {}, {'version': 1, 'total_ms': float('nan')}, trace(-1), {'version': 1, 'total_ms': 10, 'stages': 'bad'}]:
            self.assertEqual(trace_rows(data), [])


class GatewayTimingTests(TestCase):
    setUp = ApplicationScopeTests.setUp
    run_native_gateway = ApplicationScopeTests.run_native_gateway

    def test_real_query_persists_stages_and_does_not_expose_trace_to_user(self):
        result, _ = self.run_native_gateway()
        self.assertTrue(result['ok'], result)
        stored = AgentRunLog.objects.get().performance_summary
        self.assertEqual(stored['request_id'], result['request_id'])
        self.assertEqual(stored['total_ms'], result['duration_ms'])
        stages = {s['name'] for s in stored['stages']}
        self.assertTrue({'connection', 'discovery', 'selection', 'authorization', 'backend', 'presentation'} <= stages)
        self.assertNotIn('_timings', result)
        self.assertNotIn('performance_summary', result)

    def test_backend_timeout_still_persists_interrupted_stage(self):
        with patch('agentic_ai.tests.test_live_scope_reconciliation.envelope', side_effect=TimeoutError):
            result, _ = self.run_native_gateway()
        self.assertEqual(result['code'], 'upstream_timeout')
        stored = AgentRunLog.objects.get().performance_summary
        backend = next(item for item in stored['stages'] if item['name'] == 'backend')
        self.assertEqual(backend['status'], 'interrupted')
        self.assertTrue(stored['dispatched'])
        self.assertFalse(stored['completed'])
        self.assertNotIn('_timings', result)

    def test_api_query_to_saved_trace_to_reviewer_dashboard(self):
        self.client.force_login(self.user)
        with patch('agentic_ai.api.execute_query', side_effect=lambda **kwargs: self.run_native_gateway()[0]):
            response = self.client.post('/agentic/api/query/', {'message': 'Report for my permitted branch'}, content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        request_id = response.json()['request_id']
        reviewer = User.objects.create_user('timing-reviewer', is_staff=True)
        reviewer.user_permissions.add(Permission.objects.get(codename='view_agentrunlog'))
        self.client.force_login(reviewer)
        dashboard = self.client.get('/agentic/performance/')
        self.assertContains(dashboard, request_id)
        self.assertEqual(dashboard.context['metrics']['measured'], 1)

    def test_denied_user_gets_timing_without_external_dispatch(self):
        with patch('agentic_ai.mcp_gateway._authorized_names', return_value=set()):
            result = run_mcp_query(user=self.user, query='A report request')
        self.assertEqual(result['code'], 'no_tools')
        stored = AgentRunLog.objects.get().performance_summary
        self.assertFalse(stored['dispatched'])
        self.assertNotIn('backend', [s['name'] for s in stored['stages']])


class PerformanceDashboardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('24123')
        self.reviewer = User.objects.create_user('reviewer', is_staff=True)
        self.reviewer.user_permissions.add(Permission.objects.get(codename='view_agentrunlog'))
        self.client.force_login(self.reviewer)
        self.log = AgentRunLog.objects.create(user=self.user, user_query='<script>bad()</script>',
            selected_tool='loan-due-report', status='success', performance_summary=trace())

    def test_reviewer_sees_persisted_timings_and_safe_markup(self):
        response = self.client.get('/agentic/performance/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertContains(response, 'Retrieve data through MCP')
        self.assertContains(response, 'safe-request-id')
        self.assertContains(response, '&lt;script&gt;bad()&lt;/script&gt;')
        self.assertNotContains(response, '<script>bad()</script>')
        self.assertEqual(response.context['metrics']['measured'], 1)

    def test_staff_permission_and_get_only_are_enforced(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/agentic/performance/').status_code, 302)
        self.reviewer.user_permissions.clear()
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get('/agentic/performance/').status_code, 403)
        self.reviewer.user_permissions.add(Permission.objects.get(codename='view_agentrunlog'))
        self.assertEqual(self.client.post('/agentic/performance/').status_code, 405)

    def test_filters_time_window_and_unmeasured_attempts(self):
        AgentRunLog.objects.create(user_query='old format', status='success')
        other = AgentRunLog.objects.create(user_query='outside period', performance_summary=trace(999))
        AgentRunLog.objects.filter(pk=other.pk).update(created_at=timezone.now()-timedelta(days=8))
        response = self.client.get('/agentic/performance/')
        self.assertEqual(response.context['metrics']['measured'], 1)
        self.assertEqual(response.context['metrics']['unmeasured'], 1)
        response = self.client.get('/agentic/performance/?user=24123&tool=loan-due-report&status=success&window=7d')
        self.assertEqual(response.context['sampled'], 1)
        response = self.client.get('/agentic/performance/?user=unknown&window=invalid')
        self.assertContains(response, 'No timing traces')
        self.assertEqual(response.context['window'], '24h')

    def test_aggregation_is_bounded_and_pagination_reaches_older_queries(self):
        AgentRunLog.objects.bulk_create([AgentRunLog(user_query='sample', performance_summary=trace()) for _ in range(1001)])
        response = self.client.get('/agentic/performance/?page=2')
        self.assertEqual(response.context['sampled'], 1000)
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertEqual(len(response.context['attempts']), 25)
