# IDMS Workflow Pipeline: NL Rule -> Patterned Plan -> SOLF Clauses -> Python Functions

## 1. Goal

Enable a user to provide a natural-language workflow request such as:

"Create a workflow pipeline for reconciliation of a bank statement to the bookings in ledger lines in the database. Payments can be split up and multiple payments could be made for an invoice that appears as one booking."

and have IDMS:

1. Resolve the intent iteratively with an LLM planner.
2. Convert the result to structured rule/workflow patterns.
3. Map those patterns to internal SOLF clauses.
4. Trigger Python runtime functions (from `solf_function.py` and/or `domain_function.py`) through SOLF.
5. Persist, version, publish, and execute as a workflow pipeline run.

---

## 2. What Already Exists In IDMS-Demo

### A) Iterative workflow design from NL request

- API: `POST /api/interaction/workflow/design`
- Router: `idms_api_server/routers/interaction.py` -> `workflow_design(...)`
- Engine method: `interaction.py` -> `design_workflow_interaction(...)`
- Iterative planner method: `interaction.py` -> `_workflow_design_iteration(...)`

Current behavior:

- Uses iterative LLM planning (up to `max_iterations`) with strict JSON output.
- Returns:
  - `workflow_steps`
  - `gaps`
  - `solf_candidates`
  - `next_questions`
  - `next_action`
- Grounds planner with available SOLF symbols from `solf_script.txt` via `_get_solf_symbols_snapshot(...)`.

### B) Natural-language business rule parsing and SOLF compilation

- API: `POST /api/business-rules` and `POST /api/business-rules/simulate-draft`
- Parser: `business_rules.py` -> `parse_rule_text_to_structured(...)`
- Normalizer: `business_rules.py` -> `_normalize_structured_rule(...)`
- Compiler: `business_rules.py` -> `compile_structured_rule_to_solf(...)`

Current behavior:

- Parses NL into structured JSON (`mappings`, `workflow_hints`, `fact_assertions`, `multi_hop_dependencies`, `quantified_conditions`, `post_extraction_directives`, etc.).
- Compiles structured rule into SOLF clauses (`business_rule_attribute_alias`, `business_rule_workflow_hint`, `business_rule_workflow_execute`, etc.).
- Upserts SQL-side SOLF clauses and semantic terms when persisted.

### C) Rule -> Workflow Registry -> Versioned workflow steps

- One-click endpoint: `POST /api/business-rules/create-with-registry`
- Sync function: `business_rules.py` -> `sync_business_rule_workflow_registry(...)`
- Derivation helpers:
  - `_derive_workflow_entries_from_structured_rule(...)`
  - `_build_steps_for_rule_registry(...)`
- Publish endpoint: `POST /api/business-rules/workflow-registry/{workflow_id}/publish`

Current behavior:

- Creates `solf_workflow_registry` entries.
- Creates `solf_workflow_versions` entries.
- Creates `solf_workflow_steps` entries.
- Links business rule to workflow/version.

### D) Pipeline runtime with pause/resume

- API router: `idms_api_server/routers/pipeline_runs.py`
  - `POST /api/pipeline-runs`
  - `POST /api/pipeline-runs/{run_id}/resume`
  - `POST /api/pipeline-runs/{run_id}/cancel`
  - `GET /api/pipeline-runs/{run_id}`
- Executor: `workflow_pipeline_executor.py`
- Built-in python bindings: `workflow_pipeline_builtin_steps.py`

Step kinds supported by runtime:

- `clause`: invokes SOLF clause with current context.
- `python_binding`: invokes Python function `module.function(context, config)`.
- `class_*` step kinds are currently treated as no-op placeholders.

### E) SOLF -> Python function bridge

- Interpreter bridge: `solf_interpreter.py` -> `_try_python_function(...)`
- Extension module search order:
  1. `solf_function`
  2. `domain_function`

This means any unresolved SOLF predicate can call a same-named Python function in those modules.

### F) Existing workflow-capable Python actions in `solf_function.py`

- `workflow_llm_call(...)`
- `workflow_generate_schema_and_solf(...)`
- `workflow_domain_operation(...)`
- `workflow_compose_operations(...)`

`workflow_domain_operation(...)` already supports accounting-related operations such as:

- `db_validate_ledger_payload`
- `resolve_accounting_booking_company`
- `db_accounting_ingest`
- `db_accounting_delete`

---

## 3. Critical Gaps To Close For Full End-To-End Execution

## Gap 1: Pipeline runtime does not inject SOLF interpreter

Current code in `idms_api_server/routers/pipeline_runs.py` builds executor as:

`WorkflowPipelineExecutor(db_connection_fn=object_db.get_connection)`

without `solf_interpreter`.

Impact:

