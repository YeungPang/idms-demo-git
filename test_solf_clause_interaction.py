import unittest
from unittest.mock import MagicMock

from interaction import IDMSInteractionTools, InteractionState


class _FakeInterpreter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def _invoke_clause(self, clause_name, call_args):
        self.calls.append((clause_name, call_args))
        return self.result


class TestSolfClauseInteraction(unittest.TestCase):
    def _new_tools(self) -> IDMSInteractionTools:
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)
        tools.logger = MagicMock()
        tools.state = InteractionState()
        tools.query_cache = None
        return tools

    def test_evaluate_solf_clause_uses_payload_when_args_missing(self):
        tools = self._new_tools()
        fake = _FakeInterpreter({"ok": True})
        tools.solf_interpreter = fake

        result = tools.evaluate_solf_clause(
            clause_name="query_style_policy",
            payload={"query": "hello"},
            args=None,
        )

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("clause_name"), "query_style_policy")
        self.assertEqual(result.get("args"), [{"query": "hello"}])
        self.assertEqual(result.get("result"), {"ok": True})
        self.assertEqual(fake.calls, [("query_style_policy", [{"query": "hello"}])])

    def test_evaluate_solf_clause_prefers_explicit_args(self):
        tools = self._new_tools()
        fake = _FakeInterpreter("done")
        tools.solf_interpreter = fake

        result = tools.evaluate_solf_clause(
            clause_name="scheduler_command",
            payload={"ignored": True},
            args=["start", {"job": "daily"}],
        )

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("args"), ["start", {"job": "daily"}])
        self.assertEqual(fake.calls, [("scheduler_command", ["start", {"job": "daily"}])])

    def test_evaluate_solf_clause_rejects_unsafe_clause_name(self):
        tools = self._new_tools()
        tools.solf_interpreter = _FakeInterpreter({"ok": True})

        result = tools.evaluate_solf_clause(
            clause_name="bad-clause;drop",
            payload={},
            args=[],
        )

        self.assertFalse(result.get("success"))
        self.assertIn("Unsafe SOLF clause name", str(result.get("message")))

    def test_evaluate_solf_clause_handles_missing_interpreter(self):
        tools = self._new_tools()
        tools.solf_interpreter = None

        result = tools.evaluate_solf_clause(
            clause_name="query_style_policy",
            payload={},
            args=[],
        )

        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("message"), "SOLF interpreter unavailable")

    def test_extract_solf_clause_request_from_text(self):
        tools = self._new_tools()

        req = tools._extract_solf_clause_request(
            'Please execute predicate query_style_policy with {"question":"hello"}'
        )

        self.assertIsInstance(req, dict)
        self.assertEqual(req.get("clause_name"), "query_style_policy")
        self.assertEqual(req.get("payload"), {"question": "hello"})
        self.assertEqual(req.get("args"), [])

    def test_query_routes_explicit_solf_predicate_request(self):
        tools = self._new_tools()
        tools.solf_interpreter = _FakeInterpreter({"mode": "allow", "reason": "ok"})

        result = tools.query('run solf predicate query_style_policy with {"question":"hello"}')

        self.assertEqual(result.get("source"), "solf_clause")
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("clause_name"), "query_style_policy")
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        self.assertEqual(data.get("result"), {"mode": "allow", "reason": "ok"})

    def test_autonomous_query_routes_explicit_solf_predicate_request(self):
        tools = self._new_tools()
        tools.solf_interpreter = _FakeInterpreter({"mode": "allow", "reason": "ok"})

        result = tools.autonomous_query(
            'invoke clause query_style_policy with {"question":"hello"}',
            max_steps=1,
            record_history=False,
        )

        self.assertEqual(result.get("answer_source"), "solf_clause")
        grounding = result.get("grounding") if isinstance(result.get("grounding"), dict) else {}
        solf_result = grounding.get("solf_clause_result") if isinstance(grounding.get("solf_clause_result"), dict) else {}
        self.assertTrue(solf_result.get("success"))
        self.assertEqual(solf_result.get("clause_name"), "query_style_policy")


if __name__ == "__main__":
    unittest.main()
