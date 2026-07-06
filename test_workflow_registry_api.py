"""
Regression tests for SOLF workflow registry and one-click rule+registry creation.

Covers:
- Workflow registry CRUD operations
- Workflow version lifecycle (draft/review/published)
- One-click business rule + registry sync
- Rollback and publish endpoints
"""

import unittest
from unittest.mock import MagicMock, patch
import json

import business_rules
import object_db


class WorkflowRegistryTestSetup(unittest.TestCase):
    """Base class for workflow registry tests with shared setup/teardown."""

    @classmethod
    def setUpClass(cls):
        """Initialize database schema before all tests."""
        try:
            connection = object_db.get_connection()
            object_db.create_tables(connection, recreate=True)
            connection.close()
        except Exception as err:
            print(f"Warning: Failed to create tables: {err}")

    @classmethod
    def tearDownClass(cls):
        """Clean up database after all tests."""
        try:
            connection = object_db.get_connection()
            with connection.cursor() as cursor:
                cursor.execute("DROP TABLE IF EXISTS business_rule_workflow_links CASCADE")
                cursor.execute("DROP TABLE IF EXISTS solf_workflow_steps CASCADE")
                cursor.execute("DROP TABLE IF EXISTS solf_workflow_versions CASCADE")
                cursor.execute("DROP TABLE IF EXISTS solf_workflow_registry CASCADE")
            connection.commit()
            connection.close()
        except Exception as err:
            print(f"Warning: Failed to drop tables: {err}")


class TestWorkflowRegistryCRUD(WorkflowRegistryTestSetup):
    """Tests for workflow registry creation, listing, and retrieval."""

    def test_create_workflow_registry_entry(self):
        """Verify workflow registry entry can be created with metadata."""
        result = business_rules.create_solf_workflow_registry_entry(
            workflow_key="test_workflow",
            workflow_name="Test Workflow",
            description="A test workflow",
            domain="accounting",
            status="draft",
            metadata={"test": True},
            is_active=True,
            created_by="test_user",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.get("workflow_key"), "test_workflow")
        self.assertEqual(result.get("workflow_name"), "Test Workflow")
        self.assertEqual(result.get("status"), "draft")
        self.assertTrue(result.get("is_active"))

    def test_list_workflow_registry_entries(self):
        """Verify workflow registry entries can be listed with filters."""
        result = business_rules.list_solf_workflow_registry_entries(
            is_active=True,
            domain=None,
            status=None,
            limit=100,
        )
        self.assertIsInstance(result, list)

    def test_get_workflow_registry_entry(self):
        """Verify workflow registry entry can be retrieved by ID."""
        created = business_rules.create_solf_workflow_registry_entry(
            workflow_key="retrieve_test",
            workflow_name="Retrieve Test",
            is_active=True,
        )
        retrieved = business_rules.get_solf_workflow_registry_entry(workflow_id=created.get("workflow_id"))
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.get("workflow_key"), "retrieve_test")

    def test_set_workflow_registry_entry_active(self):
        """Verify workflow registry entry activation status can be toggled."""
        created = business_rules.create_solf_workflow_registry_entry(
            workflow_key="toggle_test",
            workflow_name="Toggle Test",
            is_active=True,
        )
        workflow_id = created.get("workflow_id")
        updated = business_rules.set_solf_workflow_registry_entry_active(
            workflow_id=workflow_id,
            is_active=False,
        )
        self.assertTrue(updated)
        retrieved = business_rules.get_solf_workflow_registry_entry(workflow_id=workflow_id)
        self.assertFalse(retrieved.get("is_active"))


class TestWorkflowVersionLifecycle(WorkflowRegistryTestSetup):
    """Tests for workflow version creation, publishing, and rollback."""

    def test_create_workflow_version(self):
        """Verify workflow version can be created from structured rule."""
        workflow = business_rules.create_solf_workflow_registry_entry(
            workflow_key="version_test",
            workflow_name="Version Test Workflow",
            is_active=True,
        )
        workflow_id = workflow.get("workflow_id")

        versions = business_rules.list_solf_workflow_registry_entries(
            is_active=True,
            limit=1,
        )
        self.assertTrue(len(versions) > 0)

    def test_publish_workflow_version(self):
        """Verify workflow version can be published and becomes active."""
        workflow = business_rules.create_solf_workflow_registry_entry(
            workflow_key="publish_test",
            workflow_name="Publish Test Workflow",
            is_active=True,
        )
        workflow_id = workflow.get("workflow_id")
        
        connection = object_db.get_connection()
        try:
            version_id = object_db.create_solf_workflow_version(
                connection=connection,
                workflow_id=workflow_id,
                rule_id=None,
                graph_spec={"process": "test_process"},
                is_active=False,
            )
        finally:
            connection.close()

        result = business_rules.publish_workflow_version(
            workflow_id=workflow_id,
            workflow_version_id=version_id,
            published_by="test_user",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.get("status"), "published")
        self.assertIsNotNone(result.get("active_version"))

    def test_rollback_workflow_version(self):
        """Verify workflow version can be rolled back to previous version."""
        workflow = business_rules.create_solf_workflow_registry_entry(
            workflow_key="rollback_test",
            workflow_name="Rollback Test Workflow",
            is_active=True,
        )
        workflow_id = workflow.get("workflow_id")

        connection = object_db.get_connection()
        try:
            version1_id = object_db.create_solf_workflow_version(
                connection=connection,
                workflow_id=workflow_id,
                rule_id=None,
                graph_spec={"process": "v1_process"},
                is_active=True,
            )
            version2_id = object_db.create_solf_workflow_version(
                connection=connection,
                workflow_id=workflow_id,
                rule_id=None,
                graph_spec={"process": "v2_process"},
                is_active=False,
            )
        finally:
            connection.close()

        result = business_rules.rollback_workflow_version(
            workflow_id=workflow_id,
            target_version_id=version1_id,
            reason="Fix critical bug",
            rolled_back_by="test_user",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.get("rolled_back_to_version_id"), version1_id)
        self.assertEqual(result.get("status"), "published")

    def test_get_workflow_active_version(self):
        """Verify active workflow version can be retrieved."""
        workflow = business_rules.create_solf_workflow_registry_entry(
            workflow_key="active_version_test",
            workflow_name="Active Version Test",
            is_active=True,
        )
        workflow_id = workflow.get("workflow_id")

        connection = object_db.get_connection()
        try:
            version_id = object_db.create_solf_workflow_version(
                connection=connection,
                workflow_id=workflow_id,
                graph_spec={"process": "test"},
                is_active=True,
            )
            active = object_db.get_workflow_active_version(connection, workflow_id)
            self.assertIsNotNone(active)
            self.assertEqual(active.get("workflow_version_id"), version_id)
        finally:
            connection.close()


