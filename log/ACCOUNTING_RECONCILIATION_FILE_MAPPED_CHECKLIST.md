# Accounting Reconciliation: File-Mapped Code vs Policy Checklist

## Scope
This checklist maps current implementation points to either:
- **Code-owned core** (keep development-time, deterministic, safety-critical)
- **Policy/runtime-owned** (move to workflow/rule/config where possible)

---

## 1) tx_match_index.py

### Keep Code-Owned Core
- `index_rows(...)`
  - Keep embedding + upsert + replace semantics deterministic.
  - Keep id hashing and replacement behavior code-owned.
- `query_rows(...)`
  - Keep hard filter enforcement (`purpose`, `workflow_scope`, `tenant_scope`) as mandatory code checks.
  - Keep hard limits and query guardrails in code.
- `list_candidates(...)`
  - Keep pagination/cursor and strict filter application deterministic.
- `_ensure_collection(...)`, `_delete_existing_rows(...)`
  - Keep collection/schema lifecycle and row replacement in code.
- `_has_required_fields(...)`, `_build_feature_text(...)`
  - Keep minimum row validity and canonical feature text shaping deterministic.

### Move/Expose as Policy Runtime
- `_allowed_purposes(...)` / `is_allowed_purpose(...)`
  - Today env-driven; target workflow-version policy object with tenant override controls.
- `_allow_collection_override(...)` and `resolve_collection_name(...)`
  - Keep safety checks in code, move allowed override behavior to policy flags per workflow.
- `_eligible_doc_type(...)` / `is_eligible_doc_type(...)`
  - Move doc-type eligibility from global env to workflow policy profile.
- `TX_MATCH_MIN_FIELDS` behavior via `_has_required_fields(...)`
  - Keep validator in code, expose required-field profile by workflow purpose.

### Immediate Tasks
1. Introduce `matching_policy` object in workflow version metadata.
2. Resolve policy once per request in router layer; pass normalized policy to `tx_match_index` functions.
3. Keep code defaults as fallback when policy not supplied.

---

## 2) workflow_pipeline_executor.py

### Keep Code-Owned Core
- `start_pipeline_run(...)`, `resume_pipeline_run(...)`, `_execute_run(...)`
  - Keep run lifecycle state machine and transition safety in code.
- `_execute_step(...)`
  - Keep centralized pause/failure semantics deterministic.
- Pause model (`PipelineStepPauseRequired`) and terminal transitions
  - Keep canonical reasons and status writes code-owned.
- `cancel_pipeline_run(...)`
  - Keep terminal-state constraints and cancel rules in code.

### Move/Expose as Policy Runtime
- Step-level pause thresholds and gating criteria
  - Keep mechanics in code; move threshold values and acceptance criteria into step config/policy.
- Resume enrichment behavior (`user_response`, `doc_refs` merge strategy)
  - Keep merge algorithm in code; expose allowable keys and constraints via policy.

### Immediate Tasks
1. Add per-workflow run policy section in `graph_spec` metadata:
   - max step retries
   - allowed pause reasons
   - required evidence keys before approval/posting
2. Add policy validator before run start to reject unsafe configs.
3. Add explicit run-level explainability snapshot for each transition.

---

## 3) workflow_pipeline_builtin_steps.py

### Keep Code-Owned Core
- `invoke_solf_action(...)`
  - Keep execution wrapper safety, error capture, and output shaping in code.
- `require_fields(...)`, `require_user_confirmation(...)`, `require_document_refs(...)`
  - Keep pause signaling mechanics and canonical return contracts deterministic.

### Move/Expose as Policy Runtime
- Required fields list and required doc-type list
  - Keep validation code, but drive values from workflow-step config.
- Confirmation keys and accepted values
  - Keep parser/normalization in code; accepted values should be policy-defined.

### Immediate Tasks
1. Introduce strict schema for builtin step config payloads.
2. Reject unknown fields in builtin configs to avoid silent policy drift.

---

## 4) business_rules.py (workflow and alias layer)

### Keep Code-Owned Core
- `sync_business_rule_workflow_registry(...)`
  - Keep transactional sync and version creation logic deterministic.
- `_build_steps_for_rule_registry(...)`
  - Keep generation of safe canonical step contracts in code.
- `publish_workflow_version(...)`, `rollback_workflow_version(...)`
  - Keep release and rollback semantics immutable and audited.
- `resolve_workflow_resource_alias(...)`
  - Keep alias resolution safety checks and active-version resolution deterministic.
- `_derive_workflow_resource_aliases(...)`, `_infer_matching_purpose(...)`
  - Keep derivation algorithm code-owned for consistency and testability.

### Move/Expose as Policy Runtime
- Workflow process hints and optional process additions
  - Keep normalization/canonicalization in code, expose allowable process families via policy allowlist.
- Alias namespaces and optional alias patterns
  - Keep canonical alias schema in code, expose permitted prefixes per domain/workflow.

### Immediate Tasks
1. Add `workflow_policy_profile` attached to workflow version.
2. During sync, validate generated steps and aliases against policy profile.
3. Add regression tests for policy rejection paths (unsafe process, unsafe alias, unsafe publish).

---

## 5) High-Priority Refactor Sequence

1. **Policy Object First**
   - Add a single versioned policy schema for reconciliation and matching.
2. **Read-Path Integration**
   - Match/query/candidate endpoints resolve policy and pass normalized values to core functions.
3. **Validation Gate**
   - Add policy validator called from workflow sync/publish.
4. **Auditability**
   - Record policy version/hash in run records and matching responses.
5. **Regression Coverage**
   - Add tests for safe/unsafe policy boundaries and compatibility fallbacks.

---

## 6) Decision Matrix for New Changes

Use this matrix when adding a reconciliation feature:

- If wrong behavior can cause posting errors, compliance breach, or irreversible financial impact:
  - implement as **code-owned core**.
- If behavior is workflow or tenant variation constrained by validation:
  - implement as **policy/runtime-owned**.
- If mixed:
  - core algorithm in code, thresholds/allowlists/routing in policy.

---

## 7) Suggested First Concrete PR Split

### PR-A (Safety Foundation)
- Add workflow-version `matching_policy` and `reconciliation_policy` schema.
- Add validator + hash persistence.

### PR-B (Matching Policy Wiring)
- Wire policy into `tx_match_index` callers.
- Keep defaults for backward compatibility.

### PR-C (Workflow Policy Guardrails)
- Enforce policy at workflow sync/publish.
- Add negative tests for invalid policy.

### PR-D (Explainability)
- Persist policy hash/version in pipeline run + matching API output.
