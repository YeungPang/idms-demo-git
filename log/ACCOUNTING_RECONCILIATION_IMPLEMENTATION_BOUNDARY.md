# Accounting Reconciliation: Implementation Boundary

## Purpose
Define which reconciliation capabilities should be implemented during development time (hard-coded platform features) versus runtime configurable (workflow/rule/alias/policy).

---

## 1) Development-Time Features (Must Be Pre-Programmed)

These are deterministic, compliance-sensitive, and safety-critical. They should be implemented as built-in workflow steps/tools with explicit contracts and tests.

### 1.1 Financial Posting and Ledger Safety
- Double-entry posting engine and balancing checks.
- Idempotency keys for posting and replay protection.
- Legal entity and chart-of-accounts validation.
- Period locks and posting-date guardrails.
- Immutable audit trail for posting decisions.

### 1.2 Reconciliation State Machine
- Canonical statuses: unmatched, candidate, matched, exception, approved, posted, reversed.
- Allowed transitions and terminal states.
- Concurrency handling and conflict resolution.
- Retry semantics for transient failures.

### 1.3 Match Execution Primitives
- Deterministic scoring combiner (rule score + semantic score + confidence floor).
- One-to-one, one-to-many, many-to-one matching executor with residual handling.
- Split allocation engine for partial payments.
- Tolerance engines (amount/date/currency) with absolute and relative thresholds.

### 1.4 Compliance and Explainability
- Evidence capture for each match decision (fields, chunks, rule versions, scores).
- Mandatory explainability payload for approvals/postings.
- Data retention and masking rules for regulated fields.

### 1.5 Integration Contracts
- Stable API schemas for candidate retrieval, approval, posting, reversal, and exception handling.
- Versioned workflow step contracts for backward compatibility.
- Maintenance operations and guarded admin endpoints.

---

## 2) Runtime-Configurable Features (Workflow/Rules/Policies)

These vary by business unit, country policy, and process flavor. They should be configured without code changes.

### 2.1 Matching Policy Parameters
- Thresholds (semantic similarity, confidence floor).
- Date/amount tolerance ranges.
- Candidate limit and ranking strategy selection.
- Priority of exact identifiers over fuzzy evidence.

### 2.2 Workflow Routing and Aliases
- Workflow key to alias mapping (purpose, scope, collection).
- Tenant/workflow scoping values.
- Rule-to-workflow sync behavior and active version selection.

### 2.3 Process Variants
- Country/legal-entity specific approval steps.
- Exception routing (manual review queues and escalation).
- Auto-approve criteria for low-risk matches.

### 2.4 Document and Source Policies
- Doc-type eligibility for matching index.
- Collection selection policy (default vs workflow-specific override).
- Ingestion processing directives and lifecycle rule picks.

---

## 3) Borderline Features (Hybrid)

Implement core engine in code, expose policy knobs at runtime.

- Semantic matching:
  - Code: embedding/query pipeline, strict scope filtering, safety checks.
  - Runtime: purpose allowlist per tenant/workflow, threshold knobs.

- Split allocation:
  - Code: allocation math, residual rounding, invariants.
  - Runtime: allocation strategy preference and tolerance policy.

- Approval gating:
  - Code: approval state machine and required evidence fields.
  - Runtime: who can approve and risk thresholds.

---

## 4) Immediate Implementation Guidance for IDMS

### Keep as Development-Time in Current Phase
- Accounting posting and reversal primitives.
- Reconciliation state transitions and status lifecycle.
- Deterministic candidate scoring combiner.
- Queue worker behavior and failure/retry transitions.
- Canonical evidence payload schema.

### Expose as Runtime Config First
- Workflow alias mapping and workflow-key shortcuts.
- Matching thresholds and tolerance policy.
- Candidate list limits and sorting mode.
- Auto-approval policy by risk band.

---

## 5) Decision Rule for New Features

Use this rule before adding a new reconciliation capability:

- If failure can cause financial misstatement, compliance breach, or irreversible posting errors: implement as development-time built-in feature.
- If feature is primarily policy variation and can be constrained by validated parameters: expose as runtime configurable.

---

## 6) Recommended Next Refactor Steps

1. Formalize built-in step interfaces for reconciliation core operations (match, allocate, approve, post, reverse).
2. Move all tunable thresholds into a versioned policy object linked to workflow version.
3. Add a policy validator to reject unsafe runtime configurations.
4. Extend regression tests to include policy-boundary tests (safe/unsafe configs).
5. Add per-step explainability snapshots into pipeline run records.
