import argparse

from sql_db import create_tables, get_connection
from workflow_monitor import WorkflowMonitor


def run_test() -> dict:
    with get_connection() as conn:
        create_tables(conn, recreate=False)

    monitor = WorkflowMonitor(db_connection_fn=get_connection)
    metrics = monitor.collect_metrics(window_hours=24)
    health = monitor.health(window_hours=24)
    snapshot = monitor.snapshot(label="phase3_regression")
    history = monitor.history(limit=5)

    assert isinstance(metrics, dict), "Metrics must be a dictionary"
    assert isinstance(metrics.get("events"), dict), "Metrics should include events"
    assert isinstance(metrics.get("transitions"), dict), "Metrics should include transitions"
    assert isinstance(metrics.get("overdue"), dict), "Metrics should include overdue"
    assert str(health.get("status") or "") in {"healthy", "warning", "critical"}, "Health status must be valid"
    assert int(snapshot.get("snapshot_id") or 0) > 0, "Snapshot id should be positive"
    assert len(history) >= 1, "Snapshot history should include at least one row"

    return {
        "result": "ok",
        "health_status": health.get("status"),
        "snapshot_id": int(snapshot.get("snapshot_id") or 0),
        "history_count": len(history),
    }


def run_monitoring_regression_suite() -> dict:
    """Compatibility wrapper used by consolidated regression runner."""
    result = run_test()
    return {
        "result": "ok",
        "total": 1,
        "passed": 1,
        "failed": 0,
        "failures": [],
        "tests": [{"name": "workflow_monitoring", "success": True, "result": result}],
    }


def test_workflow_monitoring_module_smoke() -> None:
    assert callable(run_test)
    assert callable(run_monitoring_regression_suite)


def main() -> int:
    parser = argparse.ArgumentParser(description="Regression test for workflow monitoring")
    parser.parse_args()
    result = run_test()
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())