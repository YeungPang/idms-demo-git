# Conditional Runtime Extensions for Prime Workflows

## Goal
Allow users to define additional rules and process steps at IDMS runtime and attach them to a prime predefined workflow (for example accounting reconciliation) without changing the prime workflow definition.

Extensions must support:
- conditional activation based on run status, step outcomes, and runtime state data
- tenant/workflow/domain scoping
- deterministic ordering and safety validation
- auditable explainability of why each extension did or did not run

## Current Foundation in IDMS
IDMS already has strong primitives that this design reuses:
- workflow version and ordered step model in solf_workflow_steps
- persistent run status and current_context state in workflow_pipeline_run
- per-step statuses and pause semantics in workflow_pipeline_run_step
- executable builtins and domain operations through python_binding and invoke_solf_action

This means the extension feature can be implemented as an additive layer around step resolution/execution, not a rewrite.

## Design Summary
Introduce Workflow Extension Packs that are attached to a prime workflow key/version.

At run start and before each step execution, an Extension Resolver evaluates extension conditions against:
- run status
- current context values
- latest step outcomes
- workflow/domain metadata

Resolver outputs a deterministic extension execution plan:
- pre-step inserts
- post-step inserts
- on-pause hooks
- on-failure hooks
- on-complete hooks

The pipeline executor executes prime steps plus resolved extension steps while recording extension decisions and outcomes in run metadata.

## Core Concepts

### 1. Prime Workflow
The existing workflow version (for example template::accounting_reconciliation_full) remains unchanged and authoritative.

### 2. Extension Pack
A versioned object containing one or more extension rules.

Each extension rule defines:
- target selector: workflow_key, workflow_version_id, domain, tenant
- hook point: before_step, after_step, on_pause, on_failure, on_complete, on_run_start
- condition expression: evaluated against run context and status
- injected steps: same compatible step schema as existing workflow steps
- precedence and conflict policy: ordering and duplicate handling
- safety profile: allowed operations and max inserted steps

### 3. Condition Expression
A constrained expression DSL (or JSON predicate) that supports:
- run.status equals paused/running/failed/completed
- context field comparisons (equals, in, exists, numeric/date comparisons)
- previous step outcome checks (step key success/failure/paused)
- domain flags (for example accounting validation strict mode)

No arbitrary Python execution in conditions.

### 4. Extension Plan
Resolver output for one run snapshot:
- selected extension rules
- rejected extension rules with reasons
- ordered injected steps by hook point
- hash/signature for replayability and audit

## Data Model Additions

### A. workflow_extension_pack
Fields:
- extension_pack_id
- extension_key (stable key)
- extension_name
- description
- scope_json (workflow/domain/tenant selectors)
- is_active
- created_by
- created_at

### B. workflow_extension_version
Fields:
- extension_version_id
- extension_pack_id
- version_no
- status (draft/review/published)
- conditions_schema_version
- metadata_json
- is_active
- created_by
- created_at

### C. workflow_extension_rule
Fields:
- extension_rule_id
- extension_version_id
- rule_key
- hook_point
- target_step_key nullable
- condition_json
- precedence integer
- conflict_policy (skip/replace/merge/fail)
- inserted_steps_json
- is_active

### D. workflow_extension_attachment
Fields:
- attachment_id
- workflow_key
- workflow_version_id nullable
- extension_pack_id
- effective_from
- effective_until nullable
- priority
- tenant_scope nullable
- is_active

### E. workflow_pipeline_run_extension_log
Fields:
- run_extension_log_id
- run_id
- extension_rule_id
- decision (selected/rejected/skipped/executed/failed)
- decision_reason
- decision_context_snapshot jsonb
- executed_step_keys jsonb
- created_at

## API Design

### 1. Extension CRUD
- POST /api/business-rules/workflow-extensions
- GET /api/business-rules/workflow-extensions
- GET /api/business-rules/workflow-extensions/{extension_key}
- POST /api/business-rules/workflow-extensions/{extension_id}/publish
- POST /api/business-rules/workflow-extensions/{extension_id}/rollback

