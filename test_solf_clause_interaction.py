import unittest
from unittest.mock import MagicMock

from interaction import IDMSInteractionTools, InteractionState


class _FakeInterpreter:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.clauses = {}

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
        tools._ensure_solf_interpreter_loaded = MagicMock(return_value=False)

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

    def test_resolve_solf_clause_request_matches_generic_merge_clause(self):
        tools = self._new_tools()
        fake = _FakeInterpreter({"status": "merged"})
        fake.clauses = {
            "merge_entities": [{"args": ["_entity_1", "_entity_2"]}],
        }
        tools.solf_interpreter = fake

        req = tools._resolve_solf_clause_request("merge Yeung Pang and Chung Yeung Pang together")

        self.assertIsInstance(req, dict)
        self.assertEqual(req.get("clause_name"), "merge_entities")
        self.assertEqual(req.get("args"), ["Yeung Pang", "Chung Yeung Pang"])
        self.assertEqual(req.get("dispatch_mode"), "generic_nl")

    def test_query_routes_explicit_solf_predicate_request(self):
        tools = self._new_tools()
        tools.solf_interpreter = _FakeInterpreter({"mode": "allow", "reason": "ok"})

        result = tools.query('run solf predicate query_style_policy with {"question":"hello"}')

        self.assertEqual(result.get("source"), "solf_clause")
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("clause_name"), "query_style_policy")
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        self.assertEqual(data.get("result"), {"mode": "allow", "reason": "ok"})

    def test_query_routes_generic_solf_clause_request(self):
        tools = self._new_tools()
        fake = _FakeInterpreter({"status": "merged"})
        fake.clauses = {
            "merge_entities": [{"args": ["_entity_1", "_entity_2"]}],
        }
        tools.solf_interpreter = fake
        tools._resolve_merge_entity_reference = MagicMock(side_effect=[
            {"status": "resolved", "object_id": 11, "object_name": "Yeung Pang"},
            {"status": "resolved", "object_id": 22, "object_name": "Chung Yeung Pang"},
        ])

        result = tools.query("merge Yeung Pang and Chung Yeung Pang together")

        self.assertEqual(result.get("source"), "solf_clause")
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("clause_name"), "merge_entities")
        self.assertEqual(fake.calls, [("merge_entities", [11, 22])])

    def test_query_routes_generic_merge_clause_to_pending_clarification(self):
        tools = self._new_tools()
        fake = _FakeInterpreter({"status": "merged"})
        fake.clauses = {
            "merge_entities": [{"args": ["_entity_1", "_entity_2"]}],
        }
        tools.solf_interpreter = fake
        tools._resolve_merge_entity_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "Yeung Pang",
            "candidates": [{"name": "Chung Yeung Pang", "object_id": 11}],
        })
        tools._create_merge_clause_clarification = MagicMock(return_value={
            "intent": "solf_clause",
            "source": "pending_clarification",
            "answer": "[source: pending_clarification] choose entity",
            "status": "pending_clarification",
            "thread_id": 7,
            "clarification_question": "choose entity",
            "candidates": [{"name": "Chung Yeung Pang", "object_id": 11}],
            "data": {},
            "clause_name": "merge_entities",
        })

        result = tools.query("merge Yeung Pang and Chung Yeung Pang together")

        self.assertEqual(result.get("source"), "pending_clarification")
        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 7)

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

    def test_autonomous_query_routes_generic_solf_clause_request(self):
        tools = self._new_tools()
        fake = _FakeInterpreter({"status": "merged"})
        fake.clauses = {
            "merge_entities": [{"args": ["_entity_1", "_entity_2"]}],
        }
        tools.solf_interpreter = fake
        tools._resolve_merge_entity_reference = MagicMock(side_effect=[
            {"status": "resolved", "object_id": 11, "object_name": "Yeung Pang"},
            {"status": "resolved", "object_id": 22, "object_name": "Chung Yeung Pang"},
        ])

        result = tools.autonomous_query(
            "merge Yeung Pang and Chung Yeung Pang together",
            max_steps=1,
            record_history=False,
        )

        self.assertEqual(result.get("answer_source"), "solf_clause")
        grounding = result.get("grounding") if isinstance(result.get("grounding"), dict) else {}
        solf_result = grounding.get("solf_clause_result") if isinstance(grounding.get("solf_clause_result"), dict) else {}
        self.assertTrue(solf_result.get("success"))
        self.assertEqual(solf_result.get("clause_name"), "merge_entities")

    def test_query_routes_generic_retrieve_document_clause_with_doc_resolution(self):
        tools = self._new_tools()
        fake = _FakeInterpreter({"status": "retrieved"})
        fake.clauses = {
            "retrieve_document_file": [{"args": ["_doc_ref"]}],
        }
        tools.solf_interpreter = fake
        tools._resolve_document_reference = MagicMock(return_value={
            "status": "resolved",
            "doc_id": 55,
            "doc_name": "Invoice-55",
            "doc_key": "INV-55",
            "doc_path": "C:/docs/invoice-55.pdf",
        })

        result = tools.query("retrieve document Invoice-55")

        self.assertEqual(result.get("source"), "solf_clause")
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("clause_name"), "retrieve_document_file")
        self.assertEqual(fake.calls, [("retrieve_document_file", [55])])

    def test_query_routes_generic_retrieve_document_clause_to_pending_clarification(self):
        tools = self._new_tools()
        fake = _FakeInterpreter({"status": "retrieved"})
        fake.clauses = {
            "retrieve_document_file": [{"args": ["_doc_ref"]}],
        }
        tools.solf_interpreter = fake
        tools._resolve_document_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "invoice april",
            "candidates": [{"name": "Invoice-April-2026", "doc_id": 101}],
        })
        tools._create_document_clause_clarification = MagicMock(return_value={
            "intent": "solf_clause",
            "source": "pending_clarification",
            "answer": "[source: pending_clarification] choose document",
            "status": "pending_clarification",
            "thread_id": 13,
            "clarification_question": "choose document",
            "candidates": [{"name": "Invoice-April-2026", "doc_id": 101}],
            "data": {},
            "clause_name": "retrieve_document_file",
        })

        result = tools.query("retrieve document invoice april")

        self.assertEqual(result.get("source"), "pending_clarification")
        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 13)

    def test_query_routes_generic_entity_update_clause_with_resolution(self):
        tools = self._new_tools()
        fake = _FakeInterpreter({"status": "updated"})
        fake.clauses = {
            "entity_update": [{"args": ["_entity_payload"]}],
        }
        tools.solf_interpreter = fake
        tools._resolve_merge_entity_reference = MagicMock(return_value={
            "status": "resolved",
            "object_id": 33,
            "object_name": "Aphotonix GmbH",
            "class_name": "company",
        })

        result = tools.query("entity update Aphotonix GmbH eori_no to CH123")

        self.assertEqual(result.get("source"), "solf_clause")
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("clause_name"), "entity_update")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][0], "entity_update")
        payload = fake.calls[0][1][0]
        self.assertEqual(payload.get("resolved_object_id"), 33)
        self.assertEqual((payload.get("attributes") or {}).get("eori_no"), "CH123")

    def test_query_routes_generic_entity_update_clause_to_pending_clarification(self):
        tools = self._new_tools()
        fake = _FakeInterpreter({"status": "updated"})
        fake.clauses = {
            "entity_update": [{"args": ["_entity_payload"]}],
        }
        tools.solf_interpreter = fake
        tools._resolve_merge_entity_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "Aphotonix",
            "candidates": [{"name": "Aphotonix GmbH", "object_id": 33}],
        })
        tools._create_update_clause_clarification = MagicMock(return_value={
            "intent": "solf_clause",
            "source": "pending_clarification",
            "answer": "[source: pending_clarification] choose entity",
            "status": "pending_clarification",
            "thread_id": 17,
            "clarification_question": "choose entity",
            "candidates": [{"name": "Aphotonix GmbH", "object_id": 33}],
            "data": {},
            "clause_name": "entity_update",
        })

        result = tools.query("entity update Aphotonix eori_no to CH123")

        self.assertEqual(result.get("source"), "pending_clarification")
        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 17)

    def test_respond_to_clarification_resolves_merge_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 9,
            "status": "pending_clarification",
            "original_query": "merge Yeung Pang and Chung Yeung Pang together",
            "candidate_snapshot": [{"name": "Chung Yeung Pang", "object_id": 11}],
            "metadata": {
                "trigger": "solf_clause_merge_entities",
                "solf_clause_name": "merge_entities",
                "merge_resolution": {
                    "raw_args": ["Yeung Pang", "Chung Yeung Pang"],
                    "resolved_ids": {"entity_2": 22},
                    "pending_slot": "entity_1",
                },
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools.evaluate_solf_clause = MagicMock(return_value={"success": True, "clause_name": "merge_entities", "result": {"status": "merged"}})
        tools._format_direct_solf_query_result = MagicMock(return_value={
            "source": "solf_clause",
            "answer": "[source: solf_clause] merged",
        })

        result = tools.respond_to_clarification(9, "Chung Yeung Pang")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "solf_clause")
        tools.evaluate_solf_clause.assert_called_once_with(clause_name="merge_entities", payload={}, args=[11, 22])

    def test_respond_to_clarification_resolves_retrieve_document_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 14,
            "status": "pending_clarification",
            "original_query": "retrieve document invoice april",
            "candidate_snapshot": [{"name": "Invoice-April-2026", "doc_id": 101}],
            "metadata": {
                "trigger": "solf_clause_retrieve_document_file",
                "solf_clause_name": "retrieve_document_file",
                "document_resolution": {"raw_arg": "invoice april"},
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools.evaluate_solf_clause = MagicMock(return_value={"success": True, "clause_name": "retrieve_document_file", "result": {"status": "retrieved"}})
        tools._format_direct_solf_query_result = MagicMock(return_value={
            "source": "solf_clause",
            "answer": "[source: solf_clause] retrieved",
        })

        result = tools.respond_to_clarification(14, "Invoice-April-2026")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "solf_clause")
        tools.evaluate_solf_clause.assert_called_once_with(clause_name="retrieve_document_file", payload={}, args=[101])

    def test_respond_to_clarification_resolves_entity_update_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 18,
            "status": "pending_clarification",
            "original_query": "entity update Aphotonix eori_no to CH123",
            "candidate_snapshot": [{"name": "Aphotonix GmbH", "object_id": 33}],
            "metadata": {
                "trigger": "solf_clause_entity_update",
                "solf_clause_name": "entity_update",
                "update_resolution": {
                    "update_payload": {
                        "object_name": "Aphotonix",
                        "class_name": "entity",
                        "attributes": {"eori_no": "CH123"},
                    },
                },
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools.evaluate_solf_clause = MagicMock(return_value={"success": True, "clause_name": "entity_update", "result": {"status": "updated"}})
        tools._format_direct_solf_query_result = MagicMock(return_value={
            "source": "solf_clause",
            "answer": "[source: solf_clause] updated",
        })

        result = tools.respond_to_clarification(18, "Aphotonix GmbH")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "solf_clause")
        tools.evaluate_solf_clause.assert_called_once()
        call_kwargs = tools.evaluate_solf_clause.call_args.kwargs
        self.assertEqual(call_kwargs.get("clause_name"), "entity_update")
        args = call_kwargs.get("args") or []
        self.assertEqual((args[0] or {}).get("resolved_object_id"), 33)

    def test_respond_to_clarification_resolves_workflow_update_entity_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 21,
            "status": "pending_clarification",
            "original_query": "workflow action update_entity",
            "candidate_snapshot": [{"name": "Aphotonix GmbH", "object_id": 33}],
            "metadata": {
                "trigger": "workflow_action_update_entity",
                "action_name": "update_entity",
                "update_action_resolution": {
                    "payload": {
                        "entity_type": "company",
                        "natural_key_field": "name",
                        "natural_key_value": "Aphotonix",
                        "field": "eori_no",
                        "new_value": "CH123",
                    },
                },
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools.perform_action = MagicMock(return_value={
            "action": "update_entity",
            "success": True,
            "message": "Entity updated by id.",
        })

        result = tools.respond_to_clarification(21, "Aphotonix GmbH")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "workflow_action")
        tools.perform_action.assert_called_once()
        call_args = tools.perform_action.call_args.args
        self.assertEqual(call_args[0], "update_entity")
        self.assertEqual((call_args[1] or {}).get("entity_id"), 33)

    def test_respond_to_clarification_resolves_workflow_retrieve_document_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 22,
            "status": "pending_clarification",
            "original_query": "workflow action retrieve_document_file",
            "candidate_snapshot": [{"name": "Invoice-April-2026", "doc_id": 101}],
            "metadata": {
                "trigger": "workflow_action_retrieve_document_file",
                "action_name": "retrieve_document_file",
                "retrieve_action_resolution": {
                    "payload": {
                        "doc_name": "invoice april",
                    },
                },
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools.perform_action = MagicMock(return_value={
            "action": "retrieve_document_file",
            "success": True,
            "message": "Document retrieved.",
        })

        result = tools.respond_to_clarification(22, "Invoice-April-2026")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "workflow_action")
        tools.perform_action.assert_called_once()
        call_args = tools.perform_action.call_args.args
        self.assertEqual(call_args[0], "retrieve_document_file")
        self.assertEqual((call_args[1] or {}).get("doc_id"), 101)

    def test_respond_to_clarification_resolves_workflow_assign_task_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 23,
            "status": "pending_clarification",
            "original_query": "workflow action assign_task",
            "candidate_snapshot": [{"name": "Monthly Close - EU", "task_id": 12}],
            "metadata": {
                "trigger": "workflow_action_task_reference",
                "action_name": "assign_task",
                "task_action_resolution": {
                    "payload": {
                        "task_name": "monthly close",
                        "assigned_to": 88,
                    },
                },
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools.perform_action = MagicMock(return_value={
            "action": "assign_task",
            "success": True,
            "message": "Task assigned.",
        })

        result = tools.respond_to_clarification(23, "Monthly Close - EU")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "workflow_action")
        tools.perform_action.assert_called_once()
        call_args = tools.perform_action.call_args.args
        self.assertEqual(call_args[0], "assign_task")
        self.assertEqual((call_args[1] or {}).get("task_id"), 12)

    def test_respond_to_clarification_resolves_workflow_close_task_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 24,
            "status": "pending_clarification",
            "original_query": "workflow action close_task",
            "candidate_snapshot": [{"name": "Monthly Close - EU", "task_id": 12}],
            "metadata": {
                "trigger": "workflow_action_task_reference",
                "action_name": "close_task",
                "task_action_resolution": {
                    "payload": {
                        "task_name": "monthly close",
                    },
                },
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools.perform_action = MagicMock(return_value={
            "action": "close_task",
            "success": True,
            "message": "Task closed.",
        })

        result = tools.respond_to_clarification(24, "Monthly Close - EU")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "workflow_action")
        tools.perform_action.assert_called_once()
        call_args = tools.perform_action.call_args.args
        self.assertEqual(call_args[0], "close_task")
        self.assertEqual((call_args[1] or {}).get("task_id"), 12)

    def test_respond_to_clarification_resolves_workflow_create_task_project_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 25,
            "status": "pending_clarification",
            "original_query": "workflow action create_task",
            "candidate_snapshot": [{"name": "Phase 4 Regression Project", "project_id": 77}],
            "metadata": {
                "trigger": "workflow_action_create_task_reference",
                "action_name": "create_task",
                "create_task_action_resolution": {
                    "payload": {
                        "project_name": "Phase 4",
                        "title": "Prepare filing package",
                        "hours_estimate": 3,
                    },
                    "pending_field": "project",
                },
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools.perform_action = MagicMock(return_value={
            "action": "create_task",
            "success": True,
            "message": "Task created.",
        })

        result = tools.respond_to_clarification(25, "Phase 4 Regression Project")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "workflow_action")
        tools.perform_action.assert_called_once()
        call_args = tools.perform_action.call_args.args
        self.assertEqual(call_args[0], "create_task")
        self.assertEqual((call_args[1] or {}).get("project_id"), 77)

    def test_respond_to_clarification_resolves_workflow_create_task_assignee_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 26,
            "status": "pending_clarification",
            "original_query": "workflow action create_task",
            "candidate_snapshot": [{"name": "Yeung Pang", "object_id": 11}],
            "metadata": {
                "trigger": "workflow_action_create_task_reference",
                "action_name": "create_task",
                "create_task_action_resolution": {
                    "payload": {
                        "project_id": 77,
                        "title": "Prepare filing package",
                        "hours_estimate": 3,
                        "assigned_to": "Yeung Pang",
                    },
                    "pending_field": "assignee",
                },
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools.perform_action = MagicMock(return_value={
            "action": "create_task",
            "success": True,
            "message": "Task created.",
        })

        result = tools.respond_to_clarification(26, "Yeung Pang")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "workflow_action")
        tools.perform_action.assert_called_once()
        call_args = tools.perform_action.call_args.args
        self.assertEqual(call_args[0], "create_task")
        self.assertEqual((call_args[1] or {}).get("assigned_to"), 11)

    def test_respond_to_clarification_resolves_workflow_create_workflow_case_subject_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 27,
            "status": "pending_clarification",
            "original_query": "workflow action create_workflow_case",
            "candidate_snapshot": [{"name": "Phase 4 Regression Project", "object_id": 77}],
            "metadata": {
                "trigger": "workflow_action_create_workflow_case_reference",
                "action_name": "create_workflow_case",
                "create_workflow_case_action_resolution": {
                    "payload": {
                        "case_no": "CASE-100",
                        "case_type": "ops",
                        "subject_name": "Phase 4",
                        "initiated_by_name": "Yeung Pang",
                        "current_step": "review",
                        "sla_due_at": "2026-07-15T10:00:00Z",
                        "status": "open",
                    },
                    "pending_field": "subject",
                },
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools.perform_action = MagicMock(return_value={
            "action": "create_workflow_case",
            "success": True,
            "message": "Workflow case created.",
        })

        result = tools.respond_to_clarification(27, "Phase 4 Regression Project")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "workflow_action")
        tools.perform_action.assert_called_once()
        call_args = tools.perform_action.call_args.args
        self.assertEqual(call_args[0], "create_workflow_case")
        self.assertEqual((call_args[1] or {}).get("subject_ref"), 77)

    def test_respond_to_clarification_resolves_workflow_create_workflow_case_initiator_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 28,
            "status": "pending_clarification",
            "original_query": "workflow action create_workflow_case",
            "candidate_snapshot": [{"name": "Yeung Pang", "object_id": 11}],
            "metadata": {
                "trigger": "workflow_action_create_workflow_case_reference",
                "action_name": "create_workflow_case",
                "create_workflow_case_action_resolution": {
                    "payload": {
                        "case_no": "CASE-101",
                        "case_type": "ops",
                        "subject_ref": 77,
                        "initiated_by_name": "Yeung Pang",
                        "current_step": "review",
                        "sla_due_at": "2026-07-15T10:00:00Z",
                        "status": "open",
                    },
                    "pending_field": "initiator",
                },
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools.perform_action = MagicMock(return_value={
            "action": "create_workflow_case",
            "success": True,
            "message": "Workflow case created.",
        })

        result = tools.respond_to_clarification(28, "Yeung Pang")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "workflow_action")
        tools.perform_action.assert_called_once()
        call_args = tools.perform_action.call_args.args
        self.assertEqual(call_args[0], "create_workflow_case")
        self.assertEqual((call_args[1] or {}).get("initiated_by_ref"), 11)

    def test_respond_to_clarification_resolves_workflow_record_approval_case_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 29,
            "status": "pending_clarification",
            "original_query": "workflow action record_approval",
            "candidate_snapshot": [{"case_no": "CASE-200-A", "case_ref": 301}],
            "metadata": {
                "trigger": "workflow_action_record_approval_reference",
                "action_name": "record_approval",
                "record_approval_action_resolution": {
                    "payload": {
                        "case_no": "CASE-200",
                        "approver_name": "Yeung Pang",
                        "decision": "approved",
                        "decision_at": "2026-07-08T10:00:00Z",
                    },
                    "pending_field": "case",
                },
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools._resolve_workflow_case_reference = MagicMock(return_value={
            "status": "resolved",
            "input": "CASE-200-A",
            "case_ref": 301,
            "case_no": "CASE-200-A",
            "case_type": "ops",
        })
        tools.perform_action = MagicMock(return_value={
            "action": "record_approval",
            "success": True,
            "message": "Approval recorded.",
        })

        result = tools.respond_to_clarification(29, "CASE-200-A")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "workflow_action")
        tools.perform_action.assert_called_once()
        call_args = tools.perform_action.call_args.args
        self.assertEqual(call_args[0], "record_approval")
        self.assertEqual((call_args[1] or {}).get("case_ref"), 301)

    def test_respond_to_clarification_resolves_workflow_record_approval_approver_thread(self):
        tools = self._new_tools()
        tools._get_clarification_thread_row = MagicMock(return_value={
            "thread_id": 30,
            "status": "pending_clarification",
            "original_query": "workflow action record_approval",
            "candidate_snapshot": [{"name": "Yeung Pang", "object_id": 11}],
            "metadata": {
                "trigger": "workflow_action_record_approval_reference",
                "action_name": "record_approval",
                "record_approval_action_resolution": {
                    "payload": {
                        "case_ref": 301,
                        "approver_name": "Yeung Pang",
                        "decision": "approved",
                        "decision_at": "2026-07-08T10:00:00Z",
                    },
                    "pending_field": "approver",
                },
            },
        })
        tools._append_clarification_turn = MagicMock()
        tools._set_clarification_thread_status = MagicMock()
        tools.perform_action = MagicMock(return_value={
            "action": "record_approval",
            "success": True,
            "message": "Approval recorded.",
        })

        result = tools.respond_to_clarification(30, "Yeung Pang")

        self.assertEqual(result.get("status"), "resolved")
        self.assertEqual(result.get("answer_source"), "workflow_action")
        tools.perform_action.assert_called_once()
        call_args = tools.perform_action.call_args.args
        self.assertEqual(call_args[0], "record_approval")
        self.assertEqual((call_args[1] or {}).get("approver_ref"), 11)


if __name__ == "__main__":
    unittest.main()
