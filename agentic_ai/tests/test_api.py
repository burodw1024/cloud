import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory, TestCase

from agentic_ai import api


class AgentApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("api-user")
        self.client.force_login(self.user)
        self.url = "/agentic/api/query/"

    def post(self, value=None, **kwargs):
        return self.client.post(self.url, data=json.dumps(value if value is not None else {"message": "loan aging 0253"}), content_type="application/json", **kwargs)

    @patch.object(api, "execute_query")
    def test_requires_authentication(self, run):
        self.client.logout()
        self.assertEqual(self.post().status_code, 401)
        run.assert_not_called()

    @patch.object(api, "execute_query")
    def test_rejects_non_post(self, run):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")
        run.assert_not_called()

    @patch.object(api, "execute_query")
    def test_malformed_and_non_object_bodies(self, run):
        for data in ("[", "[]", "null", '"query"'):
            response = self.client.post(self.url, data=data, content_type="application/json")
            self.assertEqual(response.status_code, 400)
        run.assert_not_called()

    @patch.object(api, "execute_query")
    def test_non_string_or_long_query(self, run):
        for value in (123, ["loan"], {"query": "loan"}, "x" * 16001, " "):
            self.assertEqual(self.post({"message": value}).status_code, 400)
        run.assert_not_called()

    @patch.object(api, "execute_query")
    def test_oversized_body(self, run):
        self.assertEqual(self.post({"message": "x" * 66000}).status_code, 413)
        run.assert_not_called()

    @patch.object(api, "execute_query")
    def test_csrf_blocks_session_request_without_token(self, run):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(self.url, data='{"message":"loan aging"}', content_type="application/json")
        self.assertEqual(response.status_code, 403)
        run.assert_not_called()

    @patch.object(api, "execute_query", return_value={"ok": True, "answer": "report", "request_id": "test-request"})
    def test_valid_csrf_request(self, run):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        request = RequestFactory().get("/")
        token = get_token(request)
        client.cookies["csrftoken"] = request.META["CSRF_COOKIE"]
        response = client.post(self.url, data='{"message":"loan aging"}', content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Request-ID"], "test-request")
        run.assert_called_once()

    @patch.object(api, "execute_query", side_effect=lambda **kw: {"ok": True, "answer": "report", "usage": {"model_calls": 1, "total_tokens": 120}})
    def test_duplicate_idempotency_key_executes_once(self, run):
        first = self.post(HTTP_IDEMPOTENCY_KEY="same-key")
        second = self.post(HTTP_IDEMPOTENCY_KEY="same-key")
        self.assertEqual({**first.json(), "replayed": True}, second.json())
        self.assertEqual(run.call_count, 1)

    @patch.object(api, "execute_query", side_effect=lambda **kw: {"ok": True, "answer": "report"})
    def test_same_key_for_different_query_conflicts(self, run):
        self.post(HTTP_IDEMPOTENCY_KEY="same-key")
        self.assertEqual(self.post({"message": "another report"}, HTTP_IDEMPOTENCY_KEY="same-key").status_code, 409)
        self.assertEqual(run.call_count, 1)

    @patch.object(api, "execute_query", side_effect=lambda **kw: {"ok": True, "answer": "report"})
    def test_rate_limit(self, run):
        for _ in range(20):
            self.assertEqual(self.post().status_code, 200)
        response = self.post()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "60")
        self.assertEqual(run.call_count, 20)

    @patch.object(api, "execute_query")
    def test_cache_failure_prevents_execution(self, run):
        with patch.object(api.cache, "add", side_effect=RuntimeError):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        run.assert_not_called()

    @patch.object(api, "execute_query", return_value={"ok": False, "http_status": 504, "answer": "timeout", "outcome_unknown": True})
    def test_uncertain_result_retained_for_same_key(self, run):
        self.assertEqual(self.post(HTTP_IDEMPOTENCY_KEY="same-key").status_code, 504)
        self.assertEqual(self.post(HTTP_IDEMPOTENCY_KEY="same-key").status_code, 504)
        self.assertEqual(run.call_count, 1)

    def test_logout_requires_csrf_protected_post_on_django_52(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.get("/agentic/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/logout/").status_code, 405)
        self.assertEqual(client.post("/logout/").status_code, 403)
        token = client.cookies["csrftoken"].value
        response = client.post("/logout/", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", client.session)


    def test_cached_report_denied_after_role_revocation(self):
        from django.contrib.auth.models import Group
        from agentic_ai.models import AgentTool
        group = Group.objects.create(name="loan_report_access")
        self.user.groups.add(group)
        AgentTool.objects.create(name="loan_aging", allowed_roles=[group.name])
        report = {"ok": True, "answer": "sensitive loan report", "tool": "loan_aging",
                  "plan": {"intent": "mcp_tool_call"}}
        with patch.object(api, "execute_query", return_value=report) as run:
            self.assertEqual(self.post(HTTP_IDEMPOTENCY_KEY="restricted-report").status_code, 200)
            self.user.groups.remove(group)
            response = self.post(HTTP_IDEMPOTENCY_KEY="restricted-report")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("sensitive loan report", response.content.decode())
        self.assertEqual(run.call_count, 1)

    def test_cached_report_denied_after_tool_deactivation(self):
        from agentic_ai.models import AgentTool
        record = AgentTool.objects.create(name="loan_aging", allowed_roles=["staff"])
        report = {"ok": True, "answer": "sensitive loan report", "tool": record.name,
                  "plan": {"intent": "mcp_tool_call"}}
        with patch.object(api, "execute_query", return_value=report) as run:
            self.assertEqual(self.post(HTTP_IDEMPOTENCY_KEY="inactive-report").status_code, 200)
            record.is_active = False
            record.save()
            response = self.post(HTTP_IDEMPOTENCY_KEY="inactive-report")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("sensitive loan report", response.content.decode())
        self.assertEqual(run.call_count, 1)
