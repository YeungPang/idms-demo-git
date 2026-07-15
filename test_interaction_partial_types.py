"""Regression tests for previously partial interaction types.

Covers:
- ingest_document action branch
- ingest_information action branch
- generate_document action branch
- natural-key SQL identifier normalization and graceful SQL failures
"""

import unittest
import uuid
from pathlib import Path
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import business_rules
import domain_function
from interaction import IDMSInteractionTools


class _DummyEventBus:
    def __init__(self):
        self.events = []

    def emit(self, event_name, source_entity, entity_id, payload, tags):
        self.events.append(
            {
                "event_name": event_name,
                "source_entity": source_entity,
                "entity_id": entity_id,
                "payload": payload,
                "tags": tags,
            }
        )


class TestInteractionPartialTypes(unittest.TestCase):
    def _build_tools_for_perform_action(self) -> IDMSInteractionTools:
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)
        tools._log_action_execution = lambda *_args, **_kwargs: None
        tools._compose_action_plan = lambda *_args, **_kwargs: None
        tools.event_bus = _DummyEventBus()
        return tools

    def test_ingest_document_action(self):
        tools = self._build_tools_for_perform_action()
        tools.ingest_document = MagicMock(return_value={"ingested": True, "source": "doc.txt"})

        result = tools.perform_action(
            "ingest_document",
            {
                "source": "doc.txt",
                "description": "source document",
                "tags": ["ops"],
                "metadata": {"kind": "note"},
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "ingest_document")
        self.assertEqual(result["data"]["ingested"], True)
        self.assertEqual(len(tools.event_bus.events), 1)
        self.assertEqual(tools.event_bus.events[0]["event_name"], "document.ingested")

    def test_ingest_information_action(self):
        tools = self._build_tools_for_perform_action()
        tools.ingest_information = MagicMock(return_value={"ingested": True, "kind": "text"})

        result = tools.perform_action(
            "ingest_information",
            {
                "information_text": "budget reduced",
                "description": "update",
                "tags": ["finance"],
                "metadata": {"source": "meeting"},
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "ingest_information")
        self.assertEqual(result["data"]["ingested"], True)
        self.assertEqual(len(tools.event_bus.events), 1)
        self.assertEqual(tools.event_bus.events[0]["event_name"], "information.ingested")

    def test_generate_document_action(self):
        tools = self._build_tools_for_perform_action()

        result = tools.perform_action(
            "generate_document",
            {
                "document_type": "status_report",
                "subject": "Weekly Ops",
                "context": {"owner": "ops"},
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "generate_document")
        self.assertIn("content", result["data"])
        self.assertIn("Weekly Ops", result["data"]["content"])
        self.assertEqual(len(tools.event_bus.events), 1)
        self.assertEqual(tools.event_bus.events[0]["event_name"], "document.generated")

    def test_normalize_solf_identifier(self):
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)
        self.assertEqual(tools._normalize_solf_identifier("['vat_no']"), "vat_no")
        self.assertEqual(tools._normalize_solf_identifier('["status"]'), "status")
        self.assertEqual(tools._normalize_solf_identifier("name"), "name")

    def test_execute_sql_plan_returns_structured_error(self):
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return _Cursor()

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, _sql, _params):
                raise RuntimeError("forced SQL failure")

        tools.get_connection = lambda: _Conn()

        result = tools._execute_sql_plan(
            {
                "operation": "update",
                "table": "project_tasks",
                "where": {"['id']": 1},
                "values": {"['status']": "done"},
                "returning": ["id"],
            }
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["operation"], "update")
        self.assertIn("SQL update failed", result["message"])

    def test_perform_action_update_entity_natural_key_ambiguous_returns_pending_clarification(self):
        tools = self._build_tools_for_perform_action()
        tools._invoke_solf_policy = MagicMock(return_value={"mode": "allow", "reason": "ok"})
        tools._apply_workflow_rule_directives = MagicMock(
            side_effect=lambda **kwargs: {
                "payload": dict(kwargs.get("payload") or {}),
                "applied": False,
                "applied_rules": [],
            }
        )
        tools._compose_action_plan = MagicMock(return_value={
            "guard": "can_update_entity",
            "sql_builder": "sql_plan_update_entity_by_natural_key",
            "args": {
                "entity_type": "company",
                "natural_key_field": "name",
                "natural_key_value": "Aphotonix",
                "field": "eori_no",
                "new_value": "CH123",
            },
        })
        tools._invoke_solf_clause_raw = MagicMock(return_value={
            "operation": "update",
            "table": "company",
            "where": {"name": "Aphotonix"},
            "values": {"eori_no": "CH123"},
            "returning": ["id"],
        })
        tools._resolve_merge_entity_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "Aphotonix",
            "candidates": [{"name": "Aphotonix GmbH", "object_id": 33}],
        })
        tools._create_update_action_clarification = MagicMock(return_value={
            "action": "update_entity",
            "source": "pending_clarification",
            "success": False,
            "status": "pending_clarification",
            "thread_id": 27,
            "clarification_question": "choose entity",
            "candidates": [{"name": "Aphotonix GmbH", "object_id": 33}],
            "message": "choose entity",
        })
        tools._execute_sql_plan = MagicMock()

        result = tools.perform_action(
            "update_entity",
            {
                "entity_type": "company",
                "natural_key_field": "name",
                "natural_key_value": "Aphotonix",
                "field": "eori_no",
                "new_value": "CH123",
            },
        )

        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 27)
        tools._execute_sql_plan.assert_not_called()

    def test_perform_action_retrieve_document_ambiguous_returns_pending_clarification(self):
        tools = self._build_tools_for_perform_action()
        tools._invoke_solf_policy = MagicMock(return_value={"mode": "allow", "reason": "ok"})
        tools._apply_workflow_rule_directives = MagicMock(
            side_effect=lambda **kwargs: {
                "payload": dict(kwargs.get("payload") or {}),
                "applied": False,
                "applied_rules": [],
            }
        )
        tools._compose_action_plan = MagicMock(return_value={
            "guard": "can_retrieve_document_file",
            "sql_builder": "sql_plan_retrieve_document_by_doc_name",
            "args": {"doc_name": "invoice april"},
        })
        tools._invoke_solf_clause_raw = MagicMock(return_value={
            "operation": "select",
            "table": "document",
            "where": {"doc_name": "invoice april"},
        })
        tools._resolve_document_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "invoice april",
            "candidates": [{"name": "Invoice-April-2026", "doc_id": 101}],
        })
        tools._create_retrieve_action_clarification = MagicMock(return_value={
            "action": "retrieve_document_file",
            "source": "pending_clarification",
            "success": False,
            "status": "pending_clarification",
            "thread_id": 31,
            "clarification_question": "choose document",
            "candidates": [{"name": "Invoice-April-2026", "doc_id": 101}],
            "message": "choose document",
        })
        tools._execute_sql_plan = MagicMock()

        result = tools.perform_action(
            "retrieve_document_file",
            {
                "doc_name": "invoice april",
            },
        )

        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 31)
        tools._execute_sql_plan.assert_not_called()

    def test_perform_action_assign_task_ambiguous_returns_pending_clarification(self):
        tools = self._build_tools_for_perform_action()
        tools._invoke_solf_policy = MagicMock(return_value={"mode": "allow", "reason": "ok"})
        tools._apply_workflow_rule_directives = MagicMock(
            side_effect=lambda **kwargs: {
                "payload": dict(kwargs.get("payload") or {}),
                "applied": False,
                "applied_rules": [],
            }
        )
        tools._compose_action_plan = MagicMock(return_value={
            "guard": "can_assign_task",
            "sql_builder": "sql_plan_assign_task",
            "args": {
                "task_name": "monthly close",
                "assigned_to": 88,
            },
        })
        tools._invoke_solf_clause_raw = MagicMock(return_value={
            "operation": "update",
            "table": "project_tasks",
            "where": {"name": "monthly close"},
            "values": {"assigned_to": 88, "status": "in_progress"},
            "returning": ["id"],
        })
        tools._resolve_task_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "monthly close",
            "candidates": [{"name": "Monthly Close - EU", "task_id": 12}],
        })
        tools._create_task_action_clarification = MagicMock(return_value={
            "action": "assign_task",
            "source": "pending_clarification",
            "success": False,
            "status": "pending_clarification",
            "thread_id": 44,
            "clarification_question": "choose task",
            "candidates": [{"name": "Monthly Close - EU", "task_id": 12}],
            "message": "choose task",
        })
        tools._execute_sql_plan = MagicMock()

        result = tools.perform_action(
            "assign_task",
            {
                "task_name": "monthly close",
                "assigned_to": 88,
            },
        )

        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 44)
        tools._execute_sql_plan.assert_not_called()

    def test_perform_action_close_task_ambiguous_returns_pending_clarification(self):
        tools = self._build_tools_for_perform_action()
        tools._invoke_solf_policy = MagicMock(return_value={"mode": "allow", "reason": "ok"})
        tools._apply_workflow_rule_directives = MagicMock(
            side_effect=lambda **kwargs: {
                "payload": dict(kwargs.get("payload") or {}),
                "applied": False,
                "applied_rules": [],
            }
        )
        tools._compose_action_plan = MagicMock(return_value={
            "guard": "can_close_task",
            "sql_builder": "sql_plan_close_task",
            "args": {
                "task_name": "monthly close",
            },
        })
        tools._invoke_solf_clause_raw = MagicMock(return_value={
            "operation": "update",
            "table": "project_tasks",
            "where": {"name": "monthly close"},
            "values": {"status": "done"},
            "returning": ["id"],
        })
        tools._resolve_task_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "monthly close",
            "candidates": [{"name": "Monthly Close - EU", "task_id": 12}],
        })
        tools._create_task_action_clarification = MagicMock(return_value={
            "action": "close_task",
            "source": "pending_clarification",
            "success": False,
            "status": "pending_clarification",
            "thread_id": 45,
            "clarification_question": "choose task",
            "candidates": [{"name": "Monthly Close - EU", "task_id": 12}],
            "message": "choose task",
        })
        tools._execute_sql_plan = MagicMock()

        result = tools.perform_action(
            "close_task",
            {
                "task_name": "monthly close",
            },
        )

        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 45)
        tools._execute_sql_plan.assert_not_called()

    def test_perform_action_create_task_project_ambiguous_returns_pending_clarification(self):
        tools = self._build_tools_for_perform_action()
        tools._invoke_solf_policy = MagicMock(return_value={"mode": "allow", "reason": "ok"})
        tools._apply_workflow_rule_directives = MagicMock(
            side_effect=lambda **kwargs: {
                "payload": dict(kwargs.get("payload") or {}),
                "applied": False,
                "applied_rules": [],
            }
        )
        tools._compose_action_plan = MagicMock(return_value={
            "guard": "can_create_task",
            "sql_builder": "sql_plan_create_task",
            "args": {
                "project_id": None,
                "title": "Prepare filing package",
                "hours_estimate": 3,
                "assigned_to": None,
            },
        })
        tools._invoke_solf_clause_raw = MagicMock(return_value={
            "operation": "insert",
            "table": "project_tasks",
            "values": {
                "project_id": None,
                "name": "Prepare filing package",
                "estimated_hours": 3,
                "assigned_to": None,
                "status": "open",
            },
            "returning": ["id"],
        })
        tools._resolve_project_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "Phase 4",
            "candidates": [{"name": "Phase 4 Regression Project", "project_id": 77}],
        })
        tools._create_create_task_action_clarification = MagicMock(return_value={
            "action": "create_task",
            "source": "pending_clarification",
            "success": False,
            "status": "pending_clarification",
            "thread_id": 51,
            "clarification_question": "choose project",
            "candidates": [{"name": "Phase 4 Regression Project", "project_id": 77}],
            "message": "choose project",
        })
        tools._execute_sql_plan = MagicMock()

        result = tools.perform_action(
            "create_task",
            {
                "project_name": "Phase 4",
                "title": "Prepare filing package",
                "hours_estimate": 3,
            },
        )

        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 51)
        tools._execute_sql_plan.assert_not_called()

    def test_perform_action_create_task_assignee_ambiguous_returns_pending_clarification(self):
        tools = self._build_tools_for_perform_action()
        tools._invoke_solf_policy = MagicMock(return_value={"mode": "allow", "reason": "ok"})
        tools._apply_workflow_rule_directives = MagicMock(
            side_effect=lambda **kwargs: {
                "payload": dict(kwargs.get("payload") or {}),
                "applied": False,
                "applied_rules": [],
            }
        )
        tools._compose_action_plan = MagicMock(return_value={
            "guard": "can_create_task",
            "sql_builder": "sql_plan_create_task",
            "args": {
                "project_id": 77,
                "title": "Prepare filing package",
                "hours_estimate": 3,
                "assigned_to": "Yeung Pang",
            },
        })
        tools._invoke_solf_clause_raw = MagicMock(return_value={
            "operation": "insert",
            "table": "project_tasks",
            "values": {
                "project_id": 77,
                "name": "Prepare filing package",
                "estimated_hours": 3,
                "assigned_to": "Yeung Pang",
                "status": "open",
            },
            "returning": ["id"],
        })
        tools._resolve_merge_entity_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "Yeung Pang",
            "candidates": [{"name": "Yeung Pang", "object_id": 11}],
        })
        tools._create_create_task_action_clarification = MagicMock(return_value={
            "action": "create_task",
            "source": "pending_clarification",
            "success": False,
            "status": "pending_clarification",
            "thread_id": 52,
            "clarification_question": "choose assignee",
            "candidates": [{"name": "Yeung Pang", "object_id": 11}],
            "message": "choose assignee",
        })
        tools._execute_sql_plan = MagicMock()

        result = tools.perform_action(
            "create_task",
            {
                "project_id": 77,
                "title": "Prepare filing package",
                "hours_estimate": 3,
                "assigned_to": "Yeung Pang",
            },
        )

        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 52)
        tools._execute_sql_plan.assert_not_called()

    def test_perform_action_create_workflow_case_subject_ambiguous_returns_pending_clarification(self):
        tools = self._build_tools_for_perform_action()
        tools._invoke_solf_policy = MagicMock(return_value={"mode": "allow", "reason": "ok"})
        tools._apply_workflow_rule_directives = MagicMock(
            side_effect=lambda **kwargs: {
                "payload": dict(kwargs.get("payload") or {}),
                "applied": False,
                "applied_rules": [],
            }
        )
        tools._compose_action_plan = MagicMock(return_value={
            "guard": "can_create_workflow_case",
            "sql_builder": "sql_plan_create_workflow_case",
            "args": {
                "case_no": "CASE-100",
                "case_type": "ops",
                "subject_name": "Phase 4",
                "initiated_by_name": "Yeung Pang",
                "current_step": "review",
                "sla_due_at": "2026-07-15T10:00:00Z",
                "status": "open",
            },
        })
        tools._invoke_solf_clause_raw = MagicMock(return_value={
            "operation": "insert",
            "table": "workflow_cases",
            "values": {
                "case_no": "CASE-100",
                "case_type": "ops",
                "subject_ref": None,
                "initiated_by_ref": None,
                "current_step": "review",
                "sla_due_at": "2026-07-15T10:00:00Z",
                "status": "open",
            },
            "returning": ["id"],
        })
        tools._resolve_merge_entity_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "Phase 4",
            "candidates": [{"name": "Phase 4 Regression Project", "object_id": 77}],
        })
        tools._create_create_workflow_case_action_clarification = MagicMock(return_value={
            "action": "create_workflow_case",
            "source": "pending_clarification",
            "success": False,
            "status": "pending_clarification",
            "thread_id": 61,
            "clarification_question": "choose subject",
            "candidates": [{"name": "Phase 4 Regression Project", "object_id": 77}],
            "message": "choose subject",
        })
        tools._execute_sql_plan = MagicMock()

        result = tools.perform_action(
            "create_workflow_case",
            {
                "case_no": "CASE-100",
                "case_type": "ops",
                "subject_name": "Phase 4",
                "initiated_by_name": "Yeung Pang",
                "current_step": "review",
                "sla_due_at": "2026-07-15T10:00:00Z",
                "status": "open",
            },
        )

        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 61)
        tools._execute_sql_plan.assert_not_called()

    def test_perform_action_create_workflow_case_initiator_ambiguous_returns_pending_clarification(self):
        tools = self._build_tools_for_perform_action()
        tools._invoke_solf_policy = MagicMock(return_value={"mode": "allow", "reason": "ok"})
        tools._apply_workflow_rule_directives = MagicMock(
            side_effect=lambda **kwargs: {
                "payload": dict(kwargs.get("payload") or {}),
                "applied": False,
                "applied_rules": [],
            }
        )
        tools._compose_action_plan = MagicMock(return_value={
            "guard": "can_create_workflow_case",
            "sql_builder": "sql_plan_create_workflow_case",
            "args": {
                "case_no": "CASE-101",
                "case_type": "ops",
                "subject_ref": 77,
                "initiated_by_name": "Yeung Pang",
                "current_step": "review",
                "sla_due_at": "2026-07-15T10:00:00Z",
                "status": "open",
            },
        })
        tools._invoke_solf_clause_raw = MagicMock(return_value={
            "operation": "insert",
            "table": "workflow_cases",
            "values": {
                "case_no": "CASE-101",
                "case_type": "ops",
                "subject_ref": 77,
                "initiated_by_ref": None,
                "current_step": "review",
                "sla_due_at": "2026-07-15T10:00:00Z",
                "status": "open",
            },
            "returning": ["id"],
        })
        tools._resolve_merge_entity_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "Yeung Pang",
            "candidates": [{"name": "Yeung Pang", "object_id": 11}],
        })
        tools._create_create_workflow_case_action_clarification = MagicMock(return_value={
            "action": "create_workflow_case",
            "source": "pending_clarification",
            "success": False,
            "status": "pending_clarification",
            "thread_id": 62,
            "clarification_question": "choose initiator",
            "candidates": [{"name": "Yeung Pang", "object_id": 11}],
            "message": "choose initiator",
        })
        tools._execute_sql_plan = MagicMock()

        result = tools.perform_action(
            "create_workflow_case",
            {
                "case_no": "CASE-101",
                "case_type": "ops",
                "subject_ref": 77,
                "initiated_by_name": "Yeung Pang",
                "current_step": "review",
                "sla_due_at": "2026-07-15T10:00:00Z",
                "status": "open",
            },
        )

        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 62)
        tools._execute_sql_plan.assert_not_called()

    def test_perform_action_record_approval_case_ambiguous_returns_pending_clarification(self):
        tools = self._build_tools_for_perform_action()
        tools._invoke_solf_policy = MagicMock(return_value={"mode": "allow", "reason": "ok"})
        tools._apply_workflow_rule_directives = MagicMock(
            side_effect=lambda **kwargs: {
                "payload": dict(kwargs.get("payload") or {}),
                "applied": False,
                "applied_rules": [],
            }
        )
        tools._compose_action_plan = MagicMock(return_value={
            "guard": "can_record_approval",
            "sql_builder": "sql_plan_record_approval",
            "args": {
                "case_no": "CASE-200",
                "approver_name": "Yeung Pang",
                "decision": "approved",
                "decision_at": "2026-07-08T10:00:00Z",
            },
        })
        tools._invoke_solf_clause_raw = MagicMock(return_value={
            "operation": "insert",
            "table": "approvals",
            "values": {
                "case_ref": None,
                "approver_ref": None,
                "decision": "approved",
                "decision_at": "2026-07-08T10:00:00Z",
                "comment": None,
                "escalation_level": 0,
            },
            "returning": ["id"],
        })
        tools._resolve_workflow_case_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "CASE-200",
            "candidates": [{"case_no": "CASE-200-A", "case_ref": 301}],
        })
        tools._create_record_approval_action_clarification = MagicMock(return_value={
            "action": "record_approval",
            "source": "pending_clarification",
            "success": False,
            "status": "pending_clarification",
            "thread_id": 71,
            "clarification_question": "choose case",
            "candidates": [{"case_no": "CASE-200-A", "case_ref": 301}],
            "message": "choose case",
        })
        tools._execute_sql_plan = MagicMock()

        result = tools.perform_action(
            "record_approval",
            {
                "case_no": "CASE-200",
                "approver_name": "Yeung Pang",
                "decision": "approved",
                "decision_at": "2026-07-08T10:00:00Z",
            },
        )

        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 71)
        tools._execute_sql_plan.assert_not_called()

    def test_perform_action_record_approval_approver_ambiguous_returns_pending_clarification(self):
        tools = self._build_tools_for_perform_action()
        tools._invoke_solf_policy = MagicMock(return_value={"mode": "allow", "reason": "ok"})
        tools._apply_workflow_rule_directives = MagicMock(
            side_effect=lambda **kwargs: {
                "payload": dict(kwargs.get("payload") or {}),
                "applied": False,
                "applied_rules": [],
            }
        )
        tools._compose_action_plan = MagicMock(return_value={
            "guard": "can_record_approval",
            "sql_builder": "sql_plan_record_approval",
            "args": {
                "case_ref": 301,
                "approver_name": "Yeung Pang",
                "decision": "approved",
                "decision_at": "2026-07-08T10:00:00Z",
            },
        })
        tools._invoke_solf_clause_raw = MagicMock(return_value={
            "operation": "insert",
            "table": "approvals",
            "values": {
                "case_ref": 301,
                "approver_ref": None,
                "decision": "approved",
                "decision_at": "2026-07-08T10:00:00Z",
                "comment": None,
                "escalation_level": 0,
            },
            "returning": ["id"],
        })
        tools._resolve_merge_entity_reference = MagicMock(return_value={
            "status": "ambiguous",
            "input": "Yeung Pang",
            "candidates": [{"name": "Yeung Pang", "object_id": 11}],
        })
        tools._create_record_approval_action_clarification = MagicMock(return_value={
            "action": "record_approval",
            "source": "pending_clarification",
            "success": False,
            "status": "pending_clarification",
            "thread_id": 72,
            "clarification_question": "choose approver",
            "candidates": [{"name": "Yeung Pang", "object_id": 11}],
            "message": "choose approver",
        })
        tools._execute_sql_plan = MagicMock()

        result = tools.perform_action(
            "record_approval",
            {
                "case_ref": 301,
                "approver_name": "Yeung Pang",
                "decision": "approved",
                "decision_at": "2026-07-08T10:00:00Z",
            },
        )

        self.assertEqual(result.get("status"), "pending_clarification")
        self.assertEqual(result.get("thread_id"), 72)
        tools._execute_sql_plan.assert_not_called()

    def test_search_criteria_uses_scope_documents_when_sparse(self):
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)
        parsed = SimpleNamespace(
            intent="criteria_lookup",
            criteria={
                "must_contain": ["travelling"],
                "document_hint": "all",
                "semantic_frame": "document_about_entity",
            },
        )
        query_engine = MagicMock()
        query_engine.parse_query.return_value = parsed
        query_engine.search_by_criteria.side_effect = [
            {
                "criteria": parsed.criteria,
                "count": 1,
                "matches": [
                    {
                        "doc_id": 101,
                        "doc_name": "paper_23_11_annex_c_forum_account_pdf",
                        "matched_terms": ["travelling"],
                    }
                ],
                "source": "criteria_docs_sql",
            },
            {
                "criteria": parsed.criteria,
                "count": 1,
                "matches": [
                    {
                        "doc_id": 101,
                        "doc_name": "paper_23_11_annex_c_forum_account_pdf",
                        "matched_terms": ["travelling"],
                    }
                ],
                "source": "criteria_semantic_qdrant",
            },
        ]
        tools.query_engine = query_engine

        scope_payload = {
            "doc_ids": [201, 202, 203],
            "documents": [
                {"doc_id": 201, "doc_name": "2025-12-18_SBB_Kontanz_ticket_forward.pdf", "doc_path": "2025-12-18_SBB_Kontanz_ticket_forward.pdf"},
                {"doc_id": 202, "doc_name": "2025-12-18_SBB_Kontanz_ticket_return.pdf", "doc_path": "2025-12-18_SBB_Kontanz_ticket_return.pdf"},
                {"doc_id": 203, "doc_name": "vicenza-zug20260331.pdf", "doc_path": "vicenza-zug20260331.pdf"},
            ],
            "candidate_count": 12,
            "discovery_candidate_count": 0,
        }

        out = tools.search_criteria("Show me all the documents that are related to travelling", scope=scope_payload)

        self.assertTrue(out.get("success"))
        self.assertEqual(int(out.get("count") or 0), 3)
        self.assertEqual(str(out.get("source") or ""), "criteria_scope_docs")
        self.assertEqual(str(out.get("scope_retry") or ""), "scope_documents")
        self.assertEqual(
            [item.get("doc_name") for item in (out.get("matches") or [])],
            [
                "2025-12-18_SBB_Kontanz_ticket_forward.pdf",
                "2025-12-18_SBB_Kontanz_ticket_return.pdf",
                "vicenza-zug20260331.pdf",
            ],
        )

    def test_resolve_accounting_booking_company_prefers_explicit_company(self):
        payload = {
            "class_name": "accounting_transaction",
            "attributes": {
                "legal_entity_ref": "Acme AG",
                "payer_ref": "Alice Example",
                "ledger_lines": [
                    {
                        "account_number": 6500,
                        "direction": "debit",
                        "amount_source_currency": "10.00",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "10.00",
                        "swiss_vat_code": "NONE",
                    },
                    {
                        "account_number": 1000,
                        "direction": "credit",
                        "amount_source_currency": "10.00",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "10.00",
                        "swiss_vat_code": "NONE",
                    },
                ],
            },
        }

        resolved = domain_function.resolve_accounting_booking_company(payload)
        attrs = resolved["attributes"]
        self.assertEqual(attrs.get("legal_entity_ref"), "Acme AG")
        self.assertEqual(attrs.get("company_ref"), "Acme AG")
        self.assertEqual(attrs.get("employer_ref"), "Acme AG")

    def test_resolve_accounting_booking_company_from_person_ref(self):
        payload = {
            "class_name": "accounting_transaction",
            "attributes": {
                "payer_ref": "Alice Example",
                "ledger_lines": [
                    {
                        "account_number": 6500,
                        "direction": "debit",
                        "amount_source_currency": "10.00",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "10.00",
                        "swiss_vat_code": "NONE",
                    },
                    {
                        "account_number": 1000,
                        "direction": "credit",
                        "amount_source_currency": "10.00",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "10.00",
                        "swiss_vat_code": "NONE",
                    },
                ],
            },
        }

        with patch.object(domain_function, "_resolve_company_for_person", return_value="Acme AG"):
            resolved = domain_function.resolve_accounting_booking_company(payload)

        attrs = resolved["attributes"]
        self.assertEqual(attrs.get("payer_ref"), "Alice Example")
        self.assertEqual(attrs.get("legal_entity_ref"), "Acme AG")
        self.assertEqual(attrs.get("company_ref"), "Acme AG")
        self.assertEqual(attrs.get("employee_ref"), "Alice Example")

    def test_resolve_accounting_booking_company_ignores_entity_id_placeholder_ref(self):
        payload = {
            "class_name": "accounting_transaction",
            "attributes": {
                "legal_entity_ref": "e1",
                "payer_ref": "Alice Example",
                "ledger_lines": [
                    {
                        "account_number": 6500,
                        "direction": "debit",
                        "amount_source_currency": "10.00",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "10.00",
                        "swiss_vat_code": "NONE",
                    },
                    {
                        "account_number": 1000,
                        "direction": "credit",
                        "amount_source_currency": "10.00",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "10.00",
                        "swiss_vat_code": "NONE",
                    },
                ],
            },
        }

        with patch.object(domain_function, "_resolve_company_for_person", return_value="Aphotonix GmbH"):
            resolved = domain_function.resolve_accounting_booking_company(payload)

        attrs = resolved["attributes"]
        self.assertEqual(attrs.get("legal_entity_ref"), "Aphotonix GmbH")
        self.assertEqual(attrs.get("company_ref"), "Aphotonix GmbH")
        self.assertEqual(attrs.get("payer_ref"), "Alice Example")

    def test_resolve_accounting_booking_company_resolves_doc_local_entity_tokens(self):
        payload = {
            "class_name": "accounting_transaction",
            "attributes": {
                "doc_id": 14,
                "legal_entity_ref": "e1",
                "payer_ref": "e2",
                "ledger_lines": [
                    {
                        "account_number": 6400,
                        "direction": "debit",
                        "amount_source_currency": "253.61",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "253.61",
                        "swiss_vat_code": "NONE",
                    },
                    {
                        "account_number": 2000,
                        "direction": "credit",
                        "amount_source_currency": "274.15",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "274.15",
                        "swiss_vat_code": "NONE",
                    },
                ],
            },
        }

        with patch.object(domain_function, "_resolve_doc_local_entity_name", return_value="Yeung Pang"), patch.object(
            domain_function,
            "_resolve_company_for_person",
            return_value="Seveco AG",
        ):
            resolved = domain_function.resolve_accounting_booking_company(payload)

        attrs = resolved["attributes"]
        self.assertEqual(attrs.get("legal_entity_ref"), "Seveco AG")
        self.assertEqual(attrs.get("company_ref"), "Seveco AG")
        self.assertEqual(attrs.get("payer_ref"), "e2")
        self.assertEqual(attrs.get("person_ref"), "Yeung Pang")

    def test_resolve_accounting_booking_company_from_invoice_recipient_field(self):
        payload = {
            "class_name": "accounting_transaction",
            "attributes": {
                "bill_to": "APHOTONIX GmbH",
                "ledger_lines": [
                    {
                        "account_number": 6500,
                        "direction": "debit",
                        "amount_source_currency": "706.20",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "706.20",
                        "swiss_vat_code": "NONE",
                    },
                    {
                        "account_number": 2000,
                        "direction": "credit",
                        "amount_source_currency": "706.20",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "706.20",
                        "swiss_vat_code": "NONE",
                    },
                ],
            },
        }

        resolved = domain_function.resolve_accounting_booking_company(payload)
        attrs = resolved["attributes"]
        self.assertEqual(attrs.get("legal_entity_ref"), "APHOTONIX GmbH")
        self.assertEqual(attrs.get("company_ref"), "APHOTONIX GmbH")

    def test_infer_invoice_party_role_distinguishes_vendor_and_customer_flow(self):
        vendor_bill = {
            "bill_to": "Aphotonix GmbH",
            "issuer_name": "Northwind Supplies AG",
            "invoice_total": "100.00",
            "vat_amount": "7.70",
        }
        sales_invoice = {
            "bill_to": "Client AG",
            "issuer_name": "Aphotonix GmbH",
            "invoice_total": "100.00",
            "vat_amount": "7.70",
        }

        self.assertEqual(
            domain_function._infer_invoice_party_role(vendor_bill, company_hint="Aphotonix GmbH"),
            "vendor_invoice",
        )
        self.assertEqual(
            domain_function._infer_invoice_party_role(sales_invoice, company_hint="Aphotonix GmbH"),
            "sales_invoice",
        )

    def test_derive_summary_ledger_lines_uses_role_specific_posting_shape(self):
        vendor_lines = domain_function._derive_summary_ledger_lines_from_amounts(
            {
                "invoice_total": "107.70",
                "vat_amount": "7.70",
                "currency": "CHF",
            },
            "invoice",
            invoice_party_role="vendor_invoice",
        )
        sales_lines = domain_function._derive_summary_ledger_lines_from_amounts(
            {
                "invoice_total": "107.70",
                "vat_amount": "7.70",
                "currency": "CHF",
            },
            "invoice",
            invoice_party_role="sales_invoice",
        )

        self.assertEqual(vendor_lines[0]["account_number"], 4200)
        self.assertEqual(vendor_lines[-1]["account_number"], 2000)
        self.assertEqual(sales_lines[0]["account_number"], 1100)
        self.assertEqual(sales_lines[1]["account_number"], 3000)
        self.assertEqual(sales_lines[-1]["account_number"], 2200)

        purchase_order_lines = domain_function._derive_summary_ledger_lines_from_amounts(
            {
                "invoice_total": "2500.00",
                "currency": "CHF",
            },
            "purchase_order",
        )
        self.assertEqual(purchase_order_lines[0]["account_number"], 9100)
        self.assertEqual(purchase_order_lines[-1]["account_number"], 2900)

        po_linked_invoice_lines = domain_function._derive_summary_ledger_lines_from_amounts(
            {
                "invoice_total": "2500.00",
                "currency": "CHF",
                "purchase_order_no": "PO-2026-0001",
                "issuer_name": "Northwind Supplies AG",
                "bill_to": "Aphotonix GmbH",
            },
            "invoice",
            invoice_party_role="vendor_invoice",
        )
        commitment_reverse_accounts = [
            int(line.get("account_number") or 0)
            for line in po_linked_invoice_lines
            if int(line.get("account_number") or 0) in {2900, 9100}
        ]
        self.assertIn(2900, commitment_reverse_accounts)
        self.assertIn(9100, commitment_reverse_accounts)

        po_unmatched_invoice_lines = domain_function._derive_summary_ledger_lines_from_amounts(
            {
                "invoice_total": "2500.00",
                "currency": "CHF",
                "purchase_order_no": "PO-2026-0001",
            },
            "invoice",
            invoice_party_role="vendor_invoice",
        )
        unmatched_accounts = [int(line.get("account_number") or 0) for line in po_unmatched_invoice_lines]
        self.assertNotIn(2900, unmatched_accounts)
        self.assertNotIn(9100, unmatched_accounts)

    def test_derive_summary_ledger_lines_from_bank_statement_settlement(self):
        bank_lines = domain_function._derive_summary_ledger_lines_from_amounts(
            {
                "amount": "1800.00",
                "currency": "CHF",
                "payment_status": "paid",
                "payment_direction": "outgoing",
            },
            "bank_statement",
        )
        self.assertEqual(bank_lines[0]["account_number"], 2000)
        self.assertEqual(bank_lines[1]["account_number"], 1020)

    def test_db_accounting_ingest_enriches_company_before_upsert(self):
        payload = {
            "class_name": "accounting_transaction",
            "entity_name": "posting:invoice-112479923",
            "attributes": {
                "bill_to": "APHOTONIX GmbH",
                "ledger_lines": [
                    {
                        "account_number": 6500,
                        "direction": "debit",
                        "amount_source_currency": "706.20",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "706.20",
                        "swiss_vat_code": "NONE",
                    },
                    {
                        "account_number": 2000,
                        "direction": "credit",
                        "amount_source_currency": "706.20",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "706.20",
                        "swiss_vat_code": "NONE",
                    },
                ],
            },
        }

        object_row = {"object_id": 123, "object_name": "posting:invoice-112479923", "class_name": "accounting_transaction"}
        domain_result = {"ok": True, "validation": {"valid": True}, "transaction_id": "tx-1", "ledger_lines_written": 2}

        with patch.object(domain_function.solf_function, "db_ingest", return_value=object_row), patch.object(
            domain_function.domain_db,
            "upsert_transaction_and_lines",
            return_value=domain_result,
        ) as upsert_mock:
            result = domain_function.db_accounting_ingest(payload)

        self.assertTrue(result["accounting_written"])
        upsert_payload = upsert_mock.call_args.kwargs["payload"]
        upsert_attrs = upsert_payload.get("attributes") or {}
        self.assertEqual(upsert_attrs.get("legal_entity_ref"), "APHOTONIX GmbH")

    def test_apply_post_extraction_business_rules_enforces_selected_company_rule(self):
        extracted = {
            "document": {"doc_type": "invoice", "country": "switzerland", "metadata": {}},
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "invoice:apho-1",
                    "class_name": "invoice",
                    "attributes": {
                        "gross_amount": "706.20",
                        "currency": "CHF",
                    },
                }
            ],
        }
        selected_rules = [
            {
                "rule_id": 101,
                "rule_name": "aphotonix_booking",
                "rule_text": "Invoices should be booked for the company Aphotonix GmbH.",
                "structured_rule": {},
            }
        ]

        applied = business_rules.apply_post_extraction_business_rules(
            extracted,
            context={"country": "switzerland", "document_type": "invoice", "operation": "ingest"},
            selected_rules=selected_rules,
            include_inferred=False,
        )

        entity_attrs = applied["extracted"]["entities"][0]["attributes"]
        self.assertEqual(entity_attrs.get("legal_entity_ref"), "Aphotonix GmbH")
        self.assertEqual(entity_attrs.get("company_ref"), "Aphotonix GmbH")
        self.assertEqual(int(applied.get("applied_count") or 0), 1)

    def test_apply_post_extraction_business_rules_matches_receipt_scope_within_email_container(self):
        extracted = {
            "document": {
                "doc_type": "email",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Ticket 1",
                    "class_name": "receipt",
                    "attributes": {
                        "gross_amount": "26.50",
                        "currency": "CHF",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }
        selected_rules = [
            {
                "rule_id": 1,
                "rule_name": "aphotonix_booking",
                "structured_rule": {
                    "application_mode": "selected_only",
                    "post_extraction_directives": [
                        {
                            "target_scope": "entity",
                            "target_entity_classes": [],
                            "conditions": [],
                            "set_attributes": {
                                "legal_entity_ref": "Aphotonix GmbH",
                                "company_ref": "Aphotonix GmbH",
                            },
                            "overwrite_existing": False,
                            "scope": {
                                "document_types": ["invoice", "bill", "receipt"],
                                "countries": [],
                                "entity_classes": [],
                                "operations": [],
                            },
                        }
                    ],
                },
            }
        ]

        applied = business_rules.apply_post_extraction_business_rules(
            extracted,
            context={"country": "switzerland", "document_type": "email", "operation": "ingest"},
            selected_rules=selected_rules,
            include_inferred=False,
        )

        source_attrs = applied["extracted"]["entities"][0]["attributes"]
        self.assertEqual(source_attrs.get("legal_entity_ref"), "Aphotonix GmbH")
        self.assertEqual(source_attrs.get("company_ref"), "Aphotonix GmbH")
        self.assertGreaterEqual(int(applied.get("applied_count") or 0), 1)

    def test_apply_post_extraction_business_rules_matches_receipt_scope_for_generic_ticket_entity(self):
        extracted = {
            "document": {
                "doc_type": "email",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Ticket 1",
                    "class_name": "document",
                    "attributes": {
                        "ticket_type": "Point-to-point Ticket",
                        "fare_type": "Reduced fare 1/2",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }
        selected_rules = [
            {
                "rule_id": 1,
                "rule_name": "aphotonix_booking",
                "structured_rule": {
                    "application_mode": "selected_only",
                    "post_extraction_directives": [
                        {
                            "target_scope": "entity",
                            "target_entity_classes": [],
                            "conditions": [],
                            "set_attributes": {
                                "legal_entity_ref": "Aphotonix GmbH",
                                "company_ref": "Aphotonix GmbH",
                            },
                            "overwrite_existing": False,
                            "scope": {
                                "document_types": ["invoice", "bill", "receipt"],
                                "countries": [],
                                "entity_classes": [],
                                "operations": [],
                            },
                        }
                    ],
                },
            }
        ]

        applied = business_rules.apply_post_extraction_business_rules(
            extracted,
            context={"country": "switzerland", "document_type": "email", "operation": "ingest"},
            selected_rules=selected_rules,
            include_inferred=False,
        )

        source_attrs = applied["extracted"]["entities"][0]["attributes"]
        self.assertEqual(source_attrs.get("legal_entity_ref"), "Aphotonix GmbH")
        self.assertEqual(source_attrs.get("company_ref"), "Aphotonix GmbH")
        self.assertGreaterEqual(int(applied.get("applied_count") or 0), 1)

    def test_apply_post_extraction_business_rules_resolves_reference_tokens_to_entity_names(self):
        extracted = {
            "document": {
                "doc_type": "email",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Ticket 1",
                    "class_name": "receipt",
                    "attributes": {
                        "gross_amount": "26.50",
                        "currency": "CHF",
                    },
                },
                {
                    "entity_id": "e6",
                    "entity_name": "Seveco AG",
                    "class_name": "company",
                    "attributes": {},
                },
            ],
            "relationships": [],
        }
        selected_rules = [
            {
                "rule_id": 1,
                "rule_name": "generic_booking_rule",
                "structured_rule": {
                    "application_mode": "selected_only",
                    "post_extraction_directives": [
                        {
                            "target_scope": "entity",
                            "target_entity_classes": ["receipt"],
                            "conditions": [],
                            "set_attributes": {
                                "legal_entity_ref": "e6",
                                "company_ref": "e6",
                            },
                            "overwrite_existing": True,
                            "scope": {
                                "document_types": ["receipt"],
                                "countries": [],
                                "entity_classes": [],
                                "operations": [],
                            },
                        }
                    ],
                },
            }
        ]

        applied = business_rules.apply_post_extraction_business_rules(
            extracted,
            context={"country": "switzerland", "document_type": "email", "operation": "ingest"},
            selected_rules=selected_rules,
            include_inferred=False,
        )

        attrs = applied["extracted"]["entities"][0]["attributes"]
        self.assertEqual(attrs.get("legal_entity_ref"), "Seveco AG")
        self.assertEqual(attrs.get("company_ref"), "Seveco AG")
        integrity = applied.get("rule_integrity") if isinstance(applied.get("rule_integrity"), dict) else {}
        self.assertEqual(int(integrity.get("violation_count") or 0), 0)

    def test_apply_post_extraction_business_rules_flags_selected_rule_not_applied(self):
        extracted = {
            "document": {
                "doc_type": "invoice",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Invoice A",
                    "class_name": "invoice",
                    "attributes": {
                        "gross_amount": "100.00",
                        "currency": "CHF",
                    },
                }
            ],
            "relationships": [],
        }
        selected_rules = [
            {
                "rule_id": 77,
                "rule_name": "employment_only_rule",
                "structured_rule": {
                    "application_mode": "selected_only",
                    "post_extraction_directives": [
                        {
                            "target_scope": "entity",
                            "target_entity_classes": ["employment_contract"],
                            "conditions": [],
                            "set_attributes": {
                                "department_ref": "FIN",
                            },
                            "overwrite_existing": True,
                            "scope": {
                                "document_types": [],
                                "countries": [],
                                "entity_classes": ["employment_contract"],
                                "operations": [],
                            },
                        }
                    ],
                },
            }
        ]

        applied = business_rules.apply_post_extraction_business_rules(
            extracted,
            context={"country": "switzerland", "document_type": "invoice", "operation": "ingest"},
            selected_rules=selected_rules,
            include_inferred=False,
        )

        integrity = applied.get("rule_integrity") if isinstance(applied.get("rule_integrity"), dict) else {}
        self.assertEqual(int(integrity.get("violation_count") or 0), 1)
        violations = integrity.get("violations") if isinstance(integrity.get("violations"), list) else []
        self.assertEqual(str((violations[0] if violations else {}).get("type") or ""), "selected_rule_not_applied")

    def test_inferred_receipt_rule_sets_booking_account_and_particulars(self):
        extracted = {
            "document": {"doc_type": "receipt", "country": "switzerland", "metadata": {}},
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "receipt:sbb-1",
                    "class_name": "receipt",
                    "attributes": {
                        "gross_amount": "42.50",
                        "currency": "CHF",
                        "issuer_name": "SBB CFF FFS",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }
        inferred_rule = {
            "rule_id": 202,
            "rule_name": "sbb_travel_booking",
            "rule_text": "In general, all receipts from SBB should be booked to travelling expenses account and particulars are train or bus.",
            "structured_rule": {},
        }

        with patch.object(business_rules, "list_business_rules", return_value=[inferred_rule]):
            applied = business_rules.apply_post_extraction_business_rules(
                extracted,
                context={"country": "switzerland", "document_type": "receipt", "operation": "ingest"},
                selected_rules=[],
                include_inferred=True,
            )

        transformed = applied["extracted"]
        source_attrs = transformed["entities"][0]["attributes"]
        self.assertEqual(source_attrs.get("booking_debit_account_name"), "travelling expenses")
        self.assertEqual(source_attrs.get("booking_particulars"), "train or bus")

        class_defs = {"accounting_transaction": {"class_name": "accounting_transaction"}}
        enriched = domain_function.apply_document_action_policy({"document_type": "receipt"}, transformed, class_defs)
        accounting_entities = [
            item for item in (enriched.get("entities") or [])
            if isinstance(item, dict) and str(item.get("class_name") or "") == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        accounting_attrs = accounting_entities[0].get("attributes") if isinstance(accounting_entities[0].get("attributes"), dict) else {}
        self.assertEqual(accounting_attrs.get("booking_debit_account_name"), "travelling expenses")
        self.assertEqual(accounting_attrs.get("booking_particulars"), "train or bus")
        ledger_lines = accounting_attrs.get("ledger_lines") if isinstance(accounting_attrs.get("ledger_lines"), list) else []
        self.assertEqual(ledger_lines[0].get("account_number"), 6400)
        self.assertEqual(ledger_lines[0].get("line_description"), "train or bus")

    def test_inferred_rule_without_general_wording_is_skipped(self):
        extracted = {
            "document": {"doc_type": "receipt", "country": "switzerland", "metadata": {}},
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "receipt:sbb-2",
                    "class_name": "receipt",
                    "attributes": {
                        "gross_amount": "42.50",
                        "currency": "CHF",
                        "issuer_name": "SBB CFF FFS",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }
        inferred_rule = {
            "rule_id": 203,
            "rule_name": "sbb_travel_booking",
            "rule_text": "All receipts from SBB should be booked to travelling expenses account and particulars are train or bus.",
            "structured_rule": {},
        }

        with patch.object(business_rules, "list_business_rules", return_value=[inferred_rule]):
            applied = business_rules.apply_post_extraction_business_rules(
                extracted,
                context={"country": "switzerland", "document_type": "receipt", "operation": "ingest"},
                selected_rules=[],
                include_inferred=True,
            )

        source_attrs = applied["extracted"]["entities"][0]["attributes"]
        self.assertNotIn("booking_debit_account_name", source_attrs)
        self.assertNotIn("booking_particulars", source_attrs)
        self.assertEqual(int(applied.get("applied_count") or 0), 0)

    def test_inferred_sbb_rule_matches_ticket_like_invoice_using_peer_vendor_candidates(self):
        extracted = {
            "document": {"doc_type": "invoice", "country": "switzerland", "metadata": {}},
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "invoice_1",
                    "class_name": "invoice",
                    "attributes": {
                        "invoice_no": "151422346029",
                        "invoice_type": "ticket",
                        "gross_amount": "83.00",
                        "currency": "CHF",
                    },
                    "confidence": 0.98,
                },
                {
                    "entity_id": "e2",
                    "entity_name": "SBB CFF FFS",
                    "class_name": "organization",
                    "attributes": {},
                    "confidence": 0.98,
                },
            ],
            "relationships": [],
        }
        inferred_rule = {
            "rule_id": 204,
            "rule_name": "sbb_booking",
            "rule_text": "In general, for entity classes invoice, bill, receipt, ticket, document, and entity, if vendor/issuer/supplier/legal_name contains SBB or CFF, set booking debit account name to travelling expenses and booking particulars to train or bus.",
            "structured_rule": {
                "application_mode": "general",
                "post_extraction_directives": [
                    {
                        "target_scope": "entity",
                        "target_entity_classes": ["invoice", "bill", "receipt", "ticket", "document", "entity"],
                        "conditions": [
                            {
                                "attribute_terms": ["vendor", "issuer", "supplier", "legal_name"],
                                "operator": "contains_any",
                                "values": ["sbb", "cff"],
                            }
                        ],
                        "set_attributes": {
                            "booking_debit_account_name": "travelling expenses",
                            "booking_particulars": "train or bus",
                        },
                        "overwrite_existing": False,
                        "scope": {
                            "document_types": [],
                            "countries": [],
                            "entity_classes": [],
                            "operations": [],
                        },
                    }
                ],
            },
        }

        with patch.object(business_rules, "list_business_rules", return_value=[inferred_rule]):
            applied = business_rules.apply_post_extraction_business_rules(
                extracted,
                context={"country": "switzerland", "document_type": "invoice", "operation": "ingest"},
                selected_rules=[],
                include_inferred=True,
            )

        source_attrs = applied["extracted"]["entities"][0]["attributes"]
        self.assertEqual(source_attrs.get("booking_debit_account_name"), "travelling expenses")
        self.assertEqual(source_attrs.get("booking_particulars"), "train or bus")
        self.assertGreaterEqual(int(applied.get("applied_count") or 0), 1)

    def test_derive_accounting_transaction_for_email_with_paid_ticket_entity(self):
        payload = {
            "document": {
                "doc_key": "ticket-mail-1",
                "doc_path": "ticket-email.pdf",
                "doc_theme": "SBB ticket confirmation",
                "doc_date": "2025-12-18",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "ticket:sbb-1",
                    "class_name": "ticket",
                    "attributes": {
                        "gross_amount": "26.50",
                        "currency": "CHF",
                        "booking_debit_account_name": "travelling expenses",
                        "booking_particulars": "train ticket",
                        "legal_entity_ref": "Aphotonix GmbH",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        self.assertEqual(attrs.get("legal_entity_ref"), "Aphotonix GmbH")
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertEqual(ledger_lines[0].get("account_number"), 6400)

    def test_derive_accounting_transaction_for_email_with_invoice_entity(self):
        payload = {
            "document": {
                "doc_key": "invoice-mail-1",
                "doc_path": "invoice-email.pdf",
                "doc_theme": "Supplier invoice by email",
                "doc_date": "2025-12-18",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "invoice:mail-1",
                    "class_name": "invoice",
                    "attributes": {
                        "gross_amount": "706.20",
                        "currency": "CHF",
                        "bill_to": "APHOTONIX GmbH",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertEqual(ledger_lines[0].get("account_number"), 4200)

    def test_derive_accounting_transaction_for_purchase_order(self):
        payload = {
            "document": {
                "doc_key": "po-1",
                "doc_path": "purchase-order.xlsx",
                "doc_theme": "Purchase order",
                "doc_date": "2026-07-07",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "purchase_order:teyu-1",
                    "class_name": "purchase_order",
                    "attributes": {
                        "gross_amount": "2500.00",
                        "currency": "CHF",
                        "company_ref": "Aphotonix GmbH",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "purchase_order"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(ledger_lines[0].get("account_number"), 9100)
        self.assertEqual(ledger_lines[-1].get("account_number"), 2900)

    def test_derive_accounting_transaction_for_po_linked_invoice_adds_commitment_settlement(self):
        payload = {
            "document": {
                "doc_key": "inv-po-1",
                "doc_path": "invoice.pdf",
                "doc_theme": "Supplier invoice for PO",
                "doc_date": "2026-07-07",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "invoice:po-1",
                    "class_name": "invoice",
                    "attributes": {
                        "gross_amount": "2500.00",
                        "currency": "CHF",
                        "purchase_order_no": "PO-2026-0001",
                        "issuer_name": "Northwind Supplies AG",
                        "bill_to": "Aphotonix GmbH",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "invoice"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        accounts = [int(line.get("account_number") or 0) for line in ledger_lines]
        self.assertIn(4200, accounts)
        self.assertIn(2000, accounts)
        self.assertIn(2900, accounts)
        self.assertIn(9100, accounts)

    def test_derive_accounting_transaction_for_bank_statement_payment_settlement(self):
        payload = {
            "document": {
                "doc_key": "bank-1",
                "doc_path": "bank-statement.xlsx",
                "doc_theme": "Bank statement",
                "doc_date": "2026-07-07",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "payment:tx-1",
                    "class_name": "bank_transaction",
                    "attributes": {
                        "amount": "1800.00",
                        "currency": "CHF",
                        "payment_status": "paid",
                        "payment_direction": "outgoing",
                        "invoice_no": "INV-2026-001",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "bank_statement"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertEqual(len(ledger_lines), 2)
        self.assertEqual(int(ledger_lines[0].get("account_number") or 0), 2000)
        self.assertEqual(int(ledger_lines[1].get("account_number") or 0), 1020)

    def test_derive_accounting_transaction_for_purchase_order_document_fallback_without_entities(self):
        payload = {
            "document": {
                "doc_key": "po-doc-fallback-1",
                "doc_path": "purchase-order.xlsx",
                "doc_theme": "Purchase order",
                "doc_date": "2026-07-07",
                "metadata": {
                    "structured_tables": {
                        "tables": [
                            {
                                "rows": [
                                    ["Total", "CHF 2'500.00"],
                                ]
                            }
                        ]
                    }
                },
            },
            "entities": [],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "purchase_order"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(int(ledger_lines[0].get("account_number") or 0), 9100)
        self.assertEqual(int(ledger_lines[-1].get("account_number") or 0), 2900)

    def test_purchase_order_derivation_does_not_fallback_to_ticket_accounts(self):
        payload = {
            "document": {
                "doc_key": "po-ticketlike-1",
                "doc_path": "purchase-order.xlsx",
                "doc_theme": "Purchase order",
                "doc_date": "2026-07-07",
                "metadata": {
                    "processing_rule_names": ["aphotonix_booking"],
                },
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Purchase Order",
                    "class_name": "purchase_order",
                    "attributes": {
                        "amount": "131300.00",
                        "currency": "CHF",
                        "ticket_type": "Point-to-point Ticket",
                        "booking_debit_account_name": "travelling expenses",
                    },
                    "confidence": 0.9,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "purchase_order"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        accounts = [int(line.get("account_number") or 0) for line in ledger_lines]
        self.assertIn(9100, accounts)
        self.assertIn(2900, accounts)
        self.assertNotIn(6400, accounts)

    def test_derive_accounting_transaction_for_email_prefers_receipt_with_amount(self):
        payload = {
            "document": {
                "doc_key": "ticket-mail-2",
                "doc_path": "ticket-email.pdf",
                "doc_theme": "SBB ticket confirmation",
                "doc_date": "2025-12-18",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Ticket Header",
                    "class_name": "receipt",
                    "attributes": {
                        "receipt_no": "952462418416",
                        "payment_status": "Paid",
                        "currency": "CHF",
                    },
                    "confidence": 0.92,
                },
                {
                    "entity_id": "e2",
                    "entity_name": "Ticket Amount",
                    "class_name": "receipt",
                    "attributes": {
                        "gross_amount": "26.50",
                        "currency": "CHF",
                        "booking_debit_account_name": "travelling expenses",
                        "booking_particulars": "train ticket",
                    },
                    "confidence": 0.97,
                },
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(ledger_lines[0].get("account_number"), 6400)

    def test_derive_accounting_transaction_for_email_with_generic_ticket_entity_class(self):
        payload = {
            "document": {
                "doc_key": "ticket-mail-3",
                "doc_path": "ticket-email.pdf",
                "doc_theme": "ticket_purchase_confirmation",
                "doc_date": "2025-12-31",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e5",
                    "entity_name": "Ticket 1",
                    "class_name": "entity",
                    "attributes": {
                        "ticket_type": "Point-to-point Ticket",
                        "fare_type": "Reduced fare 1/2",
                        "amount": "26.50",
                        "currency": "CHF",
                        "payment_status": "Paid",
                    },
                    "confidence": 0.95,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(ledger_lines[0].get("account_number"), 6400)

    def test_derive_accounting_transaction_for_email_uses_structured_table_chf_fallback(self):
        payload = {
            "document": {
                "doc_key": "ticket-mail-4",
                "doc_path": "ticket-email.pdf",
                "doc_theme": "ticket_purchase_confirmation",
                "doc_date": "2025-12-31",
                "metadata": {
                    "processing_rule_names": ["aphotonix_booking"],
                    "user_metadata": {
                        "structured_tables": {
                            "tables": [
                                {
                                    "rows": [
                                        ["**CHF 26.50**", "Article no.:"]
                                    ]
                                }
                            ]
                        }
                    }
                },
            },
            "entities": [
                {
                    "entity_id": "e5",
                    "entity_name": "Ticket 1",
                    "class_name": "document",
                    "attributes": {
                        "ticket_type": "Point-to-point Ticket",
                        "payment_status": "Paid",
                    },
                    "confidence": 0.95,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(ledger_lines[0].get("account_number"), 6400)

    def test_derive_accounting_transaction_for_email_invoice_uses_structured_table_eur_fallback(self):
        payload = {
            "document": {
                "doc_key": "invoice-mail-eur-1",
                "doc_path": "invoice-email.pdf",
                "doc_theme": "supplier invoice via email",
                "doc_date": "2026-02-04",
                "metadata": {
                    "processing_rule_names": ["aphotonix_booking"],
                    "user_metadata": {
                        "structured_tables": {
                            "tables": [
                                {
                                    "rows": [
                                        ["Total", "EUR 99.90"],
                                    ]
                                }
                            ]
                        }
                    },
                },
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Invoice Header",
                    "class_name": "invoice",
                    "attributes": {
                        "invoice_no": "INV-2026-0001",
                        "supplier_ref": "SBB CFF FFS",
                    },
                    "confidence": 0.94,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(ledger_lines[0].get("source_currency"), "EUR")
        self.assertEqual(ledger_lines[0].get("amount_source_currency"), "99.90")

    def test_derive_accounting_transaction_for_email_honors_forced_currency_note(self):
        payload = {
            "document": {
                "doc_key": "invoice-mail-currency-override-1",
                "doc_path": "invoice-email.pdf",
                "doc_theme": "supplier invoice via email",
                "doc_date": "2026-02-04",
                "metadata": {
                    "processing_rule_names": ["aphotonix_booking"],
                    "user_metadata": {
                        "force_currency": "CHF",
                        "structured_tables": {
                            "tables": [
                                {
                                    "rows": [
                                        ["Total", "EUR 99.90"],
                                    ]
                                }
                            ]
                        },
                    },
                },
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Invoice Header",
                    "class_name": "invoice",
                    "attributes": {
                        "invoice_no": "INV-2026-0002",
                        "supplier_ref": "SBB CFF FFS",
                    },
                    "confidence": 0.94,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(ledger_lines[0].get("source_currency"), "CHF")
        self.assertEqual(ledger_lines[0].get("amount_source_currency"), "99.90")

    def test_derive_accounting_transaction_uses_booking_hint_from_non_source_entity(self):
        payload = {
            "document": {
                "doc_key": "receipt-mail-booking-hint-1",
                "doc_path": "receipt-email.pdf",
                "doc_theme": "sbb easyride receipt",
                "doc_date": "2026-02-04",
                "metadata": {
                    "processing_rule_names": ["aphotonix_booking"],
                },
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "SBB CFF FFS",
                    "class_name": "organization",
                    "attributes": {
                        "booking_debit_account_name": "travelling expenses",
                        "booking_particulars": "easyride",
                    },
                    "confidence": 0.95,
                },
                {
                    "entity_id": "e2",
                    "entity_name": "EasyRide Receipt",
                    "class_name": "receipt",
                    "attributes": {
                        "receipt_no": "260204101637918404",
                        "amount": "26.50",
                        "currency": "CHF",
                    },
                    "confidence": 0.95,
                },
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "receipt"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(int(ledger_lines[0].get("account_number") or 0), 6400)

    def test_derive_accounting_transaction_for_document_with_rule_context_uses_structured_fallback(self):
        payload = {
            "document": {
                "doc_key": "vicenza-zug20260331.pdf",
                "doc_path": "vicenza-zug20260331.pdf",
                "doc_theme": "travel_booking_confirmation",
                "doc_date": "2026-03-31",
                "metadata": {
                    "processing_rule_names": ["aphotonix_booking"],
                    "user_metadata": {
                        "structured_tables": {
                            "tables": [
                                {
                                    "rows": [
                                        ["Totale", "CHF 47.00"],
                                    ]
                                }
                            ]
                        }
                    },
                },
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Travel Booking",
                    "class_name": "order",
                    "attributes": {},
                    "confidence": 0.92,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "document"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(int(ledger_lines[0].get("account_number") or 0), 6400)
        self.assertEqual(str(ledger_lines[0].get("source_currency") or ""), "CHF")

    def test_extract_amount_from_markdown_cache_path_resolves_outside_cwd(self):
        module_dir = Path(domain_function.__file__).resolve().parent
        markdown_dir = module_dir / "generated" / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        temp_name = f"preextract-test-{uuid.uuid4().hex}.md"
        temp_file = markdown_dir / temp_name
        original_cwd = Path.cwd()

        try:
            temp_file.write_text("Total price of order: CHF 83.00", encoding="utf-8")
            # Simulate service runtime started from parent workspace instead of module directory.
            try:
                import os
                os.chdir(module_dir.parent)
            except Exception:
                pass

            amount, currency = domain_function._extract_amount_and_currency_from_structured_tables(
                {
                    "metadata": {
                        "user_metadata": {
                            "source_reference": {
                                "markdown_cache_path": f"generated\\markdown\\{temp_name}"
                            }
                        }
                    }
                }
            )
        finally:
            try:
                import os
                os.chdir(original_cwd)
            except Exception:
                pass
            if temp_file.exists():
                temp_file.unlink()

        self.assertEqual(str(amount), "83.00")
        self.assertEqual(currency, "CHF")

    def test_derive_accounting_transaction_for_travel_invoice_uses_6400_without_explicit_override(self):
        payload = {
            "document": {
                "doc_key": "travel-overview-1.pdf",
                "doc_path": "travel-overview-1.pdf",
                "doc_theme": "train_ticket",
                "doc_date": "2026-03-26",
                "metadata": {
                    "processing_rule_names": ["aphotonix_booking"],
                },
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "invoice_1",
                    "class_name": "invoice",
                    "attributes": {
                        "invoice_no": "151422346029",
                        "amount": "83.00",
                        "currency": "CHF",
                        "description": "Your travel overview",
                    },
                    "confidence": 0.95,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "invoice"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(int(ledger_lines[0].get("account_number") or 0), 6400)

    def test_derive_accounting_transaction_for_travel_invoice_overrides_sales_role_inference(self):
        payload = {
            "document": {
                "doc_key": "travel-overview-2.pdf",
                "doc_path": "travel-overview-2.pdf",
                "doc_theme": "train_ticket",
                "doc_date": "2026-03-26",
                "metadata": {
                    "processing_rule_names": ["aphotonix_booking"],
                },
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "invoice_1",
                    "class_name": "invoice",
                    "attributes": {
                        "invoice_no": "151422346029",
                        "amount": "83.00",
                        "currency": "CHF",
                        "description": "Travel booking Arth-Goldau - Vicenza",
                        "sender_name": "Aphotonix GmbH",
                    },
                    "confidence": 0.95,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "invoice"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        accounts = [int(line.get("account_number") or 0) for line in ledger_lines]
        self.assertIn(6400, accounts)
        self.assertIn(2000, accounts)
        self.assertNotIn(1100, accounts)
        self.assertNotIn(3000, accounts)

    def test_derive_accounting_transaction_propagates_reference_hints_from_other_entities(self):
        payload = {
            "document": {
                "doc_key": "ticket-mail-5",
                "doc_path": "ticket-email.pdf",
                "doc_theme": "ticket_purchase_confirmation",
                "doc_date": "2025-12-31",
                "metadata": {
                    "post_extraction_rule_applications": [
                        {
                            "rule_name": "generic_company_rule",
                            "entity_name": "Ticket Context",
                            "entity_class": "receipt",
                            "changed_keys": ["legal_entity_ref", "company_ref"],
                            "selection_mode": "explicit",
                            "target_scope": "entity",
                        }
                    ]
                },
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Ticket Main",
                    "class_name": "document",
                    "attributes": {
                        "ticket_type": "Point-to-point Ticket",
                        "payment_status": "Paid",
                        "amount": "26.50",
                        "currency": "CHF",
                    },
                    "confidence": 0.95,
                },
                {
                    "entity_id": "e2",
                    "entity_name": "Ticket Context",
                    "class_name": "receipt",
                    "attributes": {
                        "legal_entity_ref": "Seveco AG",
                        "company_ref": "Seveco AG",
                    },
                    "confidence": 0.9,
                },
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        self.assertEqual(attrs.get("legal_entity_ref"), "Seveco AG")
        self.assertEqual(attrs.get("company_ref"), "Seveco AG")

    def test_derive_accounting_transaction_copies_person_company_context(self):
        payload = {
            "document": {
                "doc_key": "rcpt-1",
                "doc_path": "receipt.pdf",
                "doc_theme": "Meal receipt",
                "doc_date": "2026-06-05",
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Lunch Receipt",
                    "class_name": "receipt",
                    "attributes": {
                        "receipt_no": "R-1",
                        "payer_ref": "Alice Example",
                        "currency": "CHF",
                        "gross_amount": "42.00",
                        "ledger_lines": [
                            {
                                "account_number": 6500,
                                "direction": "debit",
                                "amount_source_currency": "42.00",
                                "source_currency": "CHF",
                                "exchange_rate_to_chf": "1",
                                "amount_chf": "42.00",
                                "swiss_vat_code": "NONE",
                            },
                            {
                                "account_number": 1000,
                                "direction": "credit",
                                "amount_source_currency": "42.00",
                                "source_currency": "CHF",
                                "exchange_rate_to_chf": "1",
                                "amount_chf": "42.00",
                                "swiss_vat_code": "NONE",
                            },
                        ],
                    },
                },
                {
                    "entity_id": "e2",
                    "entity_name": "Alice Example",
                    "class_name": "person",
                    "attributes": {"current_employer_ref": "Acme AG"},
                },
                {
                    "entity_id": "e3",
                    "entity_name": "Acme AG",
                    "class_name": "company",
                    "attributes": {},
                },
            ],
            "relationships": [
                {
                    "source_entity_id": "e2",
                    "target_entity_id": "e3",
                    "source_name": "Alice Example",
                    "target_name": "Acme AG",
                    "relationship_type": "works_for",
                }
            ],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "receipt"},
            payload,
            {"accounting_transaction": object()},
        )

        derived = next(
            entity
            for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        )
        attrs = derived.get("attributes") or {}
        self.assertEqual(attrs.get("payer_ref"), "Alice Example")
        self.assertEqual(attrs.get("legal_entity_ref"), "Acme AG")
        self.assertEqual(attrs.get("company_ref"), "Acme AG")


def run_partial_interaction_regression_suite() -> dict:
    """Run partial-interaction regression tests and return suite summary."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestInteractionPartialTypes)
    runner = unittest.TextTestRunner(stream=StringIO(), verbosity=0)
    result = runner.run(suite)

    failures = []
    for case, err in list(result.failures) + list(result.errors):
        failures.append({"name": str(case), "error": str(err)})

    total = result.testsRun
    failed = len(failures)
    passed = total - failed
    return {
        "result": "ok" if failed == 0 else "error",
        "total": total,
        "passed": passed,
        "failed": failed,
        "failures": failures,
        "tests": [{"name": "partial_interaction_types", "success": failed == 0}],
    }


if __name__ == "__main__":
    outcome = run_partial_interaction_regression_suite()
    print(outcome)
    raise SystemExit(0 if outcome["failed"] == 0 else 1)
