# Generic Reusable Python Functions + SOLF Clause Design

## 1. Objective

Define a reusable, domain-agnostic matching toolkit for:

1. split allocation (one payment -> many invoices), and
2. many-to-one matching (many payments -> one invoice),

while keeping orchestration in SOLF and algorithmic computation in Python.

This is designed to fit the existing IDMS runtime pattern:

- SOLF clause invocation via `SOLFInterpreter._invoke_clause(...)`
- Python extension dispatch in `solf_function.py` / `domain_function.py`
- Workflow step execution via `python_binding` and `clause` step kinds

---

## 2. Reusable Generic Python Function Set

These functions are intentionally generic so they can be reused for finance, inventory, order fulfillment, payroll balancing, and other matching domains.

## 2.1 Core normalization

1. `normalize_amount(value, currency, precision=2) -> Decimal`
2. `normalize_date(value, timezone=None) -> date`
3. `normalize_key_fields(record, field_map) -> dict`
4. `canonicalize_party_name(name) -> str`
5. `coerce_record_id(record, fallback_prefix) -> str`

Purpose:

- produce stable numeric/date/key representations before any matching logic.

## 2.2 Candidate generation

1. `build_candidate_edges(left_items, right_items, rules) -> list[CandidateEdge]`
2. `score_candidate_edge(left, right, scoring_rules) -> float`
3. `filter_candidates_by_blocking_rules(edges, blocking_rules) -> list[CandidateEdge]`

Purpose:

- construct and prune the bipartite candidate graph before optimization.

## 2.3 Allocation primitives

1. `allocate_one_to_many(source_amount, targets, tolerance, strategy) -> AllocationResult`
2. `allocate_many_to_one(sources, target_amount, tolerance, strategy) -> AllocationResult`
3. `allocate_many_to_many(sources, targets, tolerance, strategy) -> AllocationResult`
4. `resolve_residuals(allocation, residual_policy) -> AllocationResult`

Strategies (plug-in):

- `greedy_high_score`
- `oldest_first`
- `smallest_residual`
- `exact_subset_sum` (bounded)
- `hybrid_greedy_then_subset`

## 2.4 Constraint and policy checks

1. `validate_allocation_constraints(allocation, constraints) -> ValidationResult`
2. `enforce_invoice_policies(allocation, policy) -> AllocationResult`
3. `detect_ambiguity(candidate_groups, confidence_threshold) -> AmbiguityReport`
4. `should_pause_for_user_decision(ambiguity_report, policy) -> bool`

Purpose:

- make pause/escalation decisions deterministic and auditable.

## 2.5 Explainability and audit

1. `build_match_explanation(allocation, candidate_scores) -> list[dict]`
2. `build_reconciliation_summary(allocation, unmatched_left, unmatched_right) -> dict`
3. `build_audit_events(run_context, allocation_result) -> list[dict]`

Purpose:

- standardize explainability output independent of domain model.

## 2.6 Storage adapters

1. `persist_reconciliation_links(db, links, metadata) -> PersistResult`
2. `persist_reconciliation_status(db, entities, status_payload) -> PersistResult`
3. `emit_metrics(metric_sink, summary) -> None`

Purpose:

- isolate compute logic from DB write logic.

---

## 3. Recommended Generic Data Contract

Use one neutral payload shape for all matching operations:

```json
{
  "left_items": [
    {"id": "invoice_1", "amount": 1200.00, "date": "2026-06-01", "party": "ACME", "ref": "INV-001"}
  ],
  "right_items": [
    {"id": "payment_1", "amount": 700.00, "date": "2026-06-05", "party": "ACME", "ref": "PMT-991"},
    {"id": "payment_2", "amount": 500.00, "date": "2026-06-08", "party": "ACME", "ref": "PMT-992"}
  ],
  "matching_rules": {
    "blocking": ["same_party", "date_window_90d"],
    "scoring": ["amount_distance", "reference_similarity", "date_proximity"],
    "tolerance": 0.01
  },
  "allocation_policy": {
    "mode": "many_to_one",
    "strategy": "hybrid_greedy_then_subset",
    "allow_partial": true,
    "max_subset_size": 8
  },
  "pause_policy": {
    "ambiguity_confidence_threshold": 0.75,
    "require_user_confirmation_on_ambiguity": true
  },
  "context": {
    "country": "ch",
    "document_type": "bank_statement",
    "process": "bank_reconciliation"
  }
}
```

---

## 4. SOLF Clause Design (Template)

These clauses orchestrate the flow and call Python operations through existing wrappers (`workflow_domain_operation`, `workflow_compose_operations`).

## 4.1 Planning clause

```text
reconciliation_action_plan(_ctx) ⦃
    ↲({
        process: bank_reconciliation,
        steps: [prepare_candidates, split_allocation, many_to_one_match, finalize_reconciliation],
        pause_on_ambiguity: true
    })
⦄
```

