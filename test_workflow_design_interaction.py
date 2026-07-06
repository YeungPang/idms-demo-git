import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from interaction import IDMSInteractionTools


class TestWorkflowDesignInteraction(unittest.TestCase):
    def _new_tools(self) -> IDMSInteractionTools:
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)
        tools.logger = MagicMock()
        tools.client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_kwargs: SimpleNamespace(text="{}")))
        tools._record_model_usage = lambda *_args, **_kwargs: None
        return tools

    def test_get_solf_symbols_snapshot_extracts_symbols(self):
        tools = self._new_tools()
        snapshot = tools._get_solf_symbols_snapshot()

        self.assertIsInstance(snapshot, dict)
        self.assertTrue("classes" in snapshot)
        self.assertTrue("clauses" in snapshot)

    def test_design_workflow_interaction_runs_multiple_iterations(self):
        tools = self._new_tools()

        outputs = [
            {
                "understanding": "Need staged reconciliation process",
                "workflow_steps": [
                    {
                        "step_id": "s1",
                        "name": "ingest statement",
                        "purpose": "collect statement lines",
                        "required_inputs": ["bank_statement_file"],
                        "expected_outputs": ["normalized_statement_rows"],
                        "retrieval_or_action": "ingest",
                    }
                ],
                "gaps": ["matching rule thresholds"],
                "solf_candidates": [{"class_name": "accounting_transaction", "clause_name": "", "usage_reason": "target object", "confidence": 0.7}],
                "next_questions": ["what tolerance rules apply"],
                "next_action": "refine_with_context",
            },
            {
                "understanding": "Process is clear and grounded",
                "workflow_steps": [
                    {
                        "step_id": "s1",
                        "name": "ingest statement",
                        "purpose": "collect statement lines",
                        "required_inputs": ["bank_statement_file"],
                        "expected_outputs": ["normalized_statement_rows"],
                        "retrieval_or_action": "ingest",
                    },
                    {
                        "step_id": "s2",
                        "name": "reconcile entries",
                        "purpose": "match statement rows against ledger",
                        "required_inputs": ["normalized_statement_rows", "ledger_lines"],
                        "expected_outputs": ["reconciliation_result"],
                        "retrieval_or_action": "retrieve",
                    },
                ],
                "gaps": [],
                "solf_candidates": [{"class_name": "accounting_transaction", "clause_name": "action_plan", "usage_reason": "workflow composition", "confidence": 0.82}],
                "next_questions": [],
                "next_action": "finalize",
            },
        ]

        call_index = {"value": 0}

        def _fake_fallback(**_kwargs):
            idx = call_index["value"]
            payload = outputs[min(idx, len(outputs) - 1)]
            call_index["value"] = idx + 1
            return SimpleNamespace(text=json.dumps(payload))

        with patch("interaction.generate_content_with_openrouter_fallback", side_effect=_fake_fallback):
            result = tools.design_workflow_interaction(
                request_text="create for me a full accounting reconciliation process for a bank statement",
                context={"rules": ["do not generate python code"]},
                max_iterations=4,
            )

        self.assertTrue(result.get("success"))
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        self.assertEqual(summary.get("final_next_action"), "finalize")
        self.assertEqual(summary.get("iterations_executed"), 2)
        final_plan = result.get("final_plan") if isinstance(result.get("final_plan"), dict) else {}
        self.assertGreaterEqual(len(final_plan.get("workflow_steps") or []), 2)


if __name__ == "__main__":
    unittest.main()
