import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import interaction as interaction_module
from interaction import IDMSInteractionTools
from query_engine import QueryEngine


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


class _NullConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestOwnershipFallbackRegression(unittest.TestCase):
    def test_sanitize_grounding_keeps_only_anchor_entity_evidence(self):
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)

        grounding = {
            "parsed": {"entity_name": "Seveco AG"},
            "attribute_result": {
                "entity_name": "Seveco AG",
                "attribute_name": "governance_roles",
                "attribute_value": [
                    {"name": "Chung Yeung Pang", "role": "Mitglied"},
                    {"name": "Käthi Johanna Pang Gerber", "role": "Direktorin"},
                ],
            },
            "criteria_result": {
                "matches": [
                    {"entity_name": "APHOTONIX GmbH", "attribute_name": "shareholders"},
                    {"entity_name": "Seveco AG", "attribute_name": "governance_roles"},
                ],
                "results": [
                    {"source_entity_name": "APHOTONIX GmbH", "attribute_name": "shareholders"},
                    {"source_entity_name": "Seveco AG", "attribute_name": "governance_roles"},
                ],
            },
            "scope": {
                "matches": [
                    {"entity_name": "APHOTONIX GmbH"},
                    {"entity_name": "Seveco AG"},
                ]
            },
        }

        sanitized = tools._sanitize_grounding_for_anchor(grounding, "Seveco AG")
        criteria = sanitized.get("criteria_result") or {}

        self.assertEqual((sanitized.get("attribute_result") or {}).get("entity_name"), "Seveco AG")
        self.assertEqual([item.get("entity_name") for item in criteria.get("matches", [])], ["Seveco AG"])
        self.assertEqual([item.get("source_entity_name") for item in criteria.get("results", [])], ["Seveco AG"])
        self.assertEqual([item.get("entity_name") for item in (sanitized.get("scope") or {}).get("matches", [])], ["Seveco AG"])

    def test_governance_role_lookup_filters_unrelated_staff_and_customers(self):
        engine = QueryEngine.__new__(QueryEngine)

        rows = [
            ("Seveco AG", "Alice Staff", "employed_by", {"role": "staff"}, 1, "doc-a", "path-a"),
            (
                "Seveco AG",
                "Chung Yeung Pang",
                "is_member_of",
                {"role": "Mitglied", "signing_authority": "Einzelunterschrift"},
                2,
                "doc-b",
                "path-b",
            ),
            (
                "Seveco AG",
                "Käthi Johanna Pang Gerber",
                "is_director_of",
                {"role": "Direktorin", "signing_authority": "Einzelunterschrift"},
                3,
                "doc-c",
                "path-c",
            ),
            ("Seveco AG", "Customer Example", "has_customer_relationship", {"role": "customer"}, 4, "doc-d", "path-d"),
        ]

        engine._get_connection = lambda: _FakeConnection(rows)
        result = engine._sql_governance_roles_lookup(engine._get_connection(), "Seveco AG")

        self.assertIsNotNone(result)
        self.assertEqual(result.get("entity_name"), "Seveco AG")
        self.assertEqual(result.get("attribute_name"), "governance_roles")

        people = [item.get("name") for item in (result.get("attribute_value") or [])]
        self.assertEqual(people, ["Chung Yeung Pang", "Käthi Johanna Pang Gerber"])
        self.assertNotIn("Alice Staff", people)
        self.assertNotIn("Customer Example", people)

    def test_autonomous_query_strips_unrelated_entities_from_final_prompt(self):
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)
        tools.state = SimpleNamespace(history=[])
        tools.query_cache = None
        tools.logger = MagicMock()

        captured = {}

        tools._extract_solf_clause_request = lambda _question: None
        tools._classify_query_style = lambda _question, _parsed: {"mode": "allow"}
        tools._select_model_by_confidence = lambda *_args, **_kwargs: "test-model"
        tools._record_model_usage = lambda *_args, **_kwargs: None
        tools._invoke_solf_policy = lambda clause_name, _payload: {
            "mode": "allow",
            "reason": clause_name,
            "message": "",
            "policy_clause": clause_name,
        }

        tools._planner_step = lambda _question, _state, step_index: (
            {"action": "resolve_scope", "rationale": "resolve"}
            if step_index == 0
            else {"action": "lookup_attribute", "attribute_name": "owner", "entity_name": "Seveco AG", "rationale": "lookup"}
            if step_index == 1
            else {"action": "finalize", "rationale": "finalize"}
        )
        tools._scope_is_empty = lambda scope: not bool(scope)
        tools.search_discovery = lambda *_args, **_kwargs: {"candidate_count": 0, "summary": None, "results": []}
        tools.search_criteria = lambda *_args, **_kwargs: {"success": False, "count": 0}

        tools.query_engine = SimpleNamespace(
            parse_query=lambda _question: SimpleNamespace(
                intent="attribute_lookup",
                entity_name="Seveco AG",
                attribute_name=None,
                relation_name="owner",
                attribute_names=None,
                confidence=0.9,
                language="en",
                criteria={
                    "relation_filters": [
                        {"relationship_names": ["owner"], "target_name": "Seveco AG"},
                    ]
                },
            ),
            answer=lambda _question: {"source": "not_found", "answer": "No explicit ownership evidence found."},
            resolve_search_scope=lambda _question, limit=12: {
                "candidate_count": 1,
                "discovery_candidate_count": 0,
                "qdrant_search_mode": "dense",
                "qdrant_collection": "idms_demo",
                "node_ids": [],
                "edge_ids": [],
                "doc_ids": [],
                "candidates": [
                    {"name": "Seveco AG", "score": 9.8},
                ],
                "discovery_results": [],
                "nodes": [],
                "edges": [],
                "documents": [],
            },
            lookup_attribute=lambda *args, **kwargs: {
                "entity_name": "Seveco AG",
                "attribute_name": "governance_roles",
                "attribute_value": [
                    {"name": "Chung Yeung Pang", "role": "Mitglied", "signing_authority": "Einzelunterschrift"},
                    {"name": "Käthi Johanna Pang Gerber", "role": "Direktorin", "signing_authority": "Einzelunterschrift"},
                ],
            },
            _is_value_document_context_question=lambda _question: False,
            _criteria_question_prefers_listing=lambda _question: False,
            _get_attribute_display_name=lambda attr_name, _lang: attr_name,
        )

        class _FakeResponse:
            text = "final ok"

        def _generate_content(*, contents, **_kwargs):
            captured["prompt"] = contents[0] if isinstance(contents, list) and contents else contents
            return _FakeResponse()

        def _fallback_stub(**kwargs):
            contents = kwargs.get("contents")
            captured["prompt"] = contents[0] if isinstance(contents, list) and contents else contents
            return _FakeResponse()

        interaction_module.generate_content_with_openrouter_fallback = _fallback_stub
        tools.client = SimpleNamespace(models=SimpleNamespace(generate_content=_generate_content))

        result = tools.autonomous_query("Who own Seveco AG?", max_steps=3, record_history=False)

        prompt = str(captured.get("prompt") or "")
        self.assertIn("Seveco AG", prompt)
        self.assertNotIn("APHOTONIX GmbH", prompt)

    def test_autonomous_query_strips_unrelated_entities_from_aphotonix_prompt(self):
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)
        tools.state = SimpleNamespace(history=[])
        tools.query_cache = None
        tools.logger = MagicMock()

        captured = {}

        tools._extract_solf_clause_request = lambda _question: None
        tools._classify_query_style = lambda _question, _parsed: {"mode": "allow"}
        tools._select_model_by_confidence = lambda *_args, **_kwargs: "test-model"
        tools._record_model_usage = lambda *_args, **_kwargs: None
        tools._invoke_solf_policy = lambda clause_name, _payload: {
            "mode": "allow",
            "reason": clause_name,
            "message": "",
            "policy_clause": clause_name,
        }
        tools._planner_step = lambda _question, _state, step_index: (
            {"action": "resolve_scope", "rationale": "resolve"}
            if step_index == 0
            else {"action": "lookup_attribute", "attribute_name": "owner", "entity_name": "APHOTONIX GmbH", "rationale": "lookup"}
            if step_index == 1
            else {"action": "finalize", "rationale": "finalize"}
        )
        tools._scope_is_empty = lambda scope: not bool(scope)
        tools.search_discovery = lambda *_args, **_kwargs: {"candidate_count": 0, "summary": None, "results": []}
        tools.search_criteria = lambda *_args, **_kwargs: {"success": False, "count": 0}

        tools.query_engine = SimpleNamespace(
            parse_query=lambda _question: SimpleNamespace(
                intent="attribute_lookup",
                entity_name="APHOTONIX GmbH",
                attribute_name=None,
                relation_name="owner",
                attribute_names=None,
                confidence=0.9,
                language="en",
                criteria={
                    "relation_filters": [
                        {"relationship_names": ["owner"], "target_name": "APHOTONIX GmbH"},
                    ]
                },
            ),
            answer=lambda _question: {"source": "not_found", "answer": "No explicit ownership evidence found."},
            resolve_search_scope=lambda _question, limit=12: {
                "candidate_count": 2,
                "discovery_candidate_count": 0,
                "qdrant_search_mode": "dense",
                "qdrant_collection": "idms_demo",
                "node_ids": [],
                "edge_ids": [],
                "doc_ids": [],
                "candidates": [
                    {"name": "Seveco AG", "score": 9.9},
                    {"name": "APHOTONIX GmbH", "score": 9.8},
                ],
                "discovery_results": [],
                "nodes": [],
                "edges": [],
                "documents": [],
            },
            lookup_attribute=lambda *args, **kwargs: {
                "entity_name": "APHOTONIX GmbH",
                "attribute_name": "governance_roles",
                "attribute_value": [
                    {"name": "Benjamin Kin Sing Pang", "role": "Geschäftsführer", "signing_authority": "Einzelunterschrift"},
                    {"name": "Chung Yeung Pang", "role": "Vorsitzender der Geschäftsführung", "signing_authority": "Einzelunterschrift"},
                ],
            },
            _is_value_document_context_question=lambda _question: False,
            _criteria_question_prefers_listing=lambda _question: False,
            _get_attribute_display_name=lambda attr_name, _lang: attr_name,
        )

        class _FakeResponse:
            text = "final ok"

        def _generate_content(*, contents, **_kwargs):
            captured["prompt"] = contents[0] if isinstance(contents, list) and contents else contents
            return _FakeResponse()

        def _fallback_stub(**kwargs):
            contents = kwargs.get("contents")
            captured["prompt"] = contents[0] if isinstance(contents, list) and contents else contents
            return _FakeResponse()

        interaction_module.generate_content_with_openrouter_fallback = _fallback_stub
        tools.client = SimpleNamespace(models=SimpleNamespace(generate_content=_generate_content))

        tools.autonomous_query("Who owns APHOTONIX GmbH?", max_steps=3, record_history=False)

        prompt = str(captured.get("prompt") or "")
        self.assertIn("APHOTONIX GmbH", prompt)
        self.assertNotIn("Seveco AG", prompt)

    def test_try_resolve_initials_supports_comma_reversed_names(self):
        engine = QueryEngine.__new__(QueryEngine)
        rows = [
            ("Pang, Benjamin Kin Sing",),
            ("Pang, Chung Yeung",),
        ]

        resolved = engine._try_resolve_initials(_FakeConnection(rows), "B. Pang")

        self.assertEqual(resolved, "Pang, Benjamin Kin Sing")


if __name__ == "__main__":
    unittest.main()