## 4.2 Prepare candidates

```text
reconciliation_prepare_candidates(_ctx) ⦃
    ↲(workflow_domain_operation({
        operation: build_reconciliation_candidates,
        operation_payload: _ctx
    }))
⦄
```

## 4.3 Split allocation (one payment -> many invoices)

```text
reconciliation_split_allocation(_ctx) ⦃
    ↲(workflow_domain_operation({
        operation: allocate_split_payments,
        operation_payload: _ctx
    }))
⦄
```

## 4.4 Many-to-one matching (many payments -> one invoice)

```text
reconciliation_many_to_one_match(_ctx) ⦃
    ↲(workflow_domain_operation({
        operation: match_many_payments_to_invoice,
        operation_payload: _ctx
    }))
⦄
```

## 4.5 Ambiguity check and optional pause

```text
reconciliation_ambiguity_policy(_ctx) ⦃
    ↲(workflow_domain_operation({
        operation: evaluate_reconciliation_ambiguity,
        operation_payload: _ctx
    }))
⦄
```

```text
reconciliation_pause_if_needed(_ctx) ⦃
    (_needs_pause ≔ _ctx[needs_user_confirmation]) ⋀
    (_needs_pause = true) ⋀
    ↲({
        paused: true,
        reason: user_interaction,
        prompt: "Multiple valid reconciliation matches found. Please confirm preferred allocation."
    })
⦄
```

## 4.6 Persist and finalize

```text
reconciliation_finalize(_ctx) ⦃
    ↲(workflow_domain_operation({
        operation: persist_reconciliation_result,
        operation_payload: _ctx
    }))
⦄
```

## 4.7 One composed clause for pipeline step

```text
reconciliation_execute(_ctx) ⦃
    ↲(workflow_compose_operations({
        context: _ctx,
        stop_on_error: true,
        operations: [
            {
                action: workflow_domain_operation,
                payload: { operation: build_reconciliation_candidates, operation_payload: _ctx },
                save_as: candidate_build
            },
            {
                action: workflow_domain_operation,
                payload: { operation: allocate_split_payments, operation_payload: _ctx },
                save_as: split_allocation
            },
            {
                action: workflow_domain_operation,
                payload: { operation: match_many_payments_to_invoice, operation_payload: _ctx },
                save_as: many_to_one_allocation
            },
            {
                action: workflow_domain_operation,
                payload: { operation: evaluate_reconciliation_ambiguity, operation_payload: _ctx },
                save_as: ambiguity
            },
            {
                action: workflow_domain_operation,
                payload: { operation: persist_reconciliation_result, operation_payload: _ctx },
                save_as: persisted
            }
        ]
    }))
⦄
```

---

## 5. Python Operation Names To Add (Minimal Set)

Add these operations in `domain_function.py` and allow them in `workflow_domain_operation(...)`:

1. `build_reconciliation_candidates`
2. `allocate_split_payments`
3. `match_many_payments_to_invoice`
4. `evaluate_reconciliation_ambiguity`
5. `persist_reconciliation_result`

Each should return a stable shape:

```json
{
  "ok": true,
  "result": {...},
  "context_patch": {...},
  "metrics": {...},
  "audit_events": [...]
}
```

---

## 6. Mapping To Workflow Registry Steps

For generated workflow versions, prefer executable `python_binding` steps with built-in adapter:

- `python_module`: `workflow_pipeline_builtin_steps`
- `python_function`: `invoke_solf_action`
- `config.action`: `workflow_domain_operation`
- `config.payload.operation`: one of the operations above

Then optionally add SOLF `clause` steps for policy gates:

- `reconciliation_ambiguity_policy`
- `reconciliation_pause_if_needed`
- `reconciliation_finalize`

---

## 7. Why This Is Generic

The same primitives can support:

- invoice-payment reconciliation
- inventory receipt-to-PO allocation
- payroll payout-to-liability settlement
- claims adjudication allocations
- shipment-to-order split fulfillment

Only domain adapters and scoring rules change; orchestration and core matching remain reusable.

---

## 8. Implementation Order (Practical)

1. Implement generic normalization + candidate generation helpers.
2. Implement `allocate_split_payments` and `match_many_payments_to_invoice` using shared primitives.
3. Add ambiguity evaluator + pause policy bridge.
4. Add persistence adapter.
5. Register as workflow operations and wire to registry step synthesis.
6. Add regression scenarios:
   - one payment split to many invoices
   - many payments match one invoice
   - ambiguous competing matches requiring pause/resume

---

## 9. Success Criteria

1. Deterministic allocation for exact and near-exact totals.
2. Explainable decision trail per match edge.
3. Pause/resume on ambiguous matches.
4. Stable contract returned to workflow executor.
5. Reusable operations usable outside accounting with only config/domain adapters changed.
