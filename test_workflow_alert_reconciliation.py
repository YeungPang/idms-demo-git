import argparse

from sql_db import create_tables, get_connection
from workflow_monitor import WorkflowMonitor
from workflow_reliability_manager import WorkflowReliabilityManager


def _reset_runtime_tables() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM workflow_alert_incidents")
            cur.execute("DELETE FROM workflow_alert_policies")
            cur.execute("DELETE FROM workflow_metric_snapshots")
            cur.execute("DELETE FROM workflow_state_transition_log")
            cur.execute("DELETE FROM event_log")
            cur.execute("DELETE FROM project_tasks")
            cur.execute("DELETE FROM compliance_actions")
            cur.execute("DELETE FROM workflow_cases")


def run_test() -> dict:
    with get_connection() as conn:
        create_tables(conn, recreate=False)

    _reset_runtime_tables()

    monitor = WorkflowMonitor(db_connection_fn=get_connection)
    manager = WorkflowReliabilityManager(db_connection_fn=get_connection, monitor=monitor, event_bus=None)

    # Seed overdue entities to trigger warning incidents.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE project_code = 'P4-REG-1'")
            cur.execute(
                """
                INSERT INTO projects (project_code, name, status)
                VALUES ('P4-REG-1', 'Phase 4 Regression Project', 'active')
                RETURNING id
                """
            )
            project_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO project_tasks (project_id, name, status, due_date)
                VALUES (%s, 'Phase4 overdue task', 'open', CURRENT_DATE - INTERVAL '2 day')
                """
                ,
                (project_id,),
            )
            cur.execute(
                """
                INSERT INTO workflow_cases (case_no, case_type, subject_ref, initiated_by_ref, current_step, sla_due_at, status)
                VALUES ('CASE-P4-1', 'ops', 1001, 2001, 'review', NOW() - INTERVAL '2 hour', 'open')
                """
            )

    first = manager.reconcile_alerts(window_hours=24, actor_ref="regression")
    assert first.get("result") == "ok", "Reconcile should succeed"
    assert int(first.get("breached_count") or 0) >= 2, "Expected at least two breached metrics"
    assert int(first.get("opened_or_updated_count") or 0) >= 2, "Expected incidents to be opened"

    open_incidents = manager.list_incidents(status="open", limit=20)
    assert len(open_incidents) >= 2, "Expected open incidents after reconcile"

    first_id = int(open_incidents[0]["id"])
    ack_result = manager.update_incident_status(first_id, action="acknowledge", actor_ref="regression")
    assert ack_result.get("success") is True, "Incident acknowledge should succeed"
    assert str((ack_result.get("incident") or {}).get("status")) == "acknowledged", "Incident should be acknowledged"

    # Clear overdue conditions and ensure incidents auto-resolve.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE project_tasks SET status='done' WHERE name='Phase4 overdue task'")
            cur.execute("UPDATE workflow_cases SET status='approved' WHERE case_no='CASE-P4-1'")

    second = manager.reconcile_alerts(window_hours=24, actor_ref="regression")
    active_keys = set(second.get("active_incident_keys") or [])
    assert "overdue.project_tasks" not in active_keys, "Task overdue breach should clear after remediation"
    assert "overdue.workflow_cases" not in active_keys, "Workflow case overdue breach should clear after remediation"
    assert int(second.get("resolved_count") or 0) >= 2, "Previously open incidents should resolve"

    resolved_incidents = manager.list_incidents(status="resolved", limit=20)
    assert len(resolved_incidents) >= 2, "Expected resolved incidents after remediation"

    return {
        "result": "ok",
        "opened_or_updated": int(first.get("opened_or_updated_count") or 0),
        "resolved": int(second.get("resolved_count") or 0),
        "resolved_count_total": len(resolved_incidents),
    }


def run_alert_reconciliation_suite() -> dict:
    """Compatibility wrapper used by consolidated regression runner."""
    result = run_test()
    return {
        "result": "ok",
        "total": 1,
        "passed": 1,
        "failed": 0,
        "failures": [],
        "tests": [{"name": "workflow_alert_reconciliation", "success": True, "result": result}],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Regression test for Phase 4 workflow alert reconciliation")
    parser.parse_args()
    result = run_test()
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_workflow_alert_reconciliation_module_smoke() -> None:
    assert callable(run_test)
    assert callable(run_alert_reconciliation_suite)
