import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from contextlib import nullcontext

from interaction import IDMSInteractionTools, InteractionState
from query_engine import ParsedQuery, QueryEngine


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _sql, _params):
        return None

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _FakeCursor(self._rows)


class _StructuredTableCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self._rows)


class _StructuredTableConnection:
    def __init__(self, rows):
        self._rows = rows
        self.cursor_stub = _StructuredTableCursor(rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_stub


class _BroadScopeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _sql, _params):
        return None

    def fetchall(self):
        return list(self._rows)


class _BroadScopeConnection:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _BroadScopeCursor(self._rows)


class TestQueryEngineStyleRegression(unittest.TestCase):
    def _new_engine(self) -> QueryEngine:
        engine = QueryEngine.__new__(QueryEngine)
        engine.attr_embedding_index = None
        engine.genai_client = None
        return engine

    def test_attribute_query_parsing(self):
        engine = self._new_engine()
        parsed = engine.parse_query("What is the eori number of Aphotonix GmbH?")

        self.assertEqual(parsed.intent, "attribute_lookup")
        self.assertIn(parsed.attribute_name, {"eori_no", "eori_number"})
        self.assertEqual(parsed.entity_name, "Aphotonix GmbH")

    def test_table_cell_query_parsing(self):
        engine = self._new_engine()
        parsed = engine.parse_query("What is the travel expense in August?")

        self.assertEqual(parsed.intent, "attribute_lookup")
        self.assertEqual(parsed.entity_name, "travel expense")
        self.assertIn(parsed.attribute_name, {"august", "aug"})
        self.assertIsInstance(parsed.criteria, dict)
        self.assertTrue(parsed.criteria.get("table_request"))
        self.assertEqual(parsed.criteria.get("table_column"), "August")

    def test_entity_directory_query_parsing(self):
        engine = self._new_engine()
        parsed = engine.parse_query("Generate me a list of suppliers with their full addresses and contact persons from Italy")

        self.assertEqual(parsed.intent, "criteria_lookup")
        self.assertEqual(parsed.criteria.get("entity_type"), "supplier")
        self.assertEqual(parsed.criteria.get("response_format"), "entity_directory")
        self.assertIn("Italy", parsed.criteria.get("must_contain", []))

    def test_structured_table_attribute_lookup(self):
        engine = self._new_engine()
        metadata = {
            "structured_tables": {
                "table_count": 1,
                "source_kind": "spreadsheet",
                "tables": [
                    {
                        "table_index": 1,
                        "table_kind": "row_records",
                        "normalized_headers": ["expense", "august", "september"],
                        "records": [
                            {"expense": "travel expense", "august": "1200", "september": "900", "row_index": 1},
                            {"expense": "office expense", "august": "300", "september": "400", "row_index": 2},
                        ],
                    }
                ],
            }
        }
        engine._get_connection = lambda: _StructuredTableConnection([
            (1, "Operations Sheet", "ops.xlsx", metadata),
        ])
        engine._resolve_entity_name_for_lookup_details = lambda *_args, **_kwargs: None
        engine.sql_exact_attribute_lookup = lambda *_args, **_kwargs: None
        engine._sql_generic_relationship_lookup = lambda *_args, **_kwargs: None

        result = engine.lookup_attribute(attribute_name="August", entity_name="travel expense")

        self.assertIsNotNone(result)
        self.assertEqual(result.get("attribute_value"), "1200")
        self.assertEqual(result.get("doc_name"), "Operations Sheet")
        self.assertEqual(result.get("entity_name"), "travel expense")

    def test_entity_directory_answer_format(self):
        engine = self._new_engine()

        def _lookup_attribute(attribute_name, entity_name, relation_name=None, scope=None, criteria=None):
            lookup = {
                ("billing_address", "Acme Supply"): {"attribute_value": "Via Roma 10, Milano, Italy"},
                ("shipping_address", "Acme Supply"): {"attribute_value": "Via Roma 10, Milano, Italy"},
                ("contact_person_ref", "Acme Supply"): {"attribute_value": "Maria Rossi"},
                ("account_manager_ref", "Acme Supply"): {"attribute_value": "Luca Bianchi"},
                ("billing_address", "Beta Supply"): {"attribute_value": "Corso Italia 5, Torino, Italy"},
                ("shipping_address", "Beta Supply"): {"attribute_value": "Corso Italia 5, Torino, Italy"},
                ("contact_person_ref", "Beta Supply"): {"attribute_value": "Giulia Verdi"},
            }
            return lookup.get((attribute_name, entity_name))

        engine.lookup_attribute = _lookup_attribute
        engine.qdrant_candidates = lambda _question, limit=8: []
        engine.discovery_candidates = lambda _question, limit=8: []
        engine.search_by_criteria = lambda **_kwargs: {
            "criteria": {
                "entity_type": "supplier",
                "response_format": "entity_directory",
                "country": "Italy",
            },
            "count": 2,
            "matches": [
                {"entity_name": "Acme Supply", "entity_type": "supplier", "score": 2.0},
                {"entity_name": "Beta Supply", "entity_type": "supplier", "score": 1.5},
            ],
            "source": "criteria_sql",
        }

        result = engine.answer("Generate me a list of suppliers with their full addresses and contact persons from Italy")

        self.assertEqual(result.get("intent"), "criteria_lookup")
        self.assertEqual(result.get("source"), "criteria_sql")
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].get("entity_name"), "Acme Supply")
        self.assertEqual(rows[0].get("filter_value"), "Italy")
        attrs = rows[0].get("attributes") if isinstance(rows[0].get("attributes"), dict) else {}
        self.assertEqual(attrs.get("billing_address"), "Via Roma 10, Milano, Italy")
        self.assertIn("Maria Rossi", attrs.values())
        self.assertIn("Entity directory:", result.get("answer", ""))

    def test_criteria_query_parsing(self):
        engine = self._new_engine()
        parsed = engine.parse_query("List all employees with backend skills.")

        self.assertEqual(parsed.intent, "criteria_lookup")
        self.assertIsInstance(parsed.criteria, dict)
        self.assertEqual(parsed.criteria.get("entity_type"), "employee")
        self.assertIn("backend", parsed.criteria.get("must_contain", []))

    def test_contact_person_details_query_parsing(self):
        engine = self._new_engine()
        parsed = engine.parse_query("Show me the details of the contact persons of Carjan bv")

        self.assertEqual(parsed.intent, "attribute_lookup")
        self.assertEqual(parsed.entity_name, "Carjan bv")
        self.assertEqual(parsed.attribute_name, "contact_person")
        self.assertTrue((parsed.criteria or {}).get("broad_scope"))
        self.assertEqual((parsed.criteria or {}).get("parse_method"), "contact_person_details_gate")

    def test_contact_person_contact_detail_attributes_are_extracted(self):
        engine = self._new_engine()
        parsed = engine.parse_query("Show me the email and telephone number of the contact persons of Carjan bv")

        self.assertEqual(parsed.intent, "attribute_lookup")
        self.assertEqual(parsed.entity_name, "Carjan bv")
        self.assertEqual(parsed.attribute_name, "contact_person")
        self.assertEqual(parsed.relation_name, "contact_person")
        self.assertEqual(parsed.attribute_names, ["email", "telephone"])
        self.assertEqual((parsed.criteria or {}).get("parse_method"), "contact_person_details_gate")

    def test_broad_relation_details_query_expands_related_attribute_hints(self):
        engine = self._new_engine()
        hints = engine._related_attribute_hints_from_text(
            "Show me the details of the contact persons of Carjan bv",
            "contact_person",
        )

        self.assertIn("email", hints)
        self.assertTrue(any(item in hints for item in ["phone", "telephone"]))

    def test_broad_scope_relationship_lookup_keeps_related_object_attributes(self):
        engine = self._new_engine()
        connection = _BroadScopeConnection([
            ("Carjan bv", 42, "Mr. Jan Carjan", "Person", "contact_person_of", "", "", "", "", "", "", 0, "", ""),
        ])
        engine._related_object_attribute_map = lambda _conn, _ids, max_attrs_per_object=12: {
            42: {"email": "jan@carjan.example", "phone": "+41 44 123 45 67", "city": "Zurich"}
        }

        result = engine._sql_generic_relationship_lookup(connection, "Carjan bv", "contact_person", broad_scope=True)

        self.assertIsNotNone(result)
        value = result.get("attribute_value")
        self.assertIsInstance(value, list)
        self.assertEqual(value[0].get("email"), "jan@carjan.example")
        self.assertEqual(value[0].get("phone"), "+41 44 123 45 67")
        self.assertEqual(value[0].get("city"), "Zurich")

    def test_lookup_attribute_uses_relationship_context_for_related_contact_details(self):
        engine = self._new_engine()
        connection = _BroadScopeConnection([])
        engine._get_connection = lambda: connection
        engine._resolve_entity_name_for_lookup_details = lambda *_args, **_kwargs: None
        engine.sql_exact_attribute_lookup = lambda *_args, **_kwargs: None
        engine._lookup_related_attribute_via_relationship = lambda *_args, **_kwargs: None
        engine._lookup_attribute_in_structured_tables = lambda *_args, **_kwargs: None
        engine._match_entity_relationship_name = lambda *_args, **_kwargs: None
        engine.sql_fallback_from_ids = lambda *_args, **_kwargs: None
        engine._sql_generic_relationship_lookup = lambda _conn, _entity_name, relationship_concept, broad_scope=False: {
            "entity_name": "Carjan bv",
            "attribute_name": relationship_concept,
            "attribute_value": [{"name": "Mr. Jan Carjan", "email": "jan@carjan.example", "phone": "+41 44 123 45 67"}],
            "relationship_concept": relationship_concept,
        }

        result = engine.lookup_attribute(
            attribute_name="email",
            entity_name="Carjan bv",
            relation_name="contact_person",
            criteria={"broad_scope": True},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.get("attribute_name"), "email")
        self.assertEqual(result.get("attribute_value"), "jan@carjan.example")

    def test_answer_for_contact_person_multi_attribute_request_uses_explicit_attributes_only(self):
        engine = self._new_engine()
        parsed = ParsedQuery(
            intent="attribute_lookup",
            entity_name="Carjan bv",
            attribute_name="contact_person",
            relation_name="contact_person",
            confidence=0.9,
            language="en",
            criteria={"parse_method": "contact_person_details_gate", "relationship_concept": "contact_person", "broad_scope": True},
            attribute_names=["email", "telephone"],
        )
        engine.parse_query = lambda _question: parsed
        engine._normalize_parsed_for_execution = lambda parsed_query, _question: parsed_query
        engine._decompose_multi_question = lambda _question: []
        engine._try_federated_sql_read = lambda _question: None
        engine._try_fuel_station_invoice_answer = lambda _question: None
        engine._try_accounting_travel_expense_total_answer = lambda _question, _parsed: None
        engine._looks_like_travel_expense_total_question = lambda _question: False
        engine._extract_table_cell_query = lambda *_args, **_kwargs: None
        engine._build_match_telemetry = lambda *_args, **_kwargs: {}
        engine._attribute_filters_from_criteria = lambda _criteria: []
        engine._has_broad_attribute_scope_cue = lambda _question: True
        engine._has_file_info_cue = lambda _question: False
        engine.qdrant_candidates = lambda _question: []
        engine.discovery_candidates = lambda _question: []
        engine._unique_ints = lambda values: [item for item in values if item is not None]
        engine._resolve_entity_name_for_lookup_details = lambda *_args, **_kwargs: None
        engine._resolve_entity_name_for_lookup = lambda *_args, **_kwargs: "Carjan bv"
        engine._is_full_profile_request = lambda _question: False
        engine._is_value_document_context_question = lambda _question: False
        engine._expand_query_attribute_candidates = lambda **_kwargs: ["phone", "contact_email"]
        engine._related_attribute_hints_from_text = lambda _question, _relation_name: ["phone", "contact_email"]
        engine._get_attribute_display_name = lambda attr_name, _language: attr_name
        engine._repair_mojibake_value = lambda value: value
        engine._format_attribute_value_for_answer = lambda value: str(value)
        engine._try_learn_pattern = lambda *_args, **_kwargs: None
        engine._derived_answer_from_pattern_matches = lambda *_args, **_kwargs: None
        engine._derived_clause_diagnostics = lambda *_args, **_kwargs: None
        engine._requires_contextual_value_resolution = lambda **_kwargs: False
        engine._result_satisfies_attribute_filters = lambda *_args, **_kwargs: True
        engine._get_connection = lambda: _BroadScopeConnection([])
        engine.sql_exact_attribute_lookup = lambda *_args, **_kwargs: None
        engine._lookup_related_attribute_via_relationship = lambda *_args, **_kwargs: None
        engine._lookup_attribute_in_structured_tables = lambda *_args, **_kwargs: None
        engine._match_entity_relationship_name = lambda *_args, **_kwargs: None
        engine.sql_fallback_from_ids = lambda *_args, **_kwargs: None
        engine._lookup_attribute_from_synchronized_source = lambda *_args, **_kwargs: None

        def _lookup(attr_name, _entity_name, relation_name=None, criteria=None):
            if attr_name == "email":
                return {"entity_name": "Carjan bv", "attribute_name": "email", "attribute_value": "factuur@carjan.nl"}
            if attr_name == "telephone":
                return {"entity_name": "Carjan bv", "attribute_name": "telephone", "attribute_value": "+31 6 19675414"}
            if attr_name == "phone":
                return {"entity_name": "Carjan bv", "attribute_name": "phone", "attribute_value": "+31 6 19675414"}
            if attr_name == "contact_email":
                return {"entity_name": "Carjan bv", "attribute_name": "contact_email", "attribute_value": None}
            return None

        engine.lookup_attribute = _lookup

        result = engine.answer("Show me the email and telephone number of the contact persons of Carjan bv")
        answer_text = str(result.get("answer") or "")

        self.assertIn("email", answer_text)
        self.assertIn("telephone", answer_text)
        self.assertNotIn("None found", answer_text)

    def test_lookup_attribute_backtracks_across_alternative_relation_routes(self):
        engine = self._new_engine()
        connection = _BroadScopeConnection([])
        engine._get_connection = lambda: connection
        engine._resolve_entity_name_for_lookup_details = lambda *_args, **_kwargs: None
        engine.sql_exact_attribute_lookup = lambda *_args, **_kwargs: None
        engine._lookup_related_attribute_via_relationship = lambda *_args, **_kwargs: None
        engine._lookup_attribute_in_structured_tables = lambda *_args, **_kwargs: None
        engine._match_entity_relationship_name = lambda *_args, **_kwargs: None
        engine.sql_fallback_from_ids = lambda *_args, **_kwargs: None
        engine._lookup_attribute_from_synchronized_source = lambda *_args, **_kwargs: None

        attempts = []

        def _generic_lookup(_conn, _entity_name, relationship_concept, broad_scope=False):
            attempts.append(relationship_concept)
            if relationship_concept == "contact_person":
                return None
            if relationship_concept == "contact":
                return {
                    "entity_name": "Carjan bv",
                    "attribute_name": "contact",
                    "attribute_value": [{"name": "Mr. Jan Carjan", "email": "jan@carjan.example"}],
                    "relationship_concept": relationship_concept,
                }
            return None

        engine._sql_generic_relationship_lookup = _generic_lookup

        result = engine.lookup_attribute(
            attribute_name="email",
            entity_name="Carjan bv",
            relation_name="contact_person",
            criteria={"broad_scope": True, "strict_relation_lookup": True},
        )

        self.assertEqual(attempts[:2], ["contact_person", "contact"])
        self.assertEqual(result.get("attribute_value"), "jan@carjan.example")

    def test_lookup_attribute_uses_synchronized_source_for_generic_relation_attribute(self):
        engine = self._new_engine()
        connection = _BroadScopeConnection([])
        engine._get_connection = lambda: connection
        engine._resolve_entity_name_for_lookup_details = lambda *_args, **_kwargs: None
        engine.sql_exact_attribute_lookup = lambda *_args, **_kwargs: None
        engine._lookup_related_attribute_via_relationship = lambda *_args, **_kwargs: None
        engine._lookup_attribute_in_structured_tables = lambda *_args, **_kwargs: None
        engine._match_entity_relationship_name = lambda *_args, **_kwargs: None
        engine.sql_fallback_from_ids = lambda *_args, **_kwargs: None

        def _synchronized_lookup(_conn, _entity_name, attribute_name, relation_name=None):
            if attribute_name == "city" and relation_name == "contact_person":
                return {
                    "entity_name": "Carjan bv",
                    "attribute_name": "city",
                    "attribute_value": "Zurich",
                    "source": "synchronized_source",
                }
            return None

        engine._lookup_attribute_from_synchronized_source = _synchronized_lookup
        engine._sql_generic_relationship_lookup = lambda *_args, **_kwargs: None

        result = engine.lookup_attribute(
            attribute_name="city",
            entity_name="Carjan bv",
            relation_name="contact_person",
            criteria={"broad_scope": True, "strict_relation_lookup": True},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.get("attribute_value"), "Zurich")
        self.assertEqual(result.get("source"), "synchronized_source")

    def test_lookup_attribute_prefers_relationship_payload_for_contact_person_name(self):
        engine = self._new_engine()
        connection = _BroadScopeConnection([])
        engine._get_connection = lambda: connection
        engine._resolve_entity_name_for_lookup_details = lambda *_args, **_kwargs: None
        engine.sql_exact_attribute_lookup = lambda *_args, **_kwargs: None
        engine._lookup_related_attribute_via_relationship = lambda *_args, **_kwargs: None
        engine._lookup_attribute_in_structured_tables = lambda *_args, **_kwargs: None
        engine._match_entity_relationship_name = lambda *_args, **_kwargs: None
        engine.sql_fallback_from_ids = lambda *_args, **_kwargs: None

        def _generic_lookup(_conn, _entity_name, relationship_concept, broad_scope=False):
            if relationship_concept != "contact_person":
                return None
            return {
                "entity_name": "Carjan bv",
                "attribute_name": relationship_concept,
                "attribute_value": [{"name": "Mr. Jan Carjan", "email": "jan@carjan.example", "phone": "+41 44 123 45 67"}],
                "relationship_concept": relationship_concept,
            }

        def _synchronized_lookup(_conn, _entity_name, attribute_name, relation_name=None):
            return {
                "entity_name": "Carjan bv",
                "attribute_name": attribute_name,
                "attribute_value": "Carjan bv",
                "source": "synchronized_source",
            }

        engine._sql_generic_relationship_lookup = _generic_lookup
        engine._lookup_attribute_from_synchronized_source = _synchronized_lookup

        result = engine.lookup_attribute(
            attribute_name="contact_person",
            entity_name="Carjan bv",
            relation_name="contact_person",
            criteria={"broad_scope": True, "strict_relation_lookup": True},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.get("attribute_value"), "Mr. Jan Carjan")
        self.assertNotEqual(result.get("attribute_value"), "Carjan bv")

    def test_lookup_attribute_uses_relation_name_for_contact_person_email_and_telephone(self):
        engine = self._new_engine()
        connection = _BroadScopeConnection([])
        engine._get_connection = lambda: connection
        engine._resolve_entity_name_for_lookup_details = lambda *_args, **_kwargs: None
        engine.sql_exact_attribute_lookup = lambda *_args, **_kwargs: None
        engine._lookup_related_attribute_via_relationship = lambda *_args, **_kwargs: None
        engine._lookup_attribute_in_structured_tables = lambda *_args, **_kwargs: None
        engine._match_entity_relationship_name = lambda *_args, **_kwargs: None
        engine.sql_fallback_from_ids = lambda *_args, **_kwargs: None
        engine._lookup_attribute_from_synchronized_source = lambda *_args, **_kwargs: None

        def _generic_lookup(_conn, _entity_name, relationship_concept, broad_scope=False):
            if relationship_concept != "contact_person":
                return None
            return {
                "entity_name": "Carjan bv",
                "attribute_name": relationship_concept,
                "attribute_value": [{"name": "Mr. Jan Carjan", "email": "jan@carjan.example", "phone": "+41 44 123 45 67"}],
                "relationship_concept": relationship_concept,
            }

        engine._sql_generic_relationship_lookup = _generic_lookup

        email_result = engine.lookup_attribute(
            attribute_name="email",
            entity_name="Carjan bv",
            relation_name="contact_person",
            criteria={"broad_scope": True},
        )
        telephone_result = engine.lookup_attribute(
            attribute_name="telephone",
            entity_name="Carjan bv",
            relation_name="contact_person",
            criteria={"broad_scope": True},
        )

        self.assertEqual(email_result.get("attribute_value"), "jan@carjan.example")
        self.assertEqual(telephone_result.get("attribute_value"), "+41 44 123 45 67")

    def test_lookup_attribute_falls_back_to_synchronized_source_mapping(self):
        engine = self._new_engine()

        class _FederatedSourceCursor:
            def __init__(self):
                self._rows = []
                self._executed = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params):
                self._executed.append((sql, params))
                lowered = " ".join(str(sql or "").split()).lower()
                if "from object_instance" in lowered and (
                    "where lower(coalesce(object_name, ''))" in lowered
                    or "where lower(object_name)" in lowered
                ):
                    self._rows = [(7, "Carjan bv", "company")]
                elif "from source_entity_mapping" in lowered:
                    self._rows = [
                        (
                            1,
                            "public",
                            "customer",
                            "1001",
                            "company",
                            {"source_row": {"contact_name": "Mr. Jan Carjan", "telephone_no": "079 123 45 67"}},
                            "row-hash",
                            "source-name",
                        )
                    ]
                else:
                    self._rows = []

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return list(self._rows)

        class _FederatedSourceConnection:
            def __init__(self):
                self.cursor_stub = _FederatedSourceCursor()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return self.cursor_stub

        connection = _FederatedSourceConnection()
        engine._get_connection = lambda: connection
        engine._resolve_entity_name_for_lookup_details = lambda *_args, **_kwargs: None
        engine.sql_exact_attribute_lookup = lambda *_args, **_kwargs: None
        engine._sql_generic_relationship_lookup = lambda *_args, **_kwargs: None
        engine._lookup_related_attribute_via_relationship = lambda *_args, **_kwargs: None
        engine._lookup_attribute_in_structured_tables = lambda *_args, **_kwargs: None
        engine._match_entity_relationship_name = lambda *_args, **_kwargs: None
        engine.sql_fallback_from_ids = lambda *_args, **_kwargs: None

        result = engine.lookup_attribute(attribute_name="telephone", entity_name="Carjan bv")

        self.assertIsNotNone(result)
        self.assertEqual(result.get("attribute_name"), "telephone")
        self.assertEqual(result.get("attribute_value"), "079 123 45 67")
        self.assertEqual(result.get("source"), "synchronized_source")

    def test_generic_process_contact_query_parsing(self):
        engine = self._new_engine()
        parsed = engine.parse_query("What are the name and email address of the customs contact person for Acme GmbH?")

        self.assertEqual(parsed.intent, "attribute_lookup")
        self.assertEqual(parsed.entity_name, "Acme GmbH")
        self.assertEqual(parsed.attribute_name, "process_contact_email")
        self.assertEqual(parsed.relation_name, "customs")
        self.assertEqual(parsed.attribute_names, ["process_contact_email", "process_contact_person"])
        self.assertEqual((parsed.criteria or {}).get("parse_method"), "process_contact_gate")

    def test_generic_process_responsible_company_query_parsing(self):
        engine = self._new_engine()
        parsed = engine.parse_query("What company is responsible for the customs application for Acme GmbH?")

        self.assertEqual(parsed.intent, "attribute_lookup")
        self.assertEqual(parsed.entity_name, "Acme GmbH")
        self.assertEqual(parsed.attribute_name, "responsible_company")
        self.assertEqual(parsed.relation_name, "customs")
        self.assertEqual(parsed.attribute_names, ["responsible_company", "process_contact_person", "process_contact_email"])
        self.assertEqual((parsed.criteria or {}).get("parse_method"), "process_actor_gate")

    def test_generic_process_responsible_company_vat_query_parsing(self):
        engine = self._new_engine()
        parsed = engine.parse_query("Which company handled the VAT registration for Acme GmbH?")

        self.assertEqual(parsed.intent, "attribute_lookup")
        self.assertEqual(parsed.entity_name, "Acme GmbH")
        self.assertEqual(parsed.attribute_name, "responsible_company")
        self.assertEqual(parsed.relation_name, "vat")
        self.assertEqual(parsed.attribute_names, ["responsible_company", "process_contact_person", "process_contact_email"])
        self.assertEqual((parsed.criteria or {}).get("parse_method"), "process_actor_gate")

    def test_semantic_query_parsing(self):
        engine = self._new_engine()
        parsed = engine.parse_query("Tell me about companies active in laser systems")

        self.assertEqual(parsed.intent, "semantic_lookup")

    def test_german_query_language_detection(self):
        engine = self._new_engine()
        parsed = engine.parse_query("Wie lautet das Statutendatum im Handelsregisterauszug der Aphotonix GmbH?")

        self.assertEqual(parsed.language, "de")
        self.assertIn("Aphotonix GmbH", parsed.entity_name)

    def test_file_info_intent_prefers_relation_entity_over_short_hint(self):
        engine = self._new_engine()
        # Simulate a weak fallback hint that would otherwise collapse to surname only.
        engine._best_entity_hint = lambda _text: "Pang"

        parsed = engine.parse_query(
            "Which document mentioned Benjamin Pang? Please return the file name with the full path."
        )

        self.assertEqual(parsed.intent, "attribute_lookup")
        self.assertEqual(parsed.attribute_name, "document_file_info")
        self.assertEqual(parsed.entity_name, "Benjamin Pang")

    def test_extract_entity_for_file_info_intent_captures_full_person_name(self):
        engine = self._new_engine()
        entity = engine._extract_entity_for_file_info_intent(
            "Which document mentioned Benjamin Pang? Please return the file name with the full path."
        )
        self.assertEqual(entity, "Benjamin Pang")

    def test_select_person_candidate_by_token_alignment_prefers_given_name_match(self):
        engine = self._new_engine()
        chosen = engine._select_person_candidate_by_token_alignment(
            "Benjamin Pang",
            ["Chung Yeung Pang", "Benjamin Kin Sing Pang", "Kaethi Johanna Pang Gerber"],
        )
        self.assertEqual(chosen, "Benjamin Kin Sing Pang")

    def test_select_person_candidate_by_token_alignment_returns_none_on_tie(self):
        engine = self._new_engine()
        chosen = engine._select_person_candidate_by_token_alignment(
            "B Pang",
            ["Benjamin Pang", "Bernhard Pang"],
        )
        self.assertIsNone(chosen)

    def test_resolve_document_full_path_prefers_absolute_source_reference(self):
        engine = self._new_engine()
        metadata = {
            "user_metadata": {
                "client_file_path": "Aphotonix_registration.pdf",
                "source_reference": {
                    "original_local_path": "Aphotonix_registration.pdf",
                    "gcs_uri": r"C:\\Users\\pang\\AppData\\Local\\Temp\\idms-upload-zjbg490o.pdf",
                },
            },
            "markdown_path": r"C:\\Project\\WebTech\\python\\IDMS-Demo\\generated\\markdown\\document-6.md",
        }
        full_path = engine._resolve_document_full_path("Aphotonix_registration.pdf", metadata)
        self.assertEqual(full_path, r"C:\\Users\\pang\\AppData\\Local\\Temp\\idms-upload-zjbg490o.pdf")

    def test_compose_document_file_identity_uses_resolved_full_path(self):
        engine = self._new_engine()
        metadata = {
            "user_metadata": {
                "client_file_name": "Aphotonix_registration.pdf",
                "source_reference": {
                    "gcs_uri": r"C:\\Users\\pang\\AppData\\Local\\Temp\\idms-upload-zjbg490o.pdf",
                },
            }
        }
        file_name, full_path = engine._compose_document_file_identity(
            doc_name="Aphotonix_registration.pdf",
            doc_key="aphotonix_registration_pdf",
            doc_path="Aphotonix_registration.pdf",
            metadata=metadata,
        )
        self.assertEqual(file_name, "Aphotonix_registration.pdf")
        self.assertEqual(full_path, r"C:\\Users\\pang\\AppData\\Local\\Temp\\idms-upload-zjbg490o.pdf")

    def test_criteria_answer_path_returns_ranked_matches(self):
        engine = self._new_engine()

        rows = [
            (
                1,
                "Aphotonix GmbH",
                "company",
                {"summary": "sales engineering services for laser systems"},
                "description",
                {"value": "sales, engineering and services for laser systems"},
                3,
                "Aphotonix_registration.pdf",
                r"C:\\Seveco\\Aphotonix\\Aphotonix_registration.pdf",
                "2026-01-01",
            ),
            (
                2,
                "Other GmbH",
                "company",
                {"summary": "consulting only"},
                "description",
                {"value": "consulting"},
                4,
                "other.pdf",
                r"C:\\Seveco\\Other\\other.pdf",
                "2026-01-02",
            ),
        ]

        engine._get_connection = lambda: _FakeConnection(rows)
        engine.qdrant_candidates = lambda _question, limit=8: []
        engine.discovery_candidates = lambda _question, limit=8: []

        result = engine.answer("Which company provides sales, engineering and services for laser systems?")

        self.assertEqual(result.get("intent"), "criteria_lookup")
        self.assertIn(result.get("source"), {"criteria_sql", "criteria_semantic_text"})
        self.assertIsInstance(result.get("data"), dict)
        self.assertGreaterEqual(result["data"].get("count", 0), 1)
        self.assertEqual(result["data"]["matches"][0]["entity_name"], "Aphotonix GmbH")

    def test_lookup_attribute_routes_legacy_eori_contact_person_via_generic_process_lookup(self):
        engine = self._new_engine()
        engine._resolve_schema_term = lambda attribute_name, entity_name=None: (attribute_name, "attribute")
        engine._normalize_attribute_name = lambda raw: raw
        engine._resolve_relationship_name = lambda relation_name, entity_name=None: relation_name
        engine._unique_ints = lambda values: []
        engine._temporal_scope_from_criteria = lambda criteria: "current"
        engine._resolve_entity_name_for_lookup_details = lambda conn, entity_name: {"resolved_name": entity_name}
        engine._is_ownership_lookup_intent = lambda **kwargs: False
        engine._sql_attribute_lookup_with_relation_filters = lambda *args, **kwargs: None
        engine._sql_process_contact_lookup = lambda conn, entity_name, process_concept: {
            "entity_name": entity_name,
            "attribute_name": "process_contact_person",
            "attribute_value": "Daniel Trottmann",
            "contact_email": "services@ynoves.ch",
            "relationship_name": "eori_contact_person",
            "doc_id": 1,
            "doc_name": "German-EORI-2026",
            "doc_path": "C:/docs/eori.pdf",
        }
        engine._get_connection = lambda: nullcontext(object())

        result = engine.lookup_attribute(
            attribute_name="eori_contact_person",
            entity_name="Aphotonix GmbH",
            relation_name="eori_contact_person_of",
            criteria={
                "relation_filters": [
                    {"relationship_names": ["eori_contact_person_of"], "target_name": "Aphotonix GmbH"}
                ]
            },
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.get("attribute_name"), "eori_contact_person")
        self.assertEqual(result.get("attribute_value"), "Daniel Trottmann")
        self.assertEqual(result.get("contact_email"), "services@ynoves.ch")

    def test_answer_returns_composite_eori_contact_name_and_email(self):
        engine = self._new_engine()
        parsed = ParsedQuery(
            intent="attribute_lookup",
            entity_name="Aphotonix GmbH",
            attribute_name="eori_contact_person",
            attribute_names=["eori_contact_person", "eori_contact_email"],
            relation_name="eori_contact_person_of",
            confidence=0.9,
            language="de",
            criteria={
                "parse_method": "llm_structured_plan",
                "relation_filters": [
                    {"relationship_names": ["eori_contact_person_of"], "target_name": "Aphotonix GmbH"}
                ],
            },
        )
        engine.parse_query = lambda _question: parsed
        engine._normalize_parsed_for_execution = lambda parsed_query, _question: parsed_query
        engine._is_value_document_context_question = lambda _question: False
        engine._extract_table_cell_query = lambda *_args, **_kwargs: None
        engine._build_match_telemetry = lambda parsed_query: {"intent": parsed_query.intent}
        engine.qdrant_candidates = lambda _question: []
        engine.discovery_candidates = lambda _question: []
        engine._get_connection = lambda: nullcontext(object())
        engine._infer_temporal_scope_from_question = lambda _question: "current"
        engine._has_broad_attribute_scope_cue = lambda _question: False
        engine._has_file_info_cue = lambda _question: False
        engine._try_learn_pattern = lambda *args, **kwargs: None
        engine._attach_match_telemetry = lambda data, telemetry: data
        engine._derived_answer_from_pattern_matches = lambda parsed_query, matches: None
        engine._derived_clause_diagnostics = lambda parsed_query, matches: None
        engine._criteria_question_prefers_listing = lambda question, response_format: False
        engine.lookup_attribute = lambda attr, entity_name, relation_name=None, criteria=None: {
            "entity_name": entity_name,
            "attribute_name": attr,
            "attribute_value": "Daniel Trottmann" if attr == "eori_contact_person" else "services@ynoves.ch",
        }

        result = engine.answer("Wie lauten der Name und die E-Mail-Adresse des Kontakt für EORI-Ansprechpartner von Aphotonix GmbH?")

        self.assertEqual(result.get("source"), "sql_exact_composite")
        answer = result.get("answer", "")
        self.assertIn("Daniel Trottmann", answer)
        self.assertIn("services@ynoves.ch", answer)

    def test_answer_returns_composite_generic_process_contact_name_and_email(self):
        engine = self._new_engine()
        parsed = ParsedQuery(
            intent="attribute_lookup",
            entity_name="Acme GmbH",
            attribute_name="process_contact_email",
            attribute_names=["process_contact_email", "process_contact_person"],
            relation_name="customs",
            confidence=0.9,
            language="en",
            criteria={
                "parse_method": "process_contact_gate",
                "process_concept": "customs",
            },
        )
        engine.parse_query = lambda _question: parsed
        engine._normalize_parsed_for_execution = lambda parsed_query, _question: parsed_query
        engine._is_value_document_context_question = lambda _question: False
        engine._extract_table_cell_query = lambda *_args, **_kwargs: None
        engine._build_match_telemetry = lambda parsed_query: {"intent": parsed_query.intent}
        engine.qdrant_candidates = lambda _question: []
        engine.discovery_candidates = lambda _question: []
        engine._get_connection = lambda: nullcontext(object())
        engine._infer_temporal_scope_from_question = lambda _question: "current"
        engine._has_broad_attribute_scope_cue = lambda _question: False
        engine._has_file_info_cue = lambda _question: False
        engine._try_learn_pattern = lambda *args, **kwargs: None
        engine._attach_match_telemetry = lambda data, telemetry: data
        engine._derived_answer_from_pattern_matches = lambda parsed_query, matches: None
        engine._derived_clause_diagnostics = lambda parsed_query, matches: None
        engine._criteria_question_prefers_listing = lambda question, response_format: False
        engine.lookup_attribute = lambda attr, entity_name, relation_name=None, criteria=None: {
            "entity_name": entity_name,
            "attribute_name": attr,
            "attribute_value": "Maria Keller" if attr == "process_contact_person" else "maria.keller@acme.example",
        }

        result = engine.answer("What are the name and email address of the customs contact person for Acme GmbH?")

        self.assertEqual(result.get("source"), "sql_exact_composite")
        answer = result.get("answer", "")
        self.assertIn("Maria Keller", answer)
        self.assertIn("maria.keller@acme.example", answer)

    def test_answer_returns_composite_generic_process_responsible_company(self):
        engine = self._new_engine()
        parsed = ParsedQuery(
            intent="attribute_lookup",
            entity_name="Acme GmbH",
            attribute_name="responsible_company",
            attribute_names=["responsible_company", "process_contact_person", "process_contact_email"],
            relation_name="customs",
            confidence=0.9,
            language="en",
            criteria={
                "parse_method": "process_actor_gate",
                "process_concept": "customs",
            },
        )
        engine.parse_query = lambda _question: parsed
        engine._normalize_parsed_for_execution = lambda parsed_query, _question: parsed_query
        engine._is_value_document_context_question = lambda _question: False
        engine._extract_table_cell_query = lambda *_args, **_kwargs: None
        engine._build_match_telemetry = lambda parsed_query: {"intent": parsed_query.intent}
        engine.qdrant_candidates = lambda _question: []
        engine.discovery_candidates = lambda _question: []
        engine._get_connection = lambda: nullcontext(object())
        engine._infer_temporal_scope_from_question = lambda _question: "current"
        engine._has_broad_attribute_scope_cue = lambda _question: False
        engine._has_file_info_cue = lambda _question: False
        engine._try_learn_pattern = lambda *args, **kwargs: None
        engine._attach_match_telemetry = lambda data, telemetry: data
        engine._derived_answer_from_pattern_matches = lambda parsed_query, matches: None
        engine._derived_clause_diagnostics = lambda parsed_query, matches: None
        engine._criteria_question_prefers_listing = lambda question, response_format: False
        lookup = {
            "responsible_company": "Ynoves AG",
            "process_contact_person": "Maria Keller",
            "process_contact_email": "maria.keller@acme.example",
        }
        engine.lookup_attribute = lambda attr, entity_name, relation_name=None, criteria=None: {
            "entity_name": entity_name,
            "attribute_name": attr,
            "attribute_value": lookup[attr],
        }

        result = engine.answer("What company is responsible for the customs application for Acme GmbH?")

        self.assertEqual(result.get("source"), "sql_exact_composite")
        answer = result.get("answer", "")
        self.assertIn("Ynoves AG", answer)
        self.assertIn("Maria Keller", answer)
        self.assertIn("maria.keller@acme.example", answer)