- Any registry step with `step_kind='clause'` fails at runtime because executor cannot call SOLF.

Required fix:

- Build and inject the same SOLF interpreter used by interaction tools (base script + active business rules), then pass it to `WorkflowPipelineExecutor(...)`.

## Gap 2: Registry sync emits mostly clause/class steps, but class steps are no-op and reconciliation operations are not mapped yet

Current sync behavior in `business_rules.py`:

- `_build_steps_for_rule_registry(...)` emits:
  - `class_generate` steps (currently no-op in runtime)
  - `clause` steps (depends on Gap 1 fix)

For your reconciliation use case, we still need explicit operation mapping to Python actions for:

- bank statement line extraction
- split payment allocation
- invoice-to-payment many-to-one and one-to-many matching
- posting/reconciliation write-back

Required fix:

- Extend step synthesis to include `python_binding` steps that call built-ins (for example `invoke_solf_action`) and then `solf_function` operations.

---

## 4. Recommended End-To-End Process (Target State)

## Stage 1: User intent decomposition

1. User submits NL workflow request to `POST /api/interaction/workflow/design`.
2. Iterate planner until `next_action in {finalize, map_to_solf}` and workflow steps are stable.
3. Capture `workflow_steps`, `gaps`, `solf_candidates`.

## Stage 2: Draft rule simulation and artifact review

1. Send enriched rule text to `POST /api/business-rules/simulate-draft`.
2. Validate:
   - structured rule quality
   - generated SOLF clauses
   - scope extraction
   - workflow hint extraction
3. If needed, revise NL request and repeat.

## Stage 3: Persist and register workflow

1. Persist rule + compile SOLF + sync registry in one call:
   - `POST /api/business-rules/create-with-registry`
2. Result gives:
   - `rule_id`
   - linked `workflow_id`
   - `workflow_version_id`
   - created steps

## Stage 4: Publish version

1. Publish target version:
   - `POST /api/business-rules/workflow-registry/{workflow_id}/publish`
2. Version status becomes `published` and active.

## Stage 5: Execute pipeline run

1. Start run:
   - `POST /api/pipeline-runs`
2. Runtime executes steps sequentially with context updates.
3. If missing data/user action is needed, run pauses with reason and prompt.
4. Resume run with provided data/doc refs:
   - `POST /api/pipeline-runs/{run_id}/resume`

## Stage 6: SOLF clause dispatch and Python function triggering

1. Clause step calls SOLF predicate:
   - `solf_interpreter._invoke_clause(clause_name, [context])`
2. If predicate includes callable not defined as clause body symbol, interpreter tries Python extension lookup.
3. Matching Python function in `solf_function.py` or `domain_function.py` is executed.

This is where operation patterns from LLM output become concrete runtime behavior.

---

## 5. Concrete Pattern Mapping For Bank Statement Reconciliation

For your specific example, define these operation patterns (suggested):

1. `collect_bank_statement_lines`
2. `normalize_ledger_bookings`
3. `candidate_match_statement_to_invoice`
4. `allocate_split_payments`
5. `aggregate_multi_payment_invoice`
6. `decide_reconciliation_status`
7. `persist_reconciliation_result`
8. `emit_audit_metrics`

Recommended implementation mapping:

- Add these functions in `domain_function.py` (or `solf_function.py` wrappers).
- Expose them through `workflow_domain_operation(...)` allowed operations or direct SOLF-callable names.
- Generate registry `python_binding` steps (via `workflow_pipeline_builtin_steps.invoke_solf_action`) that invoke those actions.

Example step config pattern (conceptual):

```json
{
  "step_kind": "python_binding",
  "python_module": "workflow_pipeline_builtin_steps",
  "python_function": "invoke_solf_action",
  "config": {
    "action": "workflow_domain_operation",
    "payload": {
      "operation": "allocate_split_payments"
    },
    "save_as": "split_allocation"
  }
}
```

---

## 6. Suggested SOLF Layer For Reconciliation

Use SOLF clauses as orchestration policy and decision points, while Python performs heavy computation.

Example conceptual SOLF clauses:

- `bank_recon_plan(_ctx)` -> returns ordered operation strategy
- `bank_recon_match_policy(_ctx)` -> controls tolerance/priority behavior
- `bank_recon_execute(_ctx)` -> calls `workflow_compose_operations(...)`
- `bank_recon_escalation_policy(_ctx)` -> sends unresolved cases to manual review

This keeps:

- policy/versioning in SOLF
- computational complexity in Python
- workflow runtime in registry/pipeline engine

---

## 7. Minimal Code Changes Needed

1. `idms_api_server/routers/pipeline_runs.py`
- Inject SOLF interpreter into `WorkflowPipelineExecutor`.

