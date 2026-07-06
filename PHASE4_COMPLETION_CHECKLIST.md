# Phase 4 Completion Checklist

This checklist summarizes implemented reliability automation for workflow operations.

## CI Status

[![Phase 4 Regressions](https://github.com/<OWNER>/<REPO>/actions/workflows/phase4-regressions.yml/badge.svg)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase4-regressions.yml)
[![Phase 4 Regressions (main)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase4-regressions.yml/badge.svg?branch=main)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase4-regressions.yml?query=branch%3Amain)

Replace `<OWNER>/<REPO>` with your GitHub repository path to activate the badge.

## 1. Alert Policy Engine

- [x] Added `WorkflowReliabilityManager` for policy-based alert evaluation.
- [x] Added default alert signals for overdue workload and deny ratios.
- [x] Added policy table support to enable threshold tuning over time.

## 2. Incident Lifecycle

- [x] Added incident table with `open`, `acknowledged`, and `resolved` status flow.
- [x] Added incident deduplication via `incident_key` and status tracking.
- [x] Added manual incident actions: acknowledge and resolve.
- [x] Added auto-resolution when metrics return below threshold.

## 3. Runtime and CLI Integration

- [x] Integrated reliability manager in `IDMSInteractionTools` startup.
- [x] Added monitor subcommands: `alerts`, `reconcile`, `incidents`, `incident`.
- [x] Added incident command args: `--incident-id`, `--incident-action`, `--actor-ref`.

## 4. Schema Coverage

- [x] Added `workflow_alert_policies` table to schema bootstrap.
- [x] Added `workflow_alert_incidents` table to schema bootstrap.
- [x] Added alert-policy and incident indexes in shared index DDL.
- [x] Added Phase 4 tables to recreate/drop list.

## 5. Regression Coverage

- [x] Added reliability lifecycle regression: `test_workflow_alert_reconciliation.py`.
- [x] Added consolidated runner: `run_phase4_regressions.py`.
- [x] Phase 4 runner includes Phase 3 baseline.