class TestInteractionStyleRoutingRegression(unittest.TestCase):
    def _new_tools(self) -> IDMSInteractionTools:
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)
        tools.logger = MagicMock()
        tools.query_cache = None
        tools.state = InteractionState()
        return tools

    def test_query_style_classifies_document_retrieval(self):
        tools = self._new_tools()
        tools._invoke_solf_clause_raw = lambda _clause, _args: {}

        style = tools._classify_query_style(
            "Show me the document for the registration of Aphotonix GmbH",
            {
                "intent": "semantic_lookup",
                "entity_name": "Aphotonix GmbH",
                "attribute_name": None,
                "confidence": 0.4,
            },
        )

        self.assertEqual(style.get("query_style"), "document_retrieval")
        self.assertEqual(style.get("initial_action"), "lookup_attribute")

    def test_autonomous_query_executes_criteria_action(self):
        tools = self._new_tools()

        parsed = ParsedQuery(
            intent="criteria_lookup",
            entity_name=None,
            attribute_name=None,
            confidence=0.86,
            criteria={
                "entity_type": "employee",
                "must_contain": ["backend"],
                "attribute_hints": ["skills", "description"],
            },
        )

        mock_query_engine = SimpleNamespace()
        mock_query_engine.parse_query = lambda _q: parsed
        mock_query_engine.search_by_criteria = lambda **_kwargs: {
            "criteria": parsed.criteria,
            "count": 1,
            "matches": [
                {
                    "object_id": 100,
                    "entity_name": "Max Mustermann",
                    "entity_type": "employee",
                    "matched_terms": ["backend"],
                    "score": 1.0,
                }
            ],
        }
        mock_query_engine.resolve_search_scope = lambda _q, limit=12: {
            "candidate_count": 0,
            "discovery_candidate_count": 0,
            "node_ids": [],
            "edge_ids": [],
            "doc_ids": [],
            "candidates": [],
            "discovery_results": [],
            "nodes": [],
            "edges": [],
            "documents": [],
        }
        mock_query_engine._is_value_document_context_question = lambda _q: False
        mock_query_engine._criteria_question_prefers_listing = lambda _q: False
        tools.query_engine = mock_query_engine

        tools._invoke_solf_clause_raw = lambda _clause, _args: {
            "mode": "allow",
            "reason": "test",
            "query_style": "criteria_list",
            "initial_action": "search_criteria",
            "message": "",
        }

        def _policy(clause_name, _payload):
            if clause_name == "interaction_answer_policy":
                return {"mode": "deny", "message": "stop before LLM"}
            return {"mode": "allow", "message": ""}

        tools._invoke_solf_policy = _policy

        result = tools.autonomous_query(
            "List all employees with backend skills.",
            max_steps=1,
            record_history=False,
        )

        self.assertEqual(result.get("answer_source"), "policy_blocked")
        grounding = result.get("grounding") or {}
        criteria_result = grounding.get("criteria_result") or {}
        self.assertEqual(criteria_result.get("count"), 1)
        self.assertEqual(criteria_result.get("matches", [])[0].get("entity_name"), "Max Mustermann")

    def test_autonomous_query_reroutes_finalize_to_search_criteria_when_missing_result(self):
        tools = self._new_tools()

        parsed = ParsedQuery(
            intent="criteria_lookup",
            entity_name=None,
            attribute_name=None,
            confidence=0.92,
            criteria={
                "entity_type": "document",
                "must_contain": ["travelling"],
                "attribute_hints": ["description", "document"],
            },
        )

        mock_query_engine = SimpleNamespace()
        mock_query_engine.parse_query = lambda _q: parsed
        mock_query_engine._is_value_document_context_question = lambda _q: False
        mock_query_engine._criteria_question_prefers_listing = lambda _q, _rf="": True
        tools.query_engine = mock_query_engine

        tools._planner_step = lambda _question, _state, _step: {
            "action": "finalize",
            "attribute_name": None,
            "entity_name": None,
            "rationale": "planner asked to finalize early",
        }

        tools.search_criteria = MagicMock(
            return_value={
                "success": True,
                "count": 1,
                "matches": [
                    {
                        "entity_name": "2025-12-18_SBB_Kontanz_ticket_forward.pdf",
                        "entity_type": "document",
                        "matched_terms": ["travelling"],
                    }
                ],
                "source": "criteria_scope_docs",
            }
        )

        tools._invoke_solf_clause_raw = lambda _clause, _args: {
            "mode": "allow",
            "reason": "test",
            "query_style": "criteria_list",
            "initial_action": "search_criteria",
            "message": "",
        }

        def _policy(clause_name, _payload):
            if clause_name == "interaction_answer_policy":
                return {"mode": "deny", "message": "stop before LLM"}
            return {"mode": "allow", "message": ""}

        tools._invoke_solf_policy = _policy

        result = tools.autonomous_query(
            "Show me all the documents that are related to travelling",
            max_steps=1,
            record_history=False,
        )

        self.assertEqual(result.get("answer_source"), "policy_blocked")
        tools.search_criteria.assert_called_once()
        grounding = result.get("grounding") if isinstance(result.get("grounding"), dict) else {}
        criteria_result = grounding.get("criteria_result") if isinstance(grounding.get("criteria_result"), dict) else {}
        self.assertEqual(int(criteria_result.get("count") or 0), 1)
        self.assertEqual(str(criteria_result.get("source") or ""), "criteria_scope_docs")