### 2. Attachment APIs
- POST /api/business-rules/workflow-extensions/attach
- POST /api/business-rules/workflow-extensions/detach
- GET /api/business-rules/workflow-extensions/attachments?workflow_key=...

### 3. Explain/Simulation APIs
- POST /api/business-rules/workflow-extensions/resolve-preview
  Returns selected/rejected rules for supplied run context.
- POST /api/business-rules/workflow-extensions/validate
  Validates condition schema, step compatibility, and policy constraints.

### 4. Run Introspection
- GET /api/pipeline-runs/{run_id}/extensions
  Returns extension decisions and execution outcomes.

## Executor Integration

### Phase 1 integration point
At run start:
1. load prime steps
2. resolve active extension attachments for workflow_key/workflow_version/tenant
3. evaluate rules with hook point on_run_start
4. prepend selected injected steps to execution list

### Phase 2 integration point
Before each prime step:
1. evaluate before_step rules targeted at this step key
2. execute selected injected steps
3. execute prime step
4. evaluate after_step rules
5. execute selected injected steps

### Pause/Failure hooks
When a step pauses or fails:
- evaluate on_pause or on_failure extension rules
- execute allowed remediation/notification steps
- write decisions to run_extension_log

## Safety and Governance

### Capability restrictions
- Only allow inserted steps that pass existing capability validation.
- Reuse validate_workflow_implementation_capability for extension inserted steps.

### Operation allowlist
- For accounting domains, allow only vetted operations by default:
  db_validate_ledger_payload, resolve_accounting_booking_company, db_accounting_ingest, db_accounting_delete
- Additional operations require explicit admin allowlisting.

### Determinism rules
- Stable sort order: attachment priority, rule precedence, rule key.
- Reject ambiguous merge/replace collisions unless conflict policy is explicit.

### Isolation
- Tenant-scoped attachments are isolated by tenant selector.
- Default deny for cross-tenant extension visibility.

## Example: Accounting Reconciliation Runtime Extension

User adds extension:
- target workflow_key: template::accounting_reconciliation_full
- hook: after_step
- target_step_key: validate_double_entry
- condition:
  - context.ledger_validation.valid equals true
  - context.reconciliation_plan.risk_level in [high, medium]
- inserted step:
  - invoke_solf_action -> workflow_domain_operation -> db_validate_ledger_payload (strict profile)
- inserted step:
  - require_user_confirmation (confirmation_key: finance_override)

Result:
- prime workflow stays unchanged
- extension only triggers when high/medium risk and validation is true
- approval gate is dynamically added

## Rollout Plan

### Step 1: Data schema and attachment APIs
Create extension tables and basic CRUD + attachment endpoints.

### Step 2: Resolver service
Implement deterministic condition evaluator and extension plan builder.

### Step 3: Executor hook integration
Inject pre/post/pause/failure extension steps into execution cycle.

### Step 4: Explainability and audit
Persist extension decision logs and expose run introspection endpoint.

### Step 5: Policy hardening
Add allowlists, limits, and stricter validator profiles for accounting workflows.

## Backward Compatibility
- Existing prime workflows continue to run unchanged when no attachments exist.
- Extension resolution is additive and optional.
- Feature can be guarded behind a config flag during rollout.

## Recommended Initial Constraints
To reduce risk in first release:
- allow only before_step and after_step hooks
- max 5 inserted steps per hook
- forbid replace semantics initially (insert-only)
- require published extension versions for production attachment
- enforce dry-run validation before publish

## Success Criteria
- Users can add conditional runtime processes without editing prime workflow definition.
- Prime accounting reconciliation workflow remains stable while extensions provide controlled customization.
- Every extension decision is auditable with reason and context snapshot.
- No decrease in existing pipeline determinism or safety guarantees.
