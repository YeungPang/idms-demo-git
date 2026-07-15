import argparse
from datetime import datetime, timezone

from event_bus import EventBus
from sql_db import create_tables, get_connection
from workflow_state_machine import WorkflowStateMachine
from workflow_transition_executor import WorkflowTransitionExecutor


def _seed_open_workflow_case() -> int:
    case_no = f"WF_DENY_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    with get_connection() as conn:
        create_tables(conn, recreate=False)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow_cases (
                    case_no,
                    case_type,
                    subject_ref,
                    initiated_by_ref,
                    current_step,
                    status
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (case_no, "regression", 1, 1, "open", "open"),
            )
            row = cur.fetchone()
    return int(row[0])


def _query_assertions(case_id: int) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM workflow_cases WHERE id=%s", (int(case_id),))
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
                ("workflow_case", int(case_id), "approved"),
            )
            allowed_terminal_transition_count = int(cur.fetchone()[0])

    status = str(status_row[0] if status_row else "")
    return {
        "status": status,
        "allowed_terminal_transition_count": allowed_terminal_transition_count,
    }


def run_test() -> dict:
    case_id = _seed_open_workflow_case()

    bus = EventBus(solf_interpreter=None, db_connection_fn=get_connection)
    sm = WorkflowStateMachine(db_connection_fn=get_connection)
    executor = WorkflowTransitionExecutor(state_machine=sm, event_bus=bus, solf_interpreter=None)

    result = executor._apply_transition(
        entity_type="workflow_case",
        entity_id=case_id,
        to_state="approved",
        event="manual_transition",
        actor_ref="system",
        metadata={"source": "regression_negative"},
    )

    checks = _query_assertions(case_id)
    policy = result.get("policy") if isinstance(result, dict) else {}

    assert bool(result.get("success")) is False, "Expected transition application to fail"
    assert str(policy.get("mode") or "") == "deny", "Expected policy mode to be deny"
    assert checks["status"] == "open", "Expected workflow case to remain open"
    assert checks["allowed_terminal_transition_count"] == 0, "Expected no allowed approved transition log"

    return {
        "result": "ok",
        "case_id": case_id,
        "denial_reason": str(policy.get("reason") or ""),
        **checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Regression test for denied terminal workflow transition")
    parser.parse_args()
    result = run_test()
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_workflow_terminal_transition_denial_module_smoke() -> None:
    assert callable(run_test)
    assert callable(_query_assertions)