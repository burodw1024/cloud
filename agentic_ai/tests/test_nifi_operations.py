import importlib
import sys
import unittest
from types import ModuleType
from unittest.mock import Mock, patch

from agentic_ai.nifi_operations import project_result
from agentic_ai.planner import plan_query, PlanningError


class OperationTests(unittest.TestCase):
    def test_projection_removes_personal_data_and_preserves_amounts(self):
        row = {"OUTSTANDINGP": "125000.50", "Outstanding(P+I)": None,
               "MEMBERNAME": "Private", "PHONENO": "Private"}
        expected = [{"outstanding_principal": "125000.50", "outstanding_total": None}]
        for payload in (row, [row], {"rows": [row]}, {"data": {"results": [row]}}):
            self.assertEqual(project_result(payload, "loan_balance"), expected)

    def test_empty_and_invalid_results(self):
        self.assertEqual(project_result([], "loan_balance"), [])
        for payload in ({"raw": "error"}, [{"OUTSTANDINGP": 1}],
                        {"success": False, "rows": []}, "invalid"):
            with self.assertRaises(ValueError):
                project_result(payload, "loan_balance")

    def test_other_operations(self):
        for operation, row, expected in (
            ("loan_due", {"DUEAMOUNT": 100, "DUE_DAYS": 3, "DUEINSTNO_U": 1},
             {"due_amount": 100, "due_days": 3, "due_installments": 1}),
            ("loan_installment", {"INSTALLMENT_AMOUNT": 100, "PAYMENTFREQUENCY": "monthly"},
             {"installment_amount": 100, "payment_frequency": "monthly"}),
            ("savings_balance", {"GS_BALANCE": 20, "CS_BALANCE": 30},
             {"gs_balance": 20, "cs_balance": 30}),
        ):
            self.assertEqual(project_result([row], operation), [expected])

    def test_no_catalog_never_calls_model(self):
        with patch("agentic_ai.planner.request_selection") as model:
            with self.assertRaises(PlanningError):
                plan_query("my savings balance")
            model.assert_not_called()

    def test_scope_and_financial_value_validation(self):
        for row in (
            {"MEMBERID": "OTHER", "GS_BALANCE": 1, "CS_BALANCE": 2},
            {"MEMBERID": "M1", "GS_BALANCE": "NaN", "CS_BALANCE": 2},
            {"MEMBERID": "M1", "GS_BALANCE": {"private": "data"}, "CS_BALANCE": 2},
        ):
            with self.assertRaises(ValueError):
                project_result([row], "savings_balance", member_id="M1")

    def test_handler_filters_both_data_and_answer(self):
        # Isolate HTTP/config dependencies; no live services or Django DB required.
        requests = ModuleType("requests")
        response = Mock()
        response.json.return_value = [{"MEMBERID": "M1", "GS_BALANCE": 12, "CS_BALANCE": 34, "PHONENO": "private"}]
        requests.post = Mock(return_value=response)
        config = ModuleType("chat.dynamic_config")
        config.get_nifi_url = Mock(return_value="https://nifi.example/query")
        name = "agentic_ai.tools.nifi_tool"
        with patch.dict(sys.modules, {"requests": requests, "chat.dynamic_config": config}):
            sys.modules.pop(name, None)
            tool = importlib.import_module(name)
            try:
                result = tool.get_savings_balance(params={"member_id": "M1"})
                self.assertTrue(result["ok"])
                self.assertNotIn("private", str(result))
                self.assertEqual(result["data"], [{"gs_balance": 12, "cs_balance": 34}])
                self.assertEqual(requests.post.call_args.kwargs["json"]["params"], {"member_id": "M1"})
                response.json.return_value = {"unexpected": "private"}
                self.assertFalse(tool.get_savings_balance(params={"member_id": "M1"})["ok"])
            finally:
                sys.modules.pop(name, None)
