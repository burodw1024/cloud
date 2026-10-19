"""Exercise the real planner/registry/orchestrator with isolated model and HTTP."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
if not settings.configured:
    settings.configure(SECRET_KEY="isolated-test-only", INSTALLED_APPS=[
        "django.contrib.auth", "django.contrib.contenttypes", "agentic_ai"],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        USE_TZ=True)
    import django
    django.setup()

from agentic_ai import orchestrator
from agentic_ai.planner import PlanningError


def make_user(role="branch_staff"):
    return SimpleNamespace(username="staff1", is_authenticated=True, is_superuser=False,
                           groups=SimpleNamespace(values_list=lambda *a, **kw: [role]))


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.log_patch = patch.object(orchestrator, "write_agent_log")
        self.log = self.log_patch.start()
        self.gateway_patch = patch("agentic_ai.mcp_gateway.gateway_enabled", return_value=False)
        self.gateway_patch.start()
        self.addCleanup(self.gateway_patch.stop)
        self.addCleanup(self.log_patch.stop)

    def test_end_to_end_member_inquiry(self):
        config = {"get_nifi_url": lambda key: "https://nifi.example/query"}
        selection = {"tool": "get_loan_balance", "arguments": {"member_id": "M1"}, "clarification": ""}
        response = Mock()
        response.json.return_value = {"rows": [{"MEMBERID": "M1", "OUTSTANDINGP": "125000.50",
            "Outstanding(P+I)": "130000.50", "PHONENO": "private-phone"}]}
        # Config module only: actual HTTP client and actual tool implementation.
        import sys
        from types import ModuleType
        fake_config = ModuleType("chat.dynamic_config")
        fake_config.__dict__.update(config)
        fake_config.get_setting = lambda key, default="": default
        fake_config.get_llm_model = lambda: "test-model"
        with patch.dict(sys.modules, {"chat.dynamic_config": fake_config}), \
             patch("agentic_ai.planner.request_selection", return_value=selection), \
             patch("requests.post", return_value=response) as http:
            # Ensure previous dependency-isolation tests cannot leave a cached tool.
            sys.modules.pop("agentic_ai.tools.nifi_tool", None)
            try:
                result = orchestrator.run_agentic_query(make_user(), "principal remaining for member M1")
                self.assertTrue(result["ok"])
                self.assertIn("125000.50", result["answer"])
                self.assertNotIn("private-phone", str(result))
                self.assertNotIn("private-phone", str(self.log.call_args))
                self.assertEqual(http.call_args.kwargs["json"]["params"], {"member_id": "M1"})
                response.json.return_value["rows"][0]["MEMBERID"] = "M2"
                result = orchestrator.run_agentic_query(make_user(), "principal remaining for member M1")
                self.assertFalse(result["ok"])
                self.assertNotIn("125000.50", str(result))
            finally:
                sys.modules.pop("agentic_ai.tools.nifi_tool", None)

    def test_clarification_never_executes_backend(self):
        selection = {"tool": "", "arguments": {}, "clarification": "Which member ID?"}
        with patch("agentic_ai.planner.request_selection", return_value=selection), patch("requests.post") as http:
            result = orchestrator.run_agentic_query(make_user(), "my savings balance")
            self.assertTrue(result["clarification_required"])
            http.assert_not_called()

    def test_model_cannot_bypass_permissions(self):
        selection = {"tool": "get_loan_balance", "arguments": {"member_id": "M1"}, "clarification": ""}
        with patch("agentic_ai.planner.request_selection", return_value=selection), patch("requests.post") as http:
            result = orchestrator.run_agentic_query(None, "loan balance for M1")
            self.assertEqual(result["error_code"], "planning_failed")
            http.assert_not_called()

    def test_approval_comes_from_server_registry(self):
        selection = {"tool": "outlook_tool", "arguments": {}, "clarification": ""}
        with patch("agentic_ai.planner.request_selection", return_value=selection), \
             patch.object(orchestrator.AgentApprovalRequest.objects, "create", return_value=SimpleNamespace(id=42)) as create, \
             patch("requests.post") as http:
            result = orchestrator.run_agentic_query(make_user("manager"), "draft an email")
            self.assertTrue(result["approval_required"])
            self.assertEqual(result["approval_id"], 42)
            create.assert_called_once()
            http.assert_not_called()

    def test_planner_failure_is_audited_and_does_not_execute(self):
        with patch("agentic_ai.planner.request_selection", side_effect=PlanningError("Unavailable")), \
             patch("requests.post") as http:
            result = orchestrator.run_agentic_query(make_user(), "loan balance for M1")
            self.assertEqual(result["error_code"], "planning_failed")
            self.assertEqual(self.log.call_args.kwargs["intent"], "planning_failed")
            http.assert_not_called()
