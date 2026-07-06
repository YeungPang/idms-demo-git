import argparse

from workflow_transition_executor import WorkflowTransitionExecutor


def run_test() -> dict:
    executor = WorkflowTransitionExecutor(state_machine=None, event_bus=None, solf_interpreter=None)

    workflow_terminal_deny = executor._policy_allows(
        {
            "entity_type": "workflow_case",
            "to_state": "approved",
            "event": "manual_transition",
            "actor_ref": "system",
        }
    )
    project_task_done_deny = executor._policy_allows(
        {
            "entity_type": "project_task",
            "to_state": "done",
            "event": "manual_transition",
            "actor_ref": "system",
        }
    )
    compliance_escalate = executor._policy_allows(
        {
            "entity_type": "compliance_action",
            "to_state": "escalated",
            "event": "compliance_deadline_breach",
            "actor_ref": "automation",
        }
    )
    default_allow = executor._policy_allows(
        {
            "entity_type": "workflow_case",
            "to_state": "in_review",
            "event": "case_updated",
            "actor_ref": "analyst",
        }
    )

    assert str(workflow_terminal_deny.get("mode")) == "deny", "workflow_case terminal should be denied"
    assert str(project_task_done_deny.get("mode")) == "deny", "project_task done should be denied"
    assert str(compliance_escalate.get("mode")) == "escalate", "compliance deadline breach should escalate"
    assert str(default_allow.get("mode")) == "allow", "non-restricted transition should allow"

    return {
        "result": "ok",
        "workflow_terminal_mode": workflow_terminal_deny.get("mode"),
        "project_task_done_mode": project_task_done_deny.get("mode"),
        "compliance_escalation_mode": compliance_escalate.get("mode"),
        "default_mode": default_allow.get("mode"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Regression test for workflow transition policy modes")
    parser.parse_args()
    result = run_test()
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())