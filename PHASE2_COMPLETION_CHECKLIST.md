# Phase 2 Completion Checklist

This checklist summarizes the implemented Phase 2 workflow automation baseline.

## 1. State Machine and Audit

- [x] Deterministic state machine for `workflow_case`, `project_task`, and `compliance_action`.
- [x] Transition validation via `can_transition` before updates.
- [x] Transition audit log table `workflow_state_transition_log` created and indexed.
- [x] Transition history retrieval API implemented.

## 2. Event-Driven Transition Executor

- [x] Event subscriptions for:
  - `task.overdue`
  - `workflow.approval_recorded`
  - `compliance.deadline_breach`
  - `workflow.case_sla_breach`
- [x] Automatic state transitions triggered by matching events.
- [x] `workflow.state_changed` event emitted on successful transition.

## 3. Policy Hardening

- [x] SOLF transition policy clauses defined in `solf_script.txt`.
- [x] Runtime hardening fallback implemented in Python for deterministic enforcement.
- [x] Terminal `workflow_case` state transitions restricted to `approval_recorded` event.
- [x] `project_task -> done` restricted to `task_completed` event.
- [x] `compliance_action -> escalated` marked as escalation mode for deadline breach event.

## 4. Maintenance Scanner

- [x] State maintenance command scans overdue entities.
- [x] Emits maintenance events for tasks, compliance actions, and workflow cases.
- [x] Compliance breach events now auto-transition overdue actions to `escalated`.

## 5. Database and Runtime Safety

- [x] Runtime tables ensured at startup for events, notifications, action logs, and transitions.
- [x] Legacy schema repair support available for drifted DBs.
- [x] Non-destructive bootstrap path validated.

## 6. Verification Artifacts

- [x] Manual CLI validation for transition rules and maintenance flows.
- [x] Regression script added: `test_workflow_compliance_escalation.py`.
- [x] Regression script added: `test_workflow_terminal_transition_denial.py`.
- [x] Combined runner added: `run_phase2_regressions.py`.
- [x] Regression verifies:
  - compliance action status moves `open -> escalated`
  - transition log captures allowed escalation
  - `compliance.deadline_breach` event logged
  - `workflow.state_changed` event logged
  - manual terminal `workflow_case` transition is denied without approval event

## 7. Optional Next Checks

- [x] Add CI job to run workflow regression scripts.
- [x] Add negative regression for denied terminal transitions without approval event.
- [ ] Add policy-mode assertions for `deny` and `escalate` paths across entity types.