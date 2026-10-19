import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from agentic_ai.planner import plan_query, validate_selection, request_selection, PlanningError
from agentic_ai.tool_registry import available_tool_catalog


def user_with_roles(*roles):
    return SimpleNamespace(is_authenticated=True, is_superuser=False,
                           groups=SimpleNamespace(values_list=lambda *a, **kw: list(roles)))


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tools = available_tool_catalog(user_with_roles("branch_staff"))

    def test_semantic_selection_uses_model_for_paraphrases(self):
        selection = {"tool": "get_loan_balance", "arguments": {"member_id": "M1"}, "clarification": ""}
        for query in ("For member M1, how much principal remains unpaid?",
                      "সদস্য M1 এর ঋণের অবশিষ্ট মূলধন কত?"):
            with patch("agentic_ai.planner.request_selection", return_value=selection) as model:
                plan = plan_query(query, self.tools)
                self.assertEqual(plan["tool"], "get_loan_balance")
                model.assert_called_once_with(query, self.tools)

    def test_invalid_selection_rejected(self):
        base = {"tool": "get_loan_balance", "arguments": {"member_id": "M1"}, "clarification": ""}
        invalid = [
            {**base, "tool": "nifi_tool"},
            {**base, "arguments": {"member_id": "OTHER"}},
            {**base, "arguments": {"member_id": "M1", "sql": "SELECT *"}},
            {**base, "arguments": {}},
            {**base, "arguments": {"member_id": 1}},
            {**base, "requires_approval": False},
            {**base, "clarification": "also answer"},
            {**base, "tool": []},
        ]
        for selection in invalid:
            with self.subTest(selection=selection), self.assertRaises(PlanningError):
                validate_selection(selection, "member M1", self.tools)
        with self.assertRaises(PlanningError):
            validate_selection(base, "member XM1", self.tools)

    def test_missing_member_and_unsupported_report_clarify(self):
        for query in ("my savings balance", "which branch has highest overdue?"):
            with patch("agentic_ai.planner.request_selection", return_value={
                "tool": "", "arguments": {}, "clarification": "Please specify a supported member inquiry and member ID."
            }):
                self.assertIsNone(plan_query(query, self.tools)["tool"])

    def test_catalog_is_permission_filtered(self):
        anonymous = available_tool_catalog(None)
        self.assertNotIn("get_savings_balance", anonymous)
        self.assertIn("website_rag_tool", anonymous)
        self.assertNotIn("nifi_tool", self.tools)
        self.assertNotIn("outlook_tool", self.tools)
        self.assertIn("outlook_tool", available_tool_catalog(user_with_roles("manager")))

    def test_ollama_transport_uses_runtime_configuration(self):
        config = ModuleType("chat.dynamic_config")
        config.get_setting = lambda key, default="": {"AGENT_PLANNER_MODEL": "local-planner", "AGENT_PLANNER_TIMEOUT": "30"}.get(key, default)
        config.get_llm_model = lambda: "fallback"
        config.get_ollama_url = lambda: "https://ollama.example/"
        response = Mock()
        response.json.return_value = {"message": {"content": json.dumps({
            "tool": "", "arguments": {}, "clarification": "Which member?"})}}
        with patch.dict(sys.modules, {"chat.dynamic_config": config}), patch("requests.post", return_value=response) as post:
            result = request_selection("balance", self.tools)
            self.assertEqual(result["clarification"], "Which member?")
            self.assertEqual(post.call_args.args[0], "https://ollama.example/api/chat")
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["model"], "local-planner")
            self.assertIsInstance(payload["format"], dict)
            self.assertEqual(post.call_args.kwargs["timeout"], (5, 30))
            response.json.return_value = {"message": {"content": "not json"}}
            with self.assertRaises(PlanningError):
                request_selection("balance", self.tools)

    def test_model_outage_never_falls_back_to_keyword_routing(self):
        with patch("agentic_ai.planner.request_selection", side_effect=PlanningError("Unavailable")):
            with self.assertRaises(PlanningError):
                plan_query("loan outstanding member M1", self.tools)