2. `business_rules.py`
- Enhance `_build_steps_for_rule_registry(...)` to emit executable `python_binding` steps for domain operations (not only clause/class placeholders).

3. `solf_function.py` and/or `domain_function.py`
- Implement reconciliation operation functions:
  - split allocation
  - many-to-one / one-to-many payment-invoice matching
  - reconciliation write-back and metrics

4. Optional hardening
- Add confidence thresholds and fallback paths in step configs.
- Add a manual-review pause step when confidence is below threshold.

---

## 8. Validation Checklist

1. `POST /api/interaction/workflow/design` returns stable `workflow_steps` and `solf_candidates`.
2. `POST /api/business-rules/simulate-draft` returns expected SOLF and scope.
3. `POST /api/business-rules/create-with-registry` creates links and steps.
4. Published version is active.
5. `POST /api/pipeline-runs` executes clause + python_binding steps successfully.
6. Split-payment and multi-payment invoice reconciliation scenarios are covered by tests.
7. Pipeline run outputs include audit logs and deterministic reconciliation status.

---

## 9. Process Diagram

```mermaid
flowchart TD
  A[User NL workflow request] --> B[/api/interaction/workflow/design]
  B --> C[Iterative LLM plan + SOLF candidates]
  C --> D[/api/business-rules/simulate-draft]
  D --> E[Structured rule + compiled SOLF preview]
  E --> F[/api/business-rules/create-with-registry]
  F --> G[Rule + clauses + workflow registry/version/steps]
  G --> H[/api/business-rules/workflow-registry/{id}/publish]
  H --> I[/api/pipeline-runs start]
  I --> J[WorkflowPipelineExecutor]
  J --> K{Step kind}
  K -->|clause| L[SOLF _invoke_clause]
  K -->|python_binding| M[Built-in step invoke_solf_action]
  L --> N[Interpreter python extension lookup]
  N --> O[solf_function.py / domain_function.py]
  M --> O
  O --> P[Reconciliation outputs + audit]
  P --> Q[/api/pipeline-runs/{id}/resume if paused]
```

---

## 10. Bottom Line

Yes, IDMS already has almost all foundational pieces for this feature:

- iterative LLM workflow planning,
- NL rule parsing,
- SOLF compilation,
- workflow registry/versioning,
- pipeline runtime,
- SOLF-to-Python function bridge.

To make your reconciliation pipeline fully operational, prioritize:

1. passing a SOLF interpreter into pipeline run executor,
2. generating executable python-binding steps for reconciliation operations,
3. implementing reconciliation-specific split/multi-payment matching functions.

---

## 11. Related Design Doc

For a concrete reusable function catalog and SOLF clause templates for split allocation and many-to-one invoice-payment matching, see:

- `IDMS-Demo/log/GENERIC_REUSABLE_FUNCTIONS_AND_SOLF_RECONCILIATION_DESIGN.md`
- `IDMS-Demo/log/IDMS_SOLF_LLM_GENERATION_PACK.md`
- `IDMS-Demo/log/SEMANTIC_FUZZY_TRANSACTION_MATCHING_DESIGN.md`

---

## 12. Ops Regression Commands (Tx-Match Queue)

Use these commands when validating the tx-match queue lifecycle hardening:

Queue maintenance API regression (token-aware environments):

```powershell
c:/Project/WebTech/python/.venv/Scripts/python.exe IDMS-Demo/log/run_tx_match_pending_queue_regression.py --base-url http://127.0.0.1:8000 --admin-token <token-if-required>
```

Lifecycle integration (queued -> retried -> indexed) with cleanup:

```powershell
c:/Project/WebTech/python/.venv/Scripts/python.exe IDMS-Demo/log/run_tx_match_queue_lifecycle_integration.py --cleanup
```

---

## 13. Step-Driven Resource Alias Registration

IDMS now supports first-pass workflow resource alias registration derived from workflow step needs during workflow registry sync.

Current behavior:

1. During business rule -> workflow registry sync, IDMS inspects generated workflow steps.
2. If step signals indicate matching capabilities, IDMS registers workflow-scoped aliases for:
  - matching query endpoint
  - matching candidates endpoint
  - matching purpose
  - matching collection
  - matching pending queue when retry/pending/index signals are present
3. Explicit `resource_aliases` can also be supplied in workflow metadata, graph spec, or step config.

Resource aliases are stored against workflow version in canonical form and can be listed through workflow registry lookup responses.

Runtime support now includes:

1. workflow alias listing by workflow id or workflow key
2. explicit alias resolution endpoint
3. matching endpoints can resolve default purpose, collection, and workflow scope from workflow alias registrations
4. workflow-facing shortcut routes:
  - `POST /api/matching/workflow/{workflow_key}/query`
  - `POST /api/matching/workflow/{workflow_key}/candidates`