# ---------------------------------------------------------------------------
# Temporal constraint tests
# ---------------------------------------------------------------------------

class TestTemporalParsing(unittest.TestCase):
    def _new_engine(self) -> QueryEngine:
        engine = QueryEngine.__new__(QueryEngine)
        engine.attr_embedding_index = None
        engine.genai_client = None
        return engine

    # --- _extract_time_window ---

    def test_relative_last_N_months(self):
        engine = self._new_engine()
        tw = engine._extract_time_window("Show me the expenses in the last three months")
        self.assertIsNotNone(tw)
        self.assertIsNotNone(tw["date_from"])
        self.assertIsNotNone(tw["date_to"])
        # date_to should be today
        self.assertEqual(tw["date_to"], date.today().isoformat())
        # date_from should be approx 90 days ago
        expected_from = date.today() - timedelta(days=90)
        self.assertEqual(tw["date_from"], expected_from.isoformat())

    def test_relative_last_N_weeks(self):
        engine = self._new_engine()
        tw = engine._extract_time_window("Invoices from the last two weeks")
        self.assertIsNotNone(tw)
        expected_from = date.today() - timedelta(days=14)
        self.assertEqual(tw["date_from"], expected_from.isoformat())

    def test_relative_last_digit_months(self):
        engine = self._new_engine()
        tw = engine._extract_time_window("payments in the last 6 months")
        self.assertIsNotNone(tw)
        expected_from = date.today() - timedelta(days=180)
        self.assertEqual(tw["date_from"], expected_from.isoformat())

    def test_absolute_range_iso(self):
        engine = self._new_engine()
        tw = engine._extract_time_window("Show documents between 2026-01-01 and 2026-03-31")
        self.assertIsNotNone(tw)
        self.assertEqual(tw["date_from"], "2026-01-01")
        self.assertEqual(tw["date_to"], "2026-03-31")

    def test_absolute_after_date(self):
        engine = self._new_engine()
        tw = engine._extract_time_window("invoices after 2026-04-01")
        self.assertIsNotNone(tw)
        self.assertEqual(tw["date_from"], "2026-04-01")
        self.assertIsNone(tw["date_to"])

    def test_absolute_before_date(self):
        engine = self._new_engine()
        tw = engine._extract_time_window("expenses before 2026-03-31")
        self.assertIsNotNone(tw)
        self.assertIsNone(tw["date_from"])
        self.assertEqual(tw["date_to"], "2026-03-31")

    def test_no_time_window(self):
        engine = self._new_engine()
        tw = engine._extract_time_window("What is the EORI number of Aphotonix GmbH?")
        self.assertIsNone(tw)

    # --- _extract_criteria_query with entity-of-person + time window ---

    def test_expenses_of_person_last_three_months(self):
        engine = self._new_engine()
        parsed = engine.parse_query(
            "Show me the expenses of Benjamin Pang in the last three months"
        )
        self.assertEqual(parsed.intent, "criteria_lookup")
        self.assertIsInstance(parsed.criteria, dict)
        self.assertEqual(parsed.criteria.get("entity_type"), "expense")
        self.assertIn("Benjamin Pang", parsed.criteria.get("must_contain", []))
        self.assertIsInstance(parsed.criteria.get("time_window"), dict)
        tw = parsed.criteria["time_window"]
        self.assertIsNotNone(tw.get("date_from"))
        self.assertEqual(tw.get("date_to"), date.today().isoformat())

    def test_invoices_of_person_last_six_months(self):
        engine = self._new_engine()
        parsed = engine.parse_query(
            "List invoices of Alice Example in the last 6 months"
        )
        self.assertEqual(parsed.intent, "criteria_lookup")
        self.assertEqual(parsed.criteria.get("entity_type"), "invoice")
        tw = parsed.criteria.get("time_window") or {}
        expected_from = (date.today() - timedelta(days=180)).isoformat()
        self.assertEqual(tw.get("date_from"), expected_from)

    def test_criteria_with_absolute_date_range(self):
        engine = self._new_engine()
        parsed = engine.parse_query(
            "Show me the payments of John Smith between 2026-01-01 and 2026-03-31"
        )
        self.assertEqual(parsed.intent, "criteria_lookup")
        self.assertEqual(parsed.criteria.get("entity_type"), "payment")
        tw = parsed.criteria.get("time_window") or {}
        self.assertEqual(tw.get("date_from"), "2026-01-01")
        self.assertEqual(tw.get("date_to"), "2026-03-31")

    # --- search_by_criteria passes date params to SQL ---

    def _make_cursor_with_param_capture(self):
        """Returns a cursor whose last execute params are stored on self."""
        class CaptureCursor:
            def __init__(self):
                self.last_params = None

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, _sql, params):
                self.last_params = params

            def fetchall(self):
                return []

        return CaptureCursor()

    def test_search_by_criteria_passes_date_params(self):
        engine = self._new_engine()
        capture = self._make_cursor_with_param_capture()

        class _Conn:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def cursor(self):
                return capture

        engine._get_connection = lambda: _Conn()
        engine._search_by_criteria_semantic_fallback = lambda **_kwargs: None

        parsed = ParsedQuery(
            intent="criteria_lookup",
            entity_name="Benjamin Pang",
            attribute_name=None,
            confidence=0.88,
            criteria={
                "entity_type": "expense",
                "must_contain": ["Benjamin Pang"],
                "attribute_hints": ["amount", "expense", "date"],
                "time_window": {
                    "date_from": "2026-03-10",
                    "date_to": "2026-06-10",
                },
            },
        )
        engine.search_by_criteria("", parsed=parsed)
        # Verify date params were passed (positions 8,9 = date_from and 10,11 = date_to)
        params = capture.last_params
        self.assertIsNotNone(params)
        self.assertIn("2026-03-10", params)
        self.assertIn("2026-06-10", params)

    def test_search_by_criteria_no_date_params_when_no_window(self):
        engine = self._new_engine()
        capture = self._make_cursor_with_param_capture()

        class _Conn:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def cursor(self):
                return capture

        engine._get_connection = lambda: _Conn()
        engine._search_by_criteria_semantic_fallback = lambda **_kwargs: None

        parsed = ParsedQuery(
            intent="criteria_lookup",
            entity_name=None,
            attribute_name=None,
            confidence=0.86,
            criteria={
                "entity_type": "employee",
                "must_contain": ["backend"],
                "attribute_hints": ["skills"],
            },
        )
        engine.search_by_criteria("", parsed=parsed)
        params = capture.last_params
        # date_from and date_to should both be None
        self.assertIn(None, params)


