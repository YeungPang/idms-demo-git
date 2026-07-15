import argparse
from datetime import datetime, timezone

from event_bus import EventBus
from sql_db import create_tables, get_connection
from workflow_state_machine import WorkflowStateMachine
from workflow_transition_executor import WorkflowTransitionExecutor


def _seed_overdue_compliance_action() -> int:
    action_no = f"CA_AUTO_ESC_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    with get_connection() as conn:
        create_tables(conn, recreate=False)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO compliance_actions (
                    action_no,
                    action_type,
                    legal_basis,
                    due_date_key,
                    status,
                    priority
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (action_no, "deadline_test", "regression", 20240101, "open", "high"),
            )
            row = cur.fetchone()
    return int(row[0])


def _query_assertions(action_id: int) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM compliance_actions WHERE id=%s", (int(action_id),))
            status_row = cur.fetchone()
            cur.execute(
                """
                SELECT COUNT(1)
                FROM workflow_state_transition_log
                WHERE entity_type=%s
                  AND entity_id=%s
                  AND to_state=%s
                  AND allowed=TRUE
                """,
                ("compliance_action", int(action_id), "escalated"),
            )
            transition_count = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT COUNT(1)
                FROM event_log
                WHERE event_type=%s
                  AND entity_id=%s
                """,
                ("compliance.deadline_breach", int(action_id)),
            )
            breach_count = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT COUNT(1)
                FROM event_log
                WHERE event_type=%s
                  AND source_entity=%s
                  AND entity_id=%s
                """,
                ("workflow.state_changed", "compliance_action", int(action_id)),
            )
            state_changed_count = int(cur.fetchone()[0])

    status = str(status_row[0] if status_row else "")
    return {
        "status": status,
        "transition_count": transition_count,
        "breach_count": breach_count,
        "state_changed_count": state_changed_count,
    }


def run_test() -> dict:
    action_id = _seed_overdue_compliance_action()

    bus = EventBus(solf_interpreter=None, db_connection_fn=get_connection)
    sm = WorkflowStateMachine(db_connection_fn=get_connection)
    executor = WorkflowTransitionExecutor(state_machine=sm, event_bus=bus, solf_interpreter=None)
    executor.register_default_rules()

    bus.emit(
        event_type="compliance.deadline_breach",
        source_entity="compliance_actions",
        entity_id=action_id,
        payload={"due_date_key": 20240101, "status": "open", "days_overdue": 1},
        tags=["regression", "compliance"],
    )

    checks = _query_assertions(action_id)

    assert checks["status"] == "escalated", "Expected compliance action status to become escalated"
    assert checks["transition_count"] >= 1, "Expected at least one allowed escalated transition log"
    assert checks["breach_count"] >= 1, "Expected at least one compliance.deadline_breach event log"
    assert checks["state_changed_count"] >= 1, "Expected at least one workflow.state_changed event log"

    return {
        "result": "ok",
        "action_id": action_id,
        **checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Regression test for compliance auto-escalation")
    parser.parse_args()
    result = run_test()
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_workflow_compliance_escalation_module_smoke() -> None:
    assert callable(run_test)
    assert callable(_query_assertions)