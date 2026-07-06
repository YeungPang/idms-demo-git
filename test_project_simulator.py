"""ProjectSimulator regression tests aligned with current API contracts."""

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from project_simulator import ProjectSimulator, ScenarioModification


class BaseSimulatorTest(unittest.TestCase):
    def setUp(self):
        self.mock_connection_fn = MagicMock()
        self.mock_connection = MagicMock()
        self.mock_cursor = MagicMock()

        self.mock_connection_fn.return_value = self.mock_connection
        self.mock_connection.__enter__.return_value = self.mock_connection

        cursor_cm = MagicMock()
        cursor_cm.__enter__.return_value = self.mock_cursor
        self.mock_connection.cursor.return_value = cursor_cm

        self.simulator = ProjectSimulator(db_connection_fn=self.mock_connection_fn)


class TestScenarioModification(unittest.TestCase):
    def test_fields(self):
        mod = ScenarioModification("RESOURCE", "resource_count", 1, 2, "add")
        self.assertEqual(mod.param_type, "RESOURCE")
        self.assertEqual(mod.param_name, "resource_count")
        self.assertEqual(mod.original_value, 1)
        self.assertEqual(mod.modified_value, 2)


class TestProjectSimulator(BaseSimulatorTest):
    def test_create_scenario(self):
        self.mock_cursor.fetchone.return_value = (10, datetime.now(timezone.utc))
        result = self.simulator.create_scenario(
            base_project_id=1,
            scenario_name="Plan B",
            scenario_type="what_if",
            description="desc",
            modifications={"budget_percent": {"original": 100, "modified": 120}},
        )
        self.assertEqual(result["scenario_id"], 10)
        self.assertEqual(result["base_project_id"], 1)

    def test_clone_project_scenario(self):
        self.mock_cursor.fetchone.side_effect = [
            (1, "P-1", "client", 100000, None, None, "active"),
            (22, datetime.now(timezone.utc)),
        ]
        self.mock_cursor.fetchall.return_value = [(1, "Task A", 8, None, None, None)]

        result = self.simulator.clone_project_scenario(
            base_project_id=1,
            scenario_name="Clone 1",
            modifications=[ScenarioModification("BUDGET", "budget_percent", 100, 120, "increase")],
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["scenario_id"], 22)

    def test_simulate_project_schedule(self):
        self.mock_cursor.fetchone.return_value = (1, json.dumps({}))
        self.mock_cursor.fetchall.return_value = [
            (1, "Task A", 8, None, None, None, "alice"),
            (2, "Task B", 16, None, None, 1, "alice"),
        ]

        result = self.simulator.simulate_project_schedule(1)
        self.assertTrue(result["success"])
        self.assertIn("total_duration_days", result)
        self.assertIn("resource_utilization_percent", result)

    def test_calculate_budget_impact(self):
        self.mock_cursor.fetchone.return_value = (
            1,
            json.dumps({"budget_percent": {"original": 100, "modified": 120}}),
            100000,
        )
        result = self.simulator.calculate_budget_impact(1)
        self.assertTrue(result["success"])
        self.assertEqual(result["new_budget"], 120000)

    @patch.object(ProjectSimulator, "simulate_project_schedule")
    @patch.object(ProjectSimulator, "calculate_budget_impact")
    def test_compare_scenarios(self, mock_budget, mock_schedule):
        mock_schedule.side_effect = [
            {"total_duration_days": 12, "conflicts": []},
            {"total_duration_days": 10, "conflicts": ["x"]},
        ]
        mock_budget.side_effect = [
            {"new_budget": 100000, "budget_delta_percent": 0},
            {"new_budget": 110000, "budget_delta_percent": 10},
        ]
        result = self.simulator.compare_scenarios(1, 2)
        self.assertTrue(result["success"])
        self.assertEqual(result["duration_delta_days"], -2)
        self.assertEqual(result["budget_delta_percent"], 10)

    def test_list_scenarios(self):
        self.mock_cursor.fetchall.return_value = [
            (1, 100, "Plan A", "what_if", "d", json.dumps({}), datetime.now(timezone.utc), "system")
        ]
        result = self.simulator.list_scenarios(base_project_id=100, status="draft")
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)

    def test_get_scenario_details(self):
        self.mock_cursor.fetchone.side_effect = [
            (1, 100, "Plan A", "d", json.dumps({}), datetime.now(timezone.utc), "system"),
            ("schedule_simulation", json.dumps({"total_duration_days": 5}), json.dumps({"new_budget": 10}), None),
        ]
        result = self.simulator.get_scenario_details(1)
        self.assertTrue(result["success"])
        self.assertEqual(result["scenario_id"], 1)
        self.assertIn("latest_analysis", result)


if __name__ == "__main__":
    unittest.main()