class TestTemporalStyleClassification(unittest.TestCase):
    def _new_tools(self) -> IDMSInteractionTools:
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)
        tools.logger = MagicMock()
        tools.query_cache = None
        tools.state = InteractionState()
        return tools

    def test_temporal_criteria_gets_correct_style(self):
        tools = self._new_tools()
        tools._invoke_solf_clause_raw = lambda _clause, _args: {}

        style = tools._classify_query_style(
            "Show me the expenses of Benjamin Pang in the last three months",
            {
                "intent": "criteria_lookup",
                "entity_name": "Benjamin Pang",
                "attribute_name": None,
                "confidence": 0.88,
                "criteria": {
                    "entity_type": "expense",
                    "must_contain": ["Benjamin Pang"],
                    "time_window": {"date_from": "2026-03-10", "date_to": "2026-06-10"},
                },
            },
        )
        self.assertEqual(style.get("query_style"), "temporal_filtered_criteria")
        self.assertEqual(style.get("initial_action"), "search_criteria")

    def test_non_temporal_criteria_gets_criteria_list_style(self):
        tools = self._new_tools()
        tools._invoke_solf_clause_raw = lambda _clause, _args: {}

        style = tools._classify_query_style(
            "List all employees with backend skills",
            {
                "intent": "criteria_lookup",
                "entity_name": None,
                "attribute_name": None,
                "confidence": 0.86,
                "criteria": {
                    "entity_type": "employee",
                    "must_contain": ["backend"],
                },
            },
        )
        self.assertEqual(style.get("query_style"), "criteria_list")
        self.assertEqual(style.get("initial_action"), "search_criteria")


if __name__ == "__main__":
    unittest.main()
