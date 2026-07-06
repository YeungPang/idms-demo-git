import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import business_rules
import ingest


class _DummyConn:
    def close(self):
        return None


class _FakeInterpreter:
    def __init__(self):
        self.loaded_scripts: list[tuple[str, bool]] = []
        self.parser = None
        self.debug_flag = None

    def set_parser(self, parser):
        self.parser = parser

    def set_debug(self, enabled: bool):
        self.debug_flag = bool(enabled)

    def load_program_script(self, script: str, clear_existing: bool = False):
        self.loaded_scripts.append((str(script), bool(clear_existing)))


class TestSolfNaturalLanguageRuleGeneration(unittest.TestCase):
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
                    "attributes": ["declaration_no", "customs_office", "declarant_name"],
                }
            ],
        }

    def test_natural_language_rule_generates_solf_and_applies_to_ingest_and_query(self):
        rule_text = (
            "For Swiss invoices, VAT number means registration number during ingest and query. "
            "Use sequence flow for ingest and selection flow for query."
        )
        payload = self._mock_structured_payload()
        fake_response = SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))

        with patch.object(business_rules, "_make_genai_client", return_value=MagicMock()):
            with patch.object(
                business_rules,
                "generate_content_with_openrouter_fallback",
                return_value=fake_response,
            ):
                ingest_sim = business_rules.simulate_draft_business_rule(
                    rule_text=rule_text,
                    rule_name="vat_runtime_policy",
                    requested_attribute="vat number",
                    context={
                        "country": "switzerland",
                        "document_type": "invoice",
                        "entity_class": "company",
                        "operation": "ingest",
                        "process": "ingest",
                    },
                    sample_attributes={"vat number": "CHE-399.737.068"},
                )

                query_sim = business_rules.simulate_draft_business_rule(
                    rule_text=rule_text,
                    rule_name="vat_runtime_policy",
                    requested_attribute="mwst number",
                    context={
                        "country": "switzerland",
                        "document_type": "invoice",
                        "entity_class": "company",
                        "operation": "query",
                        "process": "query",
                    },
                    sample_attributes={"mwst number": "CHE-399.737.068"},
                )

        # Ingest process assertions
        self.assertTrue(ingest_sim["matched"])
        self.assertEqual(ingest_sim["resolved_attribute"]["canonical_attribute"], "registration_no")
        self.assertEqual(ingest_sim["resolved_attribute"]["relationship"], "same_as")
        self.assertEqual(ingest_sim["transformed_attributes"]["registration_no"], "CHE-399.737.068")
        self.assertIsNotNone(ingest_sim["workflow_hint"])
        self.assertEqual(ingest_sim["workflow_hint"]["control_flow"], "sequence")
        self.assertTrue(ingest_sim["workflow_hint"]["stop_on_error"])

        # Query process assertions
        self.assertTrue(query_sim["matched"])
        self.assertEqual(query_sim["resolved_attribute"]["canonical_attribute"], "registration_no")
        self.assertEqual(query_sim["transformed_attributes"]["registration_no"], "CHE-399.737.068")
        self.assertIsNotNone(query_sim["workflow_hint"])
        self.assertEqual(query_sim["workflow_hint"]["control_flow"], "selection")
        self.assertFalse(query_sim["workflow_hint"]["stop_on_error"])

        # SOLF generation assertions
        solf_script = str(ingest_sim.get("solf_script") or "")
        self.assertIn("business_rule_attribute_alias", solf_script)
        self.assertIn("business_rule_workflow_hint", solf_script)
        self.assertIn("business_rule_workflow_execute", solf_script)
        self.assertIn("business_rule_sequence_plan", solf_script)
        self.assertIn("business_rule_selection_plan", solf_script)
        self.assertEqual(ingest_sim.get("class_definition_source"), "llm")
        self.assertEqual(ingest_sim.get("class_definitions_preview_count"), 1)
        self.assertEqual((ingest_sim.get("class_definitions_preview") or [])[0].get("class_name"), "customs_declaration")

    def test_persisted_runtime_rule_is_loaded_by_ingest_interpreter(self):
        rule_text = (
            "For Swiss invoices, VAT number means registration number during ingest and query. "
            "Use sequence flow for ingest and selection flow for query."
        )
        payload = self._mock_structured_payload()
        fake_response = SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))

        pattern_library_mock = MagicMock()
        pattern_library_mock.upsert_solf_clause.return_value = 1001

        with patch.object(business_rules, "_make_genai_client", return_value=MagicMock()):
            with patch.object(
                business_rules,
                "generate_content_with_openrouter_fallback",
                return_value=fake_response,
            ):
                with patch.object(business_rules.object_db, "get_connection", return_value=_DummyConn()):
                    with patch.object(business_rules.object_db, "upsert_business_rule", return_value=321):
                        with patch.object(business_rules.object_db, "upsert_semantic_terms", return_value=None):
                            with patch.object(
                                business_rules,
                                "_upsert_runtime_solf_classes",
                                return_value=[
                                    {
                                        "class_id": 999,
                                        "class_name": "customs_declaration",
                                        "parent_class_id": 10,
                                        "metadata": {"source": "business_rule_runtime"},
                                    }
                                ],
                            ):
                                with patch.object(business_rules, "PatternLibrary", return_value=pattern_library_mock):
                                    created = business_rules.create_business_rule(
                                        rule_text=rule_text,
                                        rule_name="vat_runtime_policy",
                                        created_by="test",
                                        is_active=True,
                                    )

        self.assertEqual(created["rule_id"], 321)
        self.assertIn("business_rule_workflow_hint", str(created.get("solf_script") or ""))
        self.assertGreater(created["solf_clauses_upserted"], 0)
        self.assertEqual(created.get("class_definition_source"), "llm")
        self.assertEqual(created.get("class_definitions_created_count"), 1)
        self.assertEqual((created.get("class_definitions_created") or [])[0].get("class_name"), "customs_declaration")

        fake_interpreter = _FakeInterpreter()
        with patch.object(ingest, "SOLFInterpreter", return_value=fake_interpreter):
            with patch.object(
                ingest.business_rules,
                "load_active_business_rule_solf_script",
                return_value=str(created.get("solf_script") or ""),
            ):
                _ = ingest.build_solf_interpreter("base_clause(_x) ⦃ ↲({ok: true}) ⦄")

        self.assertGreaterEqual(len(fake_interpreter.loaded_scripts), 2)
        base_script, base_clear = fake_interpreter.loaded_scripts[0]
        runtime_script, runtime_clear = fake_interpreter.loaded_scripts[1]

        self.assertIn("base_clause", base_script)
        self.assertTrue(base_clear)
        self.assertIn("business_rule_attribute_alias", runtime_script)
        self.assertFalse(runtime_clear)

    def test_rule_text_class_definition_fallback_parser(self):
        with patch.object(business_rules, "_make_genai_client", return_value=None):
            draft = business_rules.simulate_draft_business_rule(
                rule_text=(
                    "Create new SOLF class customs declaration extends document "
                    "with attributes declaration number, customs office and declarant name."
                ),
                rule_name="customs_schema_rule",
                context={"process": "ingest"},
            )

        self.assertEqual(draft.get("class_definitions_preview_count"), 2)
        self.assertEqual(draft.get("class_definition_source"), "fallback")
        preview = draft.get("class_definitions_preview") or []
        class_names = {str(item.get("class_name") or "") for item in preview}
        self.assertIn("customs_declaration", class_names)
        self.assertIn("document", class_names)

        child_defs = [item for item in preview if str(item.get("class_name") or "") == "customs_declaration"]
        self.assertEqual(len(child_defs), 1)
        self.assertEqual(str(child_defs[0].get("parent_class_name") or ""), "document")

    def test_build_solf_interpreter_places_non_overwriting_rules_before_base_script(self):
        fake_interpreter = _FakeInterpreter()
        runtime_rules = [
            {
                "rule_id": 21,
                "rule_name": "preserve_rule",
                "solf_script": "preserve_rule(_x) ⦃ ↲({ok: preserve}) ⦄",
                "metadata": {"overwrite_existing": False},
            },
            {
                "rule_id": 22,
                "rule_name": "overwrite_rule",
                "solf_script": "overwrite_rule(_x) ⦃ ↲({ok: overwrite}) ⦄",
                "metadata": {"overwrite_existing": True},
            },
        ]

        with patch.object(ingest, "SOLFInterpreter", return_value=fake_interpreter):
            with patch.object(business_rules, "list_business_rules", return_value=runtime_rules):
                _ = ingest.build_solf_interpreter("base_clause(_x) ⦃ ↲({ok: base}) ⦄")

        self.assertGreaterEqual(len(fake_interpreter.loaded_scripts), 2)
        first_script, first_clear = fake_interpreter.loaded_scripts[0]
        second_script, second_clear = fake_interpreter.loaded_scripts[1]

        self.assertTrue(first_clear)
        self.assertFalse(second_clear)
        self.assertLess(first_script.index("preserve_rule"), first_script.index("base_clause"))
        self.assertIn("overwrite_rule", second_script)

    def test_compile_rich_inferencing_constructs_to_solf_and_artifacts(self):
        structured = {
            "rule_name": "rich_inferencing_rule",
            "mappings": [
                {
                    "source_attribute_terms": ["beneficial owner"],
                    "target_attribute": "beneficial_owner",
                    "relationship": "same_as",
                    "distinct_from": [],
                    "scope": {
                        "countries": ["switzerland"],
                        "document_types": ["compliance_report"],
                        "entity_classes": ["company"],
                        "operations": ["query"],
                    },
                }
            ],
            "workflow_hints": [],
            "class_definitions": [
                {
                    "class_name": "ownership_statement",
                    "parent_class_name": "document",
                    "attributes": ["statement_id", "owner_name"],
                }
            ],
            "class_definition_source": "llm",
            "fact_assertions": [
                {
                    "fact_name": "ubo_declared",
                    "subject": "company",
                    "predicate": "has_beneficial_owner",
                    "object": "person",
                    "value": "true",
                    "scope": {
                        "countries": ["switzerland"],
                        "document_types": ["compliance_report"],
                        "entity_classes": ["company"],
                        "operations": ["query"],
                    },
                }
            ],
            "multi_hop_dependencies": [
                {
                    "dependency_name": "ubo_through_holding_chain",
                    "source_fact": "ownership_link",
                    "target_fact": "beneficial_owner",
                    "via_relationship": "owns",
                    "max_hops": 3,
                    "direction": "outgoing",
                    "scope": {
                        "countries": ["switzerland"],
                        "document_types": ["compliance_report"],
                        "entity_classes": ["company"],
                        "operations": ["query"],
                    },
                }
            ],
            "quantified_conditions": [
                {
                    "condition_name": "minimum_risk_flags",
                    "quantifier": "count_at_least",
                    "variable": "flag",
                    "in_set": ["pep", "sanctioned", "high_risk_country"],
                    "predicate": "is_true",
                    "threshold": 2,
                    "scope": {
                        "countries": ["switzerland"],
                        "document_types": ["compliance_report"],
                        "entity_classes": ["company"],
                        "operations": ["query"],
                    },
                }
            ],
        }

        solf_script, semantic_terms = business_rules.compile_structured_rule_to_solf(structured, "rich_inferencing_rule")
        self.assertIn("business_rule_fact_assertion", solf_script)
        self.assertIn("business_rule_dependency_inference", solf_script)
        self.assertIn("business_rule_quantified_guard", solf_script)
        self.assertTrue(any(str(item.get("kind") or "") == "fact" for item in semantic_terms))

        artifacts = business_rules.build_rule_artifacts_view(
            structured_rule=structured,
            solf_script=solf_script,
            scope=business_rules._extract_scope_from_structured(structured),
        )
        self.assertGreaterEqual(int(artifacts.get("fact_count") or 0), 4)
        fact_types = {str(item.get("fact_type") or "") for item in (artifacts.get("facts_preview") or [])}
        self.assertIn("attribute_mapping", fact_types)
        self.assertIn("fact_assertion", fact_types)
        self.assertIn("multi_hop_dependency", fact_types)
        self.assertIn("quantified_condition", fact_types)
        clause_names = {str(item.get("clause_name") or "") for item in (artifacts.get("clauses") or [])}
        self.assertIn("business_rule_fact_assertion", clause_names)
        self.assertIn("business_rule_dependency_inference", clause_names)
        self.assertIn("business_rule_quantified_guard", clause_names)

    def test_generic_post_extraction_directives_apply_across_entity_and_document_scope(self):
        structured = {
            "rule_name": "generic_hr_compliance_rule",
            "mappings": [],
            "workflow_hints": [],
            "class_definitions": [],
            "fact_assertions": [],
            "multi_hop_dependencies": [],
            "quantified_conditions": [],
            "post_extraction_directives": [
                {
                    "target_scope": "entity",
                    "target_entity_classes": ["employee_profile"],
                    "conditions": [
                        {
                            "attribute_terms": ["source_tax_code"],
                            "operator": "exists",
                            "values": [],
                        }
                    ],
                    "set_attributes": {
                        "employment_status": "active",
                        "current_department_ref": "finance",
                    },
                    "overwrite_existing": True,
                    "scope": {"document_types": ["payroll_record"], "operations": ["ingest"]},
                },
                {
                    "target_scope": "document",
                    "conditions": [
                        {
                            "attribute_terms": ["doc_type"],
                            "operator": "equals_any",
                            "values": ["payroll_record"],
                        }
                    ],
                    "set_metadata": {"review_queue": "hr_compliance"},
                    "append_workflow_processes": ["compliance review"],
                    "overwrite_existing": True,
                    "scope": {"document_types": ["payroll_record"], "operations": ["ingest"]},
                },
            ],
        }

        extracted = {
            "document": {"doc_type": "payroll_record", "country": "switzerland", "metadata": {}},
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Alice Example",
                    "class_name": "employee_profile",
                    "attributes": {"source_tax_code": "A1"},
                }
            ],
        }
        rule_row = {
            "rule_id": 77,
            "rule_name": "generic_hr_compliance_rule",
            "rule_text": "If payroll record contains source tax code, mark employee active and send doc to compliance review.",
            "structured_rule": structured,
        }

        applied = business_rules.apply_post_extraction_business_rules(
            extracted,
            context={"country": "switzerland", "document_type": "payroll_record", "operation": "ingest"},
            selected_rules=[rule_row],
            include_inferred=False,
        )

        entity_attrs = applied["extracted"]["entities"][0]["attributes"]
        document_attrs = applied["extracted"]["document"]
        document_meta = applied["extracted"]["document"]["metadata"]
        self.assertEqual(entity_attrs.get("employment_status"), "active")
        self.assertEqual(entity_attrs.get("current_department_ref"), "finance")
        self.assertEqual(document_attrs.get("doc_type"), "payroll_record")
        self.assertEqual(document_meta.get("review_queue"), "hr_compliance")
        self.assertIn("compliance_review", document_meta.get("workflow_processes") or [])
        self.assertEqual(int(applied.get("applied_count") or 0), 2)

    def test_document_scope_set_attributes_updates_document_payload(self):
        structured = {
            "rule_name": "document_scope_attribute_rule",
            "mappings": [],
            "workflow_hints": [],
            "class_definitions": [],
            "fact_assertions": [],
            "multi_hop_dependencies": [],
            "quantified_conditions": [],
            "post_extraction_directives": [
                {
                    "target_scope": "document",
                    "conditions": [
                        {
                            "attribute_terms": ["doc_type"],
                            "operator": "equals_any",
                            "values": ["invoice"],
                        }
                    ],
                    "set_attributes": {"legal_entity_ref": "Seveco AG"},
                    "overwrite_existing": True,
                    "scope": {"document_types": ["invoice"], "operations": ["ingest"]},
                }
            ],
        }

        extracted = {
            "document": {"doc_type": "invoice", "country": "switzerland", "metadata": {}},
            "entities": [],
        }
        rule_row = {
            "rule_id": 88,
            "rule_name": "document_scope_attribute_rule",
            "rule_text": "Set legal entity ref to Seveco AG for this document.",
            "structured_rule": structured,
        }

        applied = business_rules.apply_post_extraction_business_rules(
            extracted,
            context={"country": "switzerland", "document_type": "invoice", "operation": "ingest"},
            selected_rules=[rule_row],
            include_inferred=False,
        )

        self.assertEqual(applied["extracted"]["document"].get("legal_entity_ref"), "Seveco AG")
        applications = applied["extracted"]["document"]["metadata"].get("post_extraction_rule_applications") or []
        self.assertTrue(any("document.legal_entity_ref" in (item.get("changed_keys") or []) for item in applications if isinstance(item, dict)))

    def test_artifacts_preview_includes_post_extraction_directives(self):
        structured = {
            "rule_name": "directive_preview_rule",
            "mappings": [],
            "workflow_hints": [],
            "class_definitions": [],
            "fact_assertions": [],
            "multi_hop_dependencies": [],
            "quantified_conditions": [],
            "post_extraction_directives": [
                {
                    "target_scope": "entity",
                    "target_entity_classes": ["receipt"],
                    "conditions": [{"attribute_terms": ["merchant_name"], "operator": "contains_any", "values": ["sbb"]}],
                    "set_attributes": {"booking_particulars": "train_or_bus"},
                    "overwrite_existing": True,
                    "scope": {"document_types": ["receipt"], "operations": ["ingest"]},
                }
            ],
        }

        artifacts = business_rules.build_rule_artifacts_view(
            structured_rule=structured,
            solf_script="",
            scope=business_rules._extract_scope_from_structured(structured),
        )
        fact_types = {str(item.get("fact_type") or "") for item in (artifacts.get("facts_preview") or [])}
        self.assertIn("post_extraction_directive", fact_types)

    def test_fallback_parser_extracts_generic_post_extraction_directive(self):
        with patch.object(business_rules, "_make_genai_client", return_value=None):
            structured = business_rules.parse_rule_text_to_structured(
                "For employee profiles, if source tax code is A1 then set employment status to active and route the document to HR compliance."
            )

        directives = structured.get("post_extraction_directives") if isinstance(structured.get("post_extraction_directives"), list) else []
        self.assertGreaterEqual(len(directives), 1)
        first = directives[0]
        self.assertIn(str(first.get("target_scope") or ""), {"entity", "document"})
        self.assertTrue(bool(first.get("conditions") or first.get("set_attributes") or first.get("set_metadata")))

    def test_fallback_parser_maps_nl_booking_fields_to_runtime_keys(self):
        with patch.object(business_rules, "_make_genai_client", return_value=None):
            structured = business_rules._normalize_structured_rule(
                business_rules.parse_rule_text_to_structured(
                    "All invoices from tank stations should be booked to travelling expenses account and particulars are train or bus."
                )
            )

        directives = structured.get("post_extraction_directives") if isinstance(structured.get("post_extraction_directives"), list) else []
        self.assertGreaterEqual(len(directives), 1)
        set_attributes = directives[0].get("set_attributes") if isinstance(directives[0].get("set_attributes"), dict) else {}
        self.assertEqual(set_attributes.get("booking_debit_account_name"), "travelling expenses")
        self.assertEqual(set_attributes.get("booking_particulars"), "train or bus")

    def test_fallback_parser_splits_multiple_condition_values(self):
        with patch.object(business_rules, "_make_genai_client", return_value=None):
            structured = business_rules._normalize_structured_rule(
                business_rules.parse_rule_text_to_structured(
                    "For invoices, bills, and receipts, if description contains diesel, petrol, gasoline, or tanking, set booking account to travelling expenses account. Particulars are fuel or tanking."
                )
            )

        directives = structured.get("post_extraction_directives") if isinstance(structured.get("post_extraction_directives"), list) else []
        self.assertGreaterEqual(len(directives), 1)
        conditions = directives[0].get("conditions") if isinstance(directives[0].get("conditions"), list) else []
        self.assertGreaterEqual(len(conditions), 1)
        self.assertEqual(conditions[0].get("attribute_terms"), ["description"])
        self.assertEqual(conditions[0].get("values"), ["diesel", "petrol", "gasoline", "tanking"])

    def test_fallback_parser_maps_reference_phrases_to_runtime_keys(self):
        with patch.object(business_rules, "_make_genai_client", return_value=None):
            structured = business_rules._normalize_structured_rule(
                business_rules.parse_rule_text_to_structured(
                    "For invoice entities of Seveco AG, set legal entity to Seveco AG and set company reference to Seveco AG."
                )
            )

        directives = structured.get("post_extraction_directives") if isinstance(structured.get("post_extraction_directives"), list) else []
        self.assertGreaterEqual(len(directives), 1)
        set_attributes = directives[0].get("set_attributes") if isinstance(directives[0].get("set_attributes"), dict) else {}
        self.assertEqual(set_attributes.get("legal_entity_ref"), "Seveco AG")
        self.assertEqual(set_attributes.get("company_ref"), "Seveco AG")

    def test_vague_booking_rule_triggers_clarification_request(self):
        with patch.object(business_rules, "_make_genai_client", return_value=None):
            with self.assertRaises(business_rules.BusinessRuleClarificationNeeded) as ctx:
                business_rules.create_business_rule(
                    rule_text="Booking for Seveco AG.",
                    rule_name="seveco_booking",
                    created_by="test",
                    is_active=True,
                )

        clarification = ctx.exception.clarification
        self.assertEqual(clarification.get("reason_code"), "ambiguous_booking_party")
        self.assertEqual(clarification.get("expected_attributes"), ["legal_entity_ref", "company_ref"])
        self.assertIn("Should legal_entity_ref and company_ref be set to Seveco AG", clarification.get("clarification_question") or "")

    def test_llm_document_scope_booking_directive_is_repaired_by_fallback_parser(self):
        llm_payload = {
            "rule_name": "tanking_booking",
            "mappings": [],
            "workflow_hints": [],
            "class_definitions": [],
            "fact_assertions": [],
            "multi_hop_dependencies": [],
            "quantified_conditions": [],
            "post_extraction_directives": [
                {
                    "target_scope": "document",
                    "target_entity_classes": [],
                    "conditions": [],
                    "set_attributes": {
                        "booking_debit_account_name": "travelling expenses",
                        "booking_particulars": "fuel or tanking",
                    },
                    "set_metadata": {},
                    "append_workflow_processes": [],
                    "overwrite_existing": True,
                    "scope": {},
                }
            ],
        }
        fake_response = SimpleNamespace(text=json.dumps(llm_payload, ensure_ascii=False))

        with patch.object(business_rules, "_make_genai_client", return_value=MagicMock()):
            with patch.object(
                business_rules,
                "generate_content_with_openrouter_fallback",
                return_value=fake_response,
            ):
                structured = business_rules._normalize_structured_rule(
                    business_rules.parse_rule_text_to_structured(
                        "All invoices, bills, and receipts from tank stations, fuel stations, petrol stations, or gas stations for diesel, petrol, gasoline, or tanking should be booked to travelling expenses account. Particulars are fuel or tanking."
                    )
                )

        directives = structured.get("post_extraction_directives") if isinstance(structured.get("post_extraction_directives"), list) else []
        self.assertGreaterEqual(len(directives), 1)
        self.assertEqual(str(directives[0].get("target_scope") or ""), "entity")
        set_attributes = directives[0].get("set_attributes") if isinstance(directives[0].get("set_attributes"), dict) else {}
        self.assertEqual(set_attributes.get("booking_debit_account_name"), "travelling expenses")
        self.assertEqual(set_attributes.get("booking_particulars"), "fuel or tanking")

    def test_create_business_rule_rejects_non_actionable_or_document_scope_booking_keys(self):
        with self.assertRaisesRegex(ValueError, "document-scope set_attributes"):
            business_rules._raise_for_non_actionable_rule(
                {
                    "warning_count": 1,
                    "warnings": [
                        {
                            "type": "document_scope_set_attributes",
                            "keys": ["legal_entity_ref"],
                        }
                    ],
                },
                operation="create",
            )

    def test_booking_reference_phrase_is_repaired_to_actionable_company_fields(self):
        with patch.object(business_rules, "_make_genai_client", return_value=None):
            draft = business_rules.simulate_draft_business_rule(
                rule_text="For invoices, bills, and receipts, set booking reference to Seveco AG.",
                rule_name="seveco_booking",
                application_mode="selected_only",
                context={"document_type": "invoice", "operation": "ingest"},
                sample_attributes={"issuer_name": "Any Vendor"},
            )

        directives = draft.get("structured_rule", {}).get("post_extraction_directives") or []
        self.assertGreaterEqual(len(directives), 1)
        first = directives[0]
        self.assertEqual(str(first.get("target_scope") or ""), "entity")
        set_attributes = first.get("set_attributes") if isinstance(first.get("set_attributes"), dict) else {}
        self.assertEqual(set_attributes.get("legal_entity_ref"), "Seveco AG")
        self.assertEqual(set_attributes.get("company_ref"), "Seveco AG")
        self.assertEqual((draft.get("actionability") or {}).get("warning_count"), 0)

        with self.assertRaisesRegex(ValueError, "unsupported set_attributes key 'unknown_field'"):
            business_rules._raise_for_non_actionable_rule(
                {
                    "warning_count": 1,
                    "warnings": [
                        {
                            "type": "non_actionable_set_attribute",
                            "attribute": "unknown_field",
                            "suggestions": [],
                        }
                    ],
                },
                operation="create",
            )


if __name__ == "__main__":
    unittest.main()