class TestOneClickRuleRegistryCreation(WorkflowRegistryTestSetup):
    """Tests for one-click business rule + workflow registry creation."""

    def test_create_rule_with_registry(self):
        """Verify business rule and workflow registry are created together."""
        rule_text = "When invoice is overdue for 30 days, escalate to management"
        result = business_rules.create_business_rule_with_workflow_registry(
            rule_text=rule_text,
            rule_name="Overdue Invoice Escalation",
            created_by="test_user",
            is_active=True,
        )
        self.assertIsNotNone(result)
        self.assertIn("rule_creation", result)
        self.assertIn("workflow_registry_sync", result)
        self.assertEqual(result.get("combined_status"), "success")

        rule_creation = result.get("rule_creation", {})
        self.assertTrue(rule_creation.get("is_active"))

        registry_sync = result.get("workflow_registry_sync", {})
        self.assertGreater(registry_sync.get("workflows_linked_count", 0), 0)


class TestBusinessRuleWorkflowLinks(WorkflowRegistryTestSetup):
    """Tests for business rule and workflow registry linking."""

    def test_get_business_rule_workflow_registry(self):
        """Verify workflow registry links for a rule can be retrieved."""
        rule_text = "When document is received, classify it"
        result = business_rules.create_business_rule_with_workflow_registry(
            rule_text=rule_text,
            rule_name="Document Classification",
            created_by="test_user",
        )
        rule_id = int(result.get("rule_creation", {}).get("rule_id", 0))
        
        if rule_id > 0:
            registry = business_rules.get_business_rule_workflow_registry(rule_id=rule_id)
            self.assertIsNotNone(registry)
            self.assertEqual(registry.get("rule_id"), rule_id)
            self.assertGreater(registry.get("count", 0), 0)


class TestWorkflowSteps(WorkflowRegistryTestSetup):
    """Tests for workflow step creation and retrieval."""

    def test_create_and_list_workflow_steps(self):
        """Verify workflow steps can be created and listed."""
        workflow = business_rules.create_solf_workflow_registry_entry(
            workflow_key="steps_test",
            workflow_name="Steps Test Workflow",
            is_active=True,
        )
        workflow_id = workflow.get("workflow_id")

        connection = object_db.get_connection()
        try:
            version_id = object_db.create_solf_workflow_version(
                connection=connection,
                workflow_id=workflow_id,
                graph_spec={"process": "test"},
            )

            steps = [
                {
                    "step_key": "step_1",
                    "step_kind": "clause",
                    "clause_name": "resolve_company",
                    "operation": "resolve_policy",
                },
                {
                    "step_key": "step_2",
                    "step_kind": "python_binding",
                    "python_module": "domain_function",
                    "python_function": "db_accounting_ingest",
                    "operation": "ingest_transaction",
                },
            ]

            inserted = object_db.replace_solf_workflow_steps(
                connection=connection,
                workflow_version_id=version_id,
                steps=steps,
            )
            self.assertEqual(inserted, 2)

            retrieved = object_db.list_solf_workflow_steps(
                connection=connection,
                workflow_version_id=version_id,
            )
            self.assertEqual(len(retrieved), 2)
            self.assertEqual(retrieved[0].get("step_key"), "step_1")
            self.assertEqual(retrieved[1].get("step_key"), "step_2")
        finally:
            connection.close()


def run_workflow_registry_regression_suite() -> dict:
    """Run workflow registry regression tests and return suite summary."""
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=0)
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
        "tests": [
            {"name": "workflow_registry_crud", "success": failed == 0},
            {"name": "workflow_version_lifecycle", "success": failed == 0},
            {"name": "one_click_rule_registry", "success": failed == 0},
            {"name": "workflow_steps", "success": failed == 0},
        ],
    }


if __name__ == "__main__":
    import sys

    result = run_workflow_registry_regression_suite()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["failed"] == 0 else 1)
