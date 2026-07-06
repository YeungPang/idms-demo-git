# Phase 3 Completion Checklist

This checklist summarizes implemented operational observability for workflow automation.

## CI Status

[![Phase 3 Regressions](https://github.com/<OWNER>/<REPO>/actions/workflows/phase3-regressions.yml/badge.svg)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase3-regressions.yml)
[![Phase 3 Regressions (main)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase3-regressions.yml/badge.svg?branch=main)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase3-regressions.yml?query=branch%3Amain)

Replace `<OWNER>/<REPO>` with your GitHub repository path to activate the badge.

## 1. Monitoring and Health APIs

- [x] Added `WorkflowMonitor` module for centralized monitoring logic.
- [x] Added metric aggregation for events, transitions, and overdue entities.
- [x] Added health status evaluation with `healthy` / `warning` / `critical` states.

## 2. Snapshot Persistence

- [x] Added `workflow_metric_snapshots` table.
- [x] Added snapshot write API with optional label.
- [x] Added snapshot history retrieval API.

## 3. CLI and Runtime Integration

- [x] Added `monitor` command to interaction CLI.
- [x] Added monitor subcommands: `stats`, `health`, `snapshot`, `history`.
- [x] Initialized workflow monitor in `IDMSInteractionTools` runtime.

## 4. Schema Bootstrap Coverage

- [x] Included `workflow_metric_snapshots` in `sql_db.create_tables` bootstrap flow.
- [x] Included monitor snapshot indexes in shared index DDL.
- [x] Included snapshot table in recreate/drop list.

## 5. Regression Coverage

- [x] Added policy mode regression: `test_workflow_policy_modes.py`.
- [x] Added monitoring regression: `test_workflow_monitoring.py`.
- [x] Added Phase 3 consolidated runner: `run_phase3_regressions.py`.

## 6. Operational Outcome

- [x] Phase 2 baseline remains executable from Phase 3 runner.
- [x] Policy-mode assertions now covered across deny/escalate/allow paths.
- [x] Operational metrics and health checks are queryable and auditable.