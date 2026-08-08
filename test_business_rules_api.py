import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import business_rules
from idms_api_server.application import app


class TestBusinessRulesAPI(unittest.TestCase):
    @staticmethod
    def _mock_structured_payload() -> dict:
        return {
            "rule_name": "vat_runtime_policy",
            "mappings": [
                {
                    "source_attribute_terms": ["vat number", "mwst number", "vat_no"],
                    "target_attribute": "registration number",
                    "relationship": "same_as",
                    "distinct_from": [],
                    "scope": {
                        "countries": ["switzerland"],
                        "document_types": ["invoice"],
                        "entity_classes": ["company"],
                        "operations": ["ingest", "query"],
                    },
                }
            ],
            "workflow_hints": [
                {"process": "ingest", "control_flow": "sequence", "stop_on_error": True},
                {"process": "query", "control_flow": "selection", "stop_on_error": False},
            ],
            "class_definitions": [
                {
                    "class_name": "customs_declaration",
                    "parent_class_name": "document",
                    "attributes": ["declaration_no", "customs_office"],
                }
            ],
        }

    def setUp(self):
        self.client = TestClient(app)

    def test_simulate_draft_endpoint_generates_solf_for_ingest_query_context(self):
        payload = self._mock_structured_payload()
        fake_response = SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))

        req = {
            "rule_text": (
                "For Swiss invoices, VAT number means registration number during ingest and query. "
                "Use sequence flow for ingest and selection flow for query."
            ),
            "rule_name": "vat_runtime_policy",
            "requested_attribute": "vat number",
            "context": {
                "country": "switzerland",
                "document_type": "invoice",
                "entity_class": "company",
                "operation": "ingest",
                "process": "ingest",
            },
            "sample_attributes": {"vat number": "CHE-399.737.068"},
        }

        with patch.object(business_rules, "_make_genai_client", return_value=MagicMock()):
            with patch.object(
                business_rules,
                "generate_content_with_openrouter_fallback",
                return_value=fake_response,
            ):
                response = self.client.post("/api/business-rules/simulate-draft", json=req)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))

        result = data.get("result") or {}
        self.assertTrue(result.get("matched"))
        self.assertEqual((result.get("resolved_attribute") or {}).get("canonical_attribute"), "registration_no")
        self.assertEqual((result.get("workflow_hint") or {}).get("control_flow"), "sequence")
        self.assertEqual((result.get("transformed_attributes") or {}).get("registration_no"), "CHE-399.737.068")
        self.assertEqual(result.get("class_definition_source"), "llm")

        solf_script = str(result.get("solf_script") or "")
        self.assertIn("business_rule_attribute_alias", solf_script)
        self.assertIn("business_rule_workflow_hint", solf_script)
        self.assertIn("business_rule_sequence_plan", solf_script)

    def test_generate_and_ingest_from_inspection_endpoint_is_wired(self):
        payload = {
            "goal": "derive a workflow rule from inspection evidence",
            "inspection_context": {
                "web": [{"source": "https://example.com", "summary": "customer contact data"}],
                "internal_db": [{"table": "company", "observation": "contact person available"}],
            },
            "persist": True,
            "created_by": "test",
        }

        with patch(
            "idms_api_server.routers.business_rules.business_rules.generate_and_process_solf_from_inspection",
            return_value={"processed": {"persisted": True, "accepted": True}},
        ) as generate_mock:
            response = self.client.post(
                "/api/business-rules/solf-generation-pack/generate-and-ingest-from-inspection",
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("success"))
        self.assertTrue((response.json().get("result") or {}).get("processed", {}).get("persisted"))
        self.assertEqual(generate_mock.call_args.kwargs.get("goal"), payload["goal"])
        self.assertEqual(generate_mock.call_args.kwargs.get("created_by"), "test")

    def test_generate_and_ingest_from_inspection_endpoint_forwards_workflow_registry_payload(self):
        payload = {
            "goal": "derive a workflow rule from inspection evidence",
            "inspection_context": {
                "web": [{"source": "https://example.com", "summary": "customer contact data"}],
            },
            "persist": True,
            "created_by": "test",
            "workflow_registry": {
                "workflow_key": "inspection_rule_v1",
                "workflow_name": "Inspection Rule V1",
                "description": "Bind the generated rule to a workflow registry entry",
                "domain": "master_data",
                "status": "draft",
                "metadata": {"source": "inspection_ui"},
                "is_active": True,
                "created_by": "test",
            },
        }

        with patch(
            "idms_api_server.routers.business_rules.business_rules.generate_and_process_solf_from_inspection",
            return_value={"processed": {"persisted": True, "accepted": True}},
        ) as generate_mock:
            response = self.client.post(
                "/api/business-rules/solf-generation-pack/generate-and-ingest-from-inspection",
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(generate_mock.call_args.kwargs.get("workflow_registry"), payload["workflow_registry"])

    def test_create_and_simulate_endpoints_are_wired(self):
        created_payload = {
            "rule_id": 321,
            "rule_name": "vat_runtime_policy",
            "structured_rule": {"rule_name": "vat_runtime_policy", "mappings": []},
            "scope": {"countries": ["switzerland"]},
            "semantic_terms_upserted": 3,
            "solf_clauses_upserted": 2,
            "solf_script": "business_rule_attribute_alias(_context) ⦃ ↲({ok: true}) ⦄",
            "is_active": True,
        }
        simulated_payload = {
            "requested_attribute": "vat number",
            "context": {"country": "switzerland", "process": "query"},
            "resolved_attribute": {
                "canonical_attribute": "registration_no",
                "relationship": "same_as",
                "rule_id": 321,
                "rule_name": "vat_runtime_policy",
            },
            "workflow_hint": {
                "control_flow": "selection",
                "stop_on_error": False,
                "rule_id": 321,
                "rule_name": "vat_runtime_policy",
            },
            "transformed_attributes": {"registration_no": "CHE-399.737.068"},
            "matched": True,
        }

        with patch("idms_api_server.routers.business_rules.business_rules.create_business_rule", return_value=created_payload):
            with patch("idms_api_server.routers.business_rules.deps.get_tools.cache_clear") as cache_clear_mock:
                create_resp = self.client.post(
                    "/api/business-rules",
                    json={
                        "rule_text": "For Swiss invoices, VAT number means registration number.",
                        "rule_name": "vat_runtime_policy",
                        "created_by": "test",
                        "is_active": True,
                    },
                )

        self.assertEqual(create_resp.status_code, 200)
        create_data = create_resp.json()
        self.assertTrue(create_data.get("success"))
        self.assertEqual((create_data.get("result") or {}).get("rule_id"), 321)
        cache_clear_mock.assert_called_once()

        with patch(
            "idms_api_server.routers.business_rules.business_rules.simulate_business_rule_resolution",
            return_value=simulated_payload,
        ):
            sim_resp = self.client.post(
                "/api/business-rules/simulate",
                json={
                    "requested_attribute": "vat number",
                    "context": {"country": "switzerland", "process": "query"},
                    "sample_attributes": {"vat number": "CHE-399.737.068"},
                    "rule_id": 321,
                    "include_inactive": True,
                },
            )

        self.assertEqual(sim_resp.status_code, 200)
        sim_data = sim_resp.json()
        self.assertTrue(sim_data.get("success"))
        self.assertTrue((sim_data.get("result") or {}).get("matched"))
        self.assertEqual(
            ((sim_data.get("result") or {}).get("resolved_attribute") or {}).get("canonical_attribute"),
            "registration_no",
        )

    def test_create_business_rule_returns_clarification_for_vague_booking_text(self):
        with patch.object(business_rules, "_make_genai_client", return_value=None):
            response = self.client.post(
                "/api/business-rules",
                json={
                    "rule_text": "Booking for Seveco AG.",
                    "rule_name": "seveco_booking",
                    "created_by": "test",
                    "is_active": True,
                },
            )

        self.assertEqual(response.status_code, 409)
        data = response.json()
        detail = data.get("detail") if isinstance(data, dict) else {}
        self.assertTrue((detail or {}).get("clarification_required"))
        clarification = (detail or {}).get("result") or {}
        self.assertEqual(clarification.get("reason_code"), "ambiguous_booking_party")
        self.assertEqual(clarification.get("expected_attributes"), ["legal_entity_ref", "company_ref"])
        self.assertIn("Seveco AG", clarification.get("clarification_question") or "")

    def test_create_business_rule_forwards_application_mode(self):
        created_payload = {
            "rule_id": 321,
            "rule_name": "vat_runtime_policy",
            "structured_rule": {"rule_name": "vat_runtime_policy", "application_mode": "selected_only", "mappings": []},
            "scope": {"countries": ["switzerland"]},
            "semantic_terms_upserted": 0,
            "solf_clauses_upserted": 0,
            "solf_script": "",
            "is_active": True,
        }

        with patch("idms_api_server.routers.business_rules.business_rules.create_business_rule", return_value=created_payload) as create_mock:
            response = self.client.post(
                "/api/business-rules",
                json={
                    "rule_text": "For invoices set legal entity to Seveco AG.",
                    "rule_name": "seveco_booking",
                    "created_by": "test",
                    "is_active": True,
                    "application_mode": "selected_only",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(create_mock.call_args.kwargs.get("application_mode"), "selected_only")

    def test_simulate_draft_business_rule_forwards_application_mode(self):
        draft_payload = {
            "rule_name": "draft_vat_policy",
            "structured_rule": {"rule_name": "draft_vat_policy", "application_mode": "general", "mappings": []},
            "scope": {"countries": []},
            "solf_script": "",
            "actionability": {"warnings": [], "clarifications": []},
            "needs_clarification": False,
            "persisted": False,
        }

        with patch("idms_api_server.routers.business_rules.business_rules.simulate_draft_business_rule", return_value=draft_payload) as simulate_mock:
            response = self.client.post(
                "/api/business-rules/simulate-draft",
                json={
                    "rule_text": "For invoices set legal entity to Seveco AG.",
                    "rule_name": "seveco_booking",
                    "requested_attribute": "",
                    "application_mode": "general",
                    "context": {},
                    "sample_attributes": {},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(simulate_mock.call_args.kwargs.get("application_mode"), "general")

    def test_get_business_rule_artifacts_endpoint(self):
        artifacts_payload = {
            "rule_id": 321,
            "rule_name": "vat_runtime_policy",
            "is_active": True,
            "artifacts": {
                "class_definition_source": "llm",
                "classes": [{"class_name": "customs_declaration", "parent_class_name": "document", "attributes": ["declaration_no"]}],
                "class_count": 1,
                "facts_preview": [{"fact_type": "attribute_mapping", "fact_key": "mapping_1"}],
                "fact_count": 1,
                "clauses": [{"index": 1, "clause_name": "business_rule_attribute_alias", "clause_text": "business_rule_attribute_alias(_context) ⦃ ↲({ok: true}) ⦄"}],
                "clause_count": 1,
                "scope": {"countries": ["switzerland"]},
            },
        }

        with patch("idms_api_server.routers.business_rules.business_rules.get_business_rule_artifacts", return_value=artifacts_payload):
            resp = self.client.get("/api/business-rules/321/artifacts")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        result = data.get("result") or {}
        self.assertEqual(result.get("rule_id"), 321)
        self.assertEqual(((result.get("artifacts") or {}).get("class_count")), 1)
        self.assertEqual(((result.get("artifacts") or {}).get("clause_count")), 1)

    def test_review_draft_business_rule_endpoint(self):
        draft_payload = {
            "rule_name": "draft_vat_policy",
            "class_definition_source": "fallback",
            "structured_rule": {
                "rule_name": "draft_vat_policy",
                "mappings": [],
                "workflow_hints": [],
                "class_definitions": [{"class_name": "customs_declaration", "parent_class_name": "document", "attributes": ["declaration_no"]}],
                "class_definition_source": "fallback",
            },
            "scope": {"countries": []},
            "solf_script": "business_rule_attribute_alias(_context) ⦃ ↲({ok: true}) ⦄",
        }
        artifacts_payload = {
            "class_definition_source": "fallback",
            "classes": [{"class_name": "customs_declaration", "parent_class_name": "document", "attributes": ["declaration_no"]}],
            "class_count": 1,
            "facts_preview": [{"fact_type": "attribute_mapping", "fact_key": "mapping_1"}],
            "fact_count": 1,
            "clauses": [{"index": 1, "clause_name": "business_rule_attribute_alias", "clause_text": "business_rule_attribute_alias(_context) ⦃ ↲({ok: true}) ⦄"}],
            "clause_count": 1,
            "scope": {"countries": []},
        }

        with patch("idms_api_server.routers.business_rules.business_rules.simulate_draft_business_rule", return_value=draft_payload):
            with patch("idms_api_server.routers.business_rules.business_rules.build_rule_artifacts_view", return_value=artifacts_payload):
                resp = self.client.post(
                    "/api/business-rules/review-draft",
                    json={
                        "rule_text": "Create class customs declaration.",
                        "rule_name": "draft_vat_policy",
                        "requested_attribute": "",
                        "context": {},
                        "sample_attributes": {},
                    },
                )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        result = data.get("result") or {}
        self.assertEqual(result.get("rule_name"), "draft_vat_policy")
        self.assertEqual(result.get("class_definition_source"), "fallback")
        self.assertEqual(((result.get("artifacts") or {}).get("fact_count")), 1)


if __name__ == "__main__":
    unittest.main()
