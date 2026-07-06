import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from interaction import IDMSInteractionTools


class TestComplexInteractionAgent(unittest.TestCase):
    def _new_tools(self) -> IDMSInteractionTools:
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)
        tools.logger = MagicMock()
        tools.client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_kwargs: SimpleNamespace(text="{}")))
        tools._record_model_usage = lambda *_args, **_kwargs: None
        return tools

    def test_resolve_complex_interaction_requests_user_input(self):
        tools = self._new_tools()

        first_payload = {
            "understanding": "Need reconciliation process but key constraints are missing",
            "objectives": ["prepare reconciliation workflow"],
            "planned_steps": [
                {
                    "step_id": "s1",
                    "name": "ingest statement",
                    "purpose": "normalize bank statement",
                    "inputs": ["statement file"],
                    "outputs": ["statement rows"],
                }
            ],
            "proposed_actions": ["retrieve matching ledger entries"],
            "retrieval_plan": ["statement rows by date and amount"],
            "solf_candidates": [
                {
                    "class_name": "accounting_transaction",
                    "clause_name": "action_plan",
                    "usage_reason": "compose retrieval and reconciliation actions",
                    "confidence": 0.78,
                }
            ],
            "missing_information": ["tolerance threshold", "matching priority"],
            "user_prompt": "Please provide the tolerance threshold and matching priority rules.",
            "response_draft": "",
            "next_action": "ask_user_input",
        }

        def _fake_fallback(**_kwargs):
            return SimpleNamespace(text=json.dumps(first_payload))

        with patch("interaction.generate_content_with_openrouter_fallback", side_effect=_fake_fallback):
            result = tools.resolve_complex_interaction(
                request_text="create a full accounting reconciliation process for a bank statement",
                context={"policy": "no python generation"},
                max_iterations=4,
            )

        self.assertTrue(result.get("success"))
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        self.assertTrue(summary.get("needs_user_input"))
        self.assertEqual(summary.get("iterations_executed"), 1)
        self.assertIn("tolerance threshold", str(result.get("user_prompt") or "").lower())

    def test_resolve_complex_interaction_finalizes_after_refinement(self):
        tools = self._new_tools()

        outputs = [
            {
                "understanding": "Need staged interaction plan",
                "objectives": ["resolve request", "map to solf"],
                "planned_steps": [
                    {
                        "step_id": "s1",
                        "name": "collect required context",
                        "purpose": "prepare constraints",
                        "inputs": ["request"],
                        "outputs": ["context"],
                    }
                ],
                "proposed_actions": ["query scope resolution"],
                "retrieval_plan": ["fetch candidate transactions"],
                "solf_candidates": [],
                "missing_information": [],
                "user_prompt": "",
                "response_draft": "",
                "next_action": "refine_with_context",
            },
            {
                "understanding": "Plan is complete",
                "objectives": ["resolve request", "map to solf"],
                "planned_steps": [
                    {
                        "step_id": "s1",
                        "name": "collect required context",
                        "purpose": "prepare constraints",
                        "inputs": ["request"],
                        "outputs": ["context"],
                    },
                    {
                        "step_id": "s2",
                        "name": "map to solf candidates",
                        "purpose": "prepare executable semantics",
                        "inputs": ["context", "solf snapshot"],
                        "outputs": ["candidate clauses"],
                    },
                ],
                "proposed_actions": ["execute interaction with policy checks"],
                "retrieval_plan": ["retrieve transactions and balances"],
                "solf_candidates": [
                    {
                        "class_name": "accounting_transaction",
                        "clause_name": "action_plan",
                        "usage_reason": "compose actions",
                        "confidence": 0.84,
                    }
                ],
                "missing_information": [],
                "user_prompt": "",
                "response_draft": "Reconciliation interaction plan prepared.",
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
            result = tools.resolve_complex_interaction(
                request_text="resolve complex reconciliation interaction",
                context={"policy": "non-code plan only"},
                max_iterations=4,
            )

        self.assertTrue(result.get("success"))
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        self.assertFalse(summary.get("needs_user_input"))
        self.assertEqual(summary.get("final_next_action"), "finalize")
        self.assertEqual(summary.get("iterations_executed"), 2)
        final_resolution = result.get("final_resolution") if isinstance(result.get("final_resolution"), dict) else {}
        self.assertGreaterEqual(len(final_resolution.get("planned_steps") or []), 2)

    def test_resolve_complex_interaction_production_runs_calculation(self):
        tools = self._new_tools()

        planner_payload = {
            "understanding": "Need payroll estimate for Zug",
            "workflow_steps": [
                {"step_id": "s1", "name": "identify required deductions"},
                {"step_id": "s2", "name": "retrieve external deduction rates"},
                {"step_id": "s3", "name": "calculate deterministic net salary"},
            ],
            "required_information": [
                {
                    "canonical_key": "gross_salary_monthly",
                    "label": "Gross salary per month",
                    "source_preference": "internal",
                    "required": True,
                    "reason": "Base amount for deductions",
                },
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "label": "AHV/IV/EO employee rate",
                    "source_preference": "external",
                    "required": True,
                    "reason": "Statutory social contribution",
                },
                {
                    "canonical_key": "alv_rate_employee",
                    "label": "ALV employee rate",
                    "source_preference": "external",
                    "required": True,
                    "reason": "Unemployment insurance contribution",
                },
                {
                    "canonical_key": "children_allowance_monthly",
                    "label": "Children allowance monthly",
                    "source_preference": "external",
                    "required": True,
                    "reason": "Allowance component",
                },
            ],
            "external_retrieval_requests": [
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "retrieval_query": "Swiss AHV IV EO employee deduction rate 2026",
                    "jurisdiction": "CH-ZG",
                    "required_fields": ["value", "effective_date", "source_url"],
                },
                {
                    "canonical_key": "alv_rate_employee",
                    "retrieval_query": "Swiss ALV employee deduction rate 2026",
                    "jurisdiction": "CH-ZG",
                    "required_fields": ["value", "effective_date", "source_url"],
                },
                {
                    "canonical_key": "children_allowance_monthly",
                    "retrieval_query": "Kanton Zug children allowance amount 2026",
                    "jurisdiction": "CH-ZG",
                    "required_fields": ["value", "effective_date", "source_url"],
                },
            ],
            "mapping_plan": [],
            "missing_information": [],
            "user_prompt": "",
            "response_draft": "Payroll estimate can now be calculated.",
            "calculator_id": "payroll_net_v1",
            "next_action": "calculate",
        }

        retrieval_payload = {
            "facts": [
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "value": 0.053,
                    "value_type": "rate",
                    "unit": "ratio",
                    "jurisdiction": "CH-ZG",
                    "effective_date": "2026-01-01",
                    "source_url": "https://example.org/ahv",
                    "source_title": "AHV statutory table",
                    "confidence": 0.93,
                    "notes": "",
                },
                {
                    "canonical_key": "alv_rate_employee",
                    "value": 0.011,
                    "value_type": "rate",
                    "unit": "ratio",
                    "jurisdiction": "CH-ZG",
                    "effective_date": "2026-01-01",
                    "source_url": "https://example.org/alv",
                    "source_title": "ALV statutory table",
                    "confidence": 0.91,
                    "notes": "",
                },
                {
                    "canonical_key": "children_allowance_monthly",
                    "value": 300.0,
                    "value_type": "amount",
                    "unit": "CHF",
                    "jurisdiction": "CH-ZG",
                    "effective_date": "2026-01-01",
                    "source_url": "https://example.org/allowance",
                    "source_title": "Zug family allowance",
                    "confidence": 0.9,
                    "notes": "",
                },
            ]
        }

        calls = [planner_payload, retrieval_payload]
        call_index = {"value": 0}

        def _fake_fallback(**_kwargs):
            idx = call_index["value"]
            payload = calls[min(idx, len(calls) - 1)]
            call_index["value"] = idx + 1
            return SimpleNamespace(text=json.dumps(payload))

        with patch("interaction.generate_content_with_openrouter_fallback", side_effect=_fake_fallback):
            result = tools.resolve_complex_interaction_production(
                request_text="Calculate monthly net salary in Zug with social deductions and children allowance",
                context={"gross_salary_monthly": 10000, "currency": "CHF"},
                max_iterations=3,
                llm_model="gemini-2.5-flash",
                max_retrieval_rounds=2,
                strict_provenance=True,
                run_calculation=True,
            )

        self.assertTrue(result.get("success"))
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        self.assertTrue(summary.get("calculation_executed"))
        calculation = result.get("calculation") if isinstance(result.get("calculation"), dict) else {}
        self.assertTrue(calculation.get("success"))
        self.assertEqual(calculation.get("calculator_id"), "payroll_net_v1")
        self.assertAlmostEqual(float(calculation.get("net_salary_monthly")), 9660.0, places=2)

    def test_resolve_complex_interaction_production_strict_provenance_requests_user_input(self):
        tools = self._new_tools()

        planner_payload = {
            "understanding": "Need payroll estimate but rates must be sourced",
            "workflow_steps": [{"step_id": "s1", "name": "collect deductions"}],
            "required_information": [
                {
                    "canonical_key": "gross_salary_monthly",
                    "label": "Gross salary per month",
                    "source_preference": "internal",
                    "required": True,
                    "reason": "",
                },
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "label": "AHV/IV/EO employee rate",
                    "source_preference": "external",
                    "required": True,
                    "reason": "",
                },
            ],
            "external_retrieval_requests": [
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "retrieval_query": "AHV/IV/EO rate",
                    "jurisdiction": "CH-ZG",
                    "required_fields": ["value", "effective_date", "source_url"],
                }
            ],
            "mapping_plan": [],
            "missing_information": ["ahv_iv_eo_rate_employee"],
            "user_prompt": "",
            "response_draft": "",
            "calculator_id": "payroll_net_v1",
            "next_action": "refine_with_context",
        }

        retrieval_payload = {
            "facts": [
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "value": 0.053,
                    "value_type": "rate",
                    "unit": "ratio",
                    "jurisdiction": "CH-ZG",
                    "effective_date": "",
                    "source_url": "",
                    "source_title": "",
                    "confidence": 0.7,
                    "notes": "",
                }
            ]
        }

        calls = [planner_payload, retrieval_payload]
        call_index = {"value": 0}

        def _fake_fallback(**_kwargs):
            idx = call_index["value"]
            payload = calls[min(idx, len(calls) - 1)]
            call_index["value"] = idx + 1
            return SimpleNamespace(text=json.dumps(payload))

        with patch("interaction.generate_content_with_openrouter_fallback", side_effect=_fake_fallback):
            result = tools.resolve_complex_interaction_production(
                request_text="Calculate payroll",
                context={"gross_salary_monthly": 9000},
                max_iterations=2,
                llm_model="gemini-2.5-flash",
                strict_provenance=True,
                run_calculation=True,
            )

        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        self.assertTrue(summary.get("needs_user_input"))
        self.assertIn("provide", str(result.get("user_prompt") or "").lower())

    def test_resolve_complex_interaction_production_executes_generic_query_action(self):
        tools = self._new_tools()
        tools.query = lambda question: {
            "answer": f"query result for: {question}",
            "answer_source": "generic_query",
            "answer_sources": ["generic_query"],
        }

        planner_payload = {
            "understanding": "Need generic complex request handling",
            "workflow_steps": [{"step_id": "s1", "name": "run generic query"}],
            "required_information": [],
            "external_retrieval_requests": [],
            "mapping_plan": [],
            "execution_actions": [
                {
                    "action_type": "query",
                    "name": "",
                    "payload": {"question": "Find open incidents with highest risk"},
                    "args": [],
                }
            ],
            "missing_information": [],
            "user_prompt": "",
            "response_draft": "",
            "calculator_id": "",
            "next_action": "execute_query",
        }

        def _fake_fallback(**_kwargs):
            return SimpleNamespace(text=json.dumps(planner_payload))

        with patch("interaction.generate_content_with_openrouter_fallback", side_effect=_fake_fallback):
            result = tools.resolve_complex_interaction_production(
                request_text="Handle this complex request that is not payroll or workflow design",
                context={"tenant": "global"},
                max_iterations=2,
                llm_model="gemini-2.5-flash",
                run_calculation=False,
                execute_generic_actions=True,
            )

        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        self.assertTrue(summary.get("generic_actions_executed"))
        generic_execution = result.get("generic_execution") if isinstance(result.get("generic_execution"), dict) else {}
        self.assertTrue(generic_execution.get("executed"))
        entries = generic_execution.get("results") if isinstance(generic_execution.get("results"), list) else []
        self.assertEqual(len(entries), 1)
        first_result = entries[0].get("result") if isinstance(entries[0], dict) else {}
        self.assertIn("open incidents", str(first_result.get("answer") or "").lower())

    def test_resolve_complex_interaction_production_live_source_verification_enforced(self):
        tools = self._new_tools()

        planner_payload = {
            "understanding": "Need source-validated rate",
            "workflow_steps": [{"step_id": "s1", "name": "retrieve and validate source"}],
            "required_information": [
                {
                    "canonical_key": "gross_salary_monthly",
                    "label": "Gross salary per month",
                    "source_preference": "internal",
                    "required": True,
                    "reason": "",
                },
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "label": "AHV/IV/EO employee rate",
                    "source_preference": "external",
                    "required": True,
                    "reason": "",
                },
            ],
            "external_retrieval_requests": [
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "retrieval_query": "AHV/IV/EO rate",
                    "jurisdiction": "CH-ZG",
                    "required_fields": ["value", "effective_date", "source_url"],
                }
            ],
            "mapping_plan": [],
            "execution_actions": [],
            "missing_information": [],
            "user_prompt": "",
            "response_draft": "",
            "calculator_id": "",
            "next_action": "retrieve_external",
        }

        retrieval_payload = {
            "facts": [
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "value": 0.053,
                    "value_type": "rate",
                    "unit": "ratio",
                    "jurisdiction": "CH-ZG",
                    "effective_date": "2026-01-01",
                    "source_url": "https://example.org/ahv",
                    "source_title": "AHV statutory table",
                    "confidence": 0.9,
                    "notes": "",
                }
            ]
        }

        calls = [planner_payload, retrieval_payload]
        call_index = {"value": 0}

        def _fake_fallback(**_kwargs):
            idx = call_index["value"]
            payload = calls[min(idx, len(calls) - 1)]
            call_index["value"] = idx + 1
            return SimpleNamespace(text=json.dumps(payload))

        with patch("interaction.generate_content_with_openrouter_fallback", side_effect=_fake_fallback):
            with patch.object(tools, "_verify_external_source_live", return_value={
                "reachable": False,
                "http_status": 503,
                "final_url": "https://example.org/ahv",
                "content_type": "text/html",
                "content_hash": "",
                "error": "http_error:503",
            }):
                result = tools.resolve_complex_interaction_production(
                    request_text="Calculate payroll",
                    context={"gross_salary_monthly": 9000},
                    max_iterations=2,
                    llm_model="gemini-2.5-flash",
                    strict_provenance=True,
                    verify_external_sources=True,
                    run_calculation=False,
                )

        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        self.assertTrue(summary.get("needs_user_input"))
        external_facts = result.get("external_facts") if isinstance(result.get("external_facts"), list) else []
        self.assertGreaterEqual(len(external_facts), 1)
        validation = external_facts[0].get("validation") if isinstance(external_facts[0], dict) else {}
        source_verification = validation.get("source_verification") if isinstance(validation, dict) else {}
        self.assertTrue(source_verification.get("checked"))
        self.assertFalse(source_verification.get("reachable"))

    def test_resolve_complex_interaction_production_rejects_unapproved_domain(self):
        tools = self._new_tools()

        planner_payload = {
            "understanding": "Need source-validated rate",
            "workflow_steps": [{"step_id": "s1", "name": "retrieve and validate source domain"}],
            "required_information": [
                {
                    "canonical_key": "gross_salary_monthly",
                    "label": "Gross salary per month",
                    "source_preference": "internal",
                    "required": True,
                    "reason": "",
                },
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "label": "AHV/IV/EO employee rate",
                    "source_preference": "external",
                    "required": True,
                    "reason": "",
                },
            ],
            "external_retrieval_requests": [
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "retrieval_query": "AHV/IV/EO rate",
                    "jurisdiction": "CH-ZG",
                    "required_fields": ["value", "effective_date", "source_url"],
                }
            ],
            "mapping_plan": [],
            "execution_actions": [],
            "missing_information": [],
            "user_prompt": "",
            "response_draft": "",
            "calculator_id": "",
            "next_action": "retrieve_external",
        }

        retrieval_payload = {
            "facts": [
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "value": 0.053,
                    "value_type": "rate",
                    "unit": "ratio",
                    "jurisdiction": "CH-ZG",
                    "effective_date": "2026-01-01",
                    "source_url": "https://untrusted.example.org/ahv",
                    "source_title": "Unofficial table",
                    "confidence": 0.6,
                    "notes": "",
                }
            ]
        }

        calls = [planner_payload, retrieval_payload]
        call_index = {"value": 0}

        def _fake_fallback(**_kwargs):
            idx = call_index["value"]
            payload = calls[min(idx, len(calls) - 1)]
            call_index["value"] = idx + 1
            return SimpleNamespace(text=json.dumps(payload))

        with patch("interaction.generate_content_with_openrouter_fallback", side_effect=_fake_fallback):
            result = tools.resolve_complex_interaction_production(
                request_text="Calculate payroll",
                context={"gross_salary_monthly": 9000},
                max_iterations=2,
                llm_model="gemini-2.5-flash",
                strict_provenance=True,
                verify_external_sources=False,
                run_calculation=False,
                enforce_approved_domains=True,
                approved_source_domains=["admin.ch", "zg.ch"],
            )

        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        self.assertTrue(summary.get("needs_user_input"))
        external_facts = result.get("external_facts") if isinstance(result.get("external_facts"), list) else []
        self.assertGreaterEqual(len(external_facts), 1)
        validation = external_facts[0].get("validation") if isinstance(external_facts[0], dict) else {}
        self.assertFalse(validation.get("domain_approved"))

    def test_resolve_complex_interaction_production_accepts_approved_domain(self):
        tools = self._new_tools()

        planner_payload = {
            "understanding": "Need source-validated rate",
            "workflow_steps": [{"step_id": "s1", "name": "retrieve and validate source domain"}],
            "required_information": [
                {
                    "canonical_key": "gross_salary_monthly",
                    "label": "Gross salary per month",
                    "source_preference": "internal",
                    "required": True,
                    "reason": "",
                },
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "label": "AHV/IV/EO employee rate",
                    "source_preference": "external",
                    "required": True,
                    "reason": "",
                },
            ],
            "external_retrieval_requests": [
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "retrieval_query": "AHV/IV/EO rate",
                    "jurisdiction": "CH-ZG",
                    "required_fields": ["value", "effective_date", "source_url"],
                }
            ],
            "mapping_plan": [],
            "execution_actions": [],
            "missing_information": [],
            "user_prompt": "",
            "response_draft": "",
            "calculator_id": "",
            "next_action": "retrieve_external",
        }

        retrieval_payload = {
            "facts": [
                {
                    "canonical_key": "ahv_iv_eo_rate_employee",
                    "value": 0.053,
                    "value_type": "rate",
                    "unit": "ratio",
                    "jurisdiction": "CH-ZG",
                    "effective_date": "2026-01-01",
                    "source_url": "https://www.zg.ch/de/steuern",
                    "source_title": "Official canton source",
                    "confidence": 0.9,
                    "notes": "",
                }
            ]
        }

        calls = [planner_payload, retrieval_payload]
        call_index = {"value": 0}

        def _fake_fallback(**_kwargs):
            idx = call_index["value"]
            payload = calls[min(idx, len(calls) - 1)]
            call_index["value"] = idx + 1
            return SimpleNamespace(text=json.dumps(payload))

        with patch("interaction.generate_content_with_openrouter_fallback", side_effect=_fake_fallback):
            result = tools.resolve_complex_interaction_production(
                request_text="Calculate payroll",
                context={"gross_salary_monthly": 9000},
                max_iterations=2,
                llm_model="gemini-2.5-flash",
                strict_provenance=True,
                verify_external_sources=False,
                run_calculation=False,
                enforce_approved_domains=True,
                approved_source_domains=["admin.ch", "zg.ch"],
            )

        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        self.assertFalse(summary.get("needs_user_input"))
        external_facts = result.get("external_facts") if isinstance(result.get("external_facts"), list) else []
        self.assertGreaterEqual(len(external_facts), 1)
        validation = external_facts[0].get("validation") if isinstance(external_facts[0], dict) else {}
        self.assertTrue(validation.get("domain_approved"))


if __name__ == "__main__":
    unittest.main()
