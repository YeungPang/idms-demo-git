# IDMS SOLF LLM Generation Pack

## Answer

Yes. IDMS can provide a compact "generation pack" to an LLM so the model returns:

1. executable SOLF clauses, and
2. required class definitions

in a format that IDMS can validate and persist.

This pack is aligned with the existing behavior in:

- business_rules.parse_rule_text_to_structured
- business_rules.review_solf_script_rule
- business_rules.create_business_rule_from_solf_script
- solf_function.workflow_domain_operation

---

## 1) Minimal SOLF Syntax the LLM Must Follow

### 1.1 Clause form

- Predicate style:
  - clause_name(arg1, arg2)
- Clause body wrapper:
  - clause_name(args) ⦃ ... ⦄
- Return operator:
  - ↲(value)

Example:

reconciliation_prepare_candidates(_ctx) ⦃
    ↲(workflow_domain_operation({
        operation: build_reconciliation_candidates,
        operation_payload: _ctx
    }))
⦄

### 1.2 Assignment and boolean composition

- Assignment: x ≔ expr
- And: a ⋀ b
- Or: a ⋁ b

Example:

pause_if_ambiguous(_ctx) ⦃
    (_needs_pause ≔ _ctx[needs_user_confirmation]) ⋀
    (_needs_pause = true) ⋀
    ↲({ paused: true, reason: user_interaction })
⦄

### 1.3 Class definition block format

Use class_name ≔ { ... } blocks.

Example:

invoice_record ≔ {
    invoice_id: string,
    invoice_number: string,
    amount_total: number,
    currency: string,
    due_date: string,
    party_name: string
}

payment_record ≔ {
    payment_id: string,
    amount: number,
    currency: string,
    payment_date: string,
    reference: string,
    party_name: string
}

---

## 2) LLM Output Contract (Strict)

Require the LLM to return JSON only with this schema:

{
  "rule_name": "string",
  "class_definitions": [
    {
      "class_name": "string",
      "parent_class_name": "string or null",
      "attributes": ["snake_case_string"]
    }
  ],
  "solf_script": "string"
}

Constraints:

- class_name and attributes must be snake_case.
- solf_script must include only class blocks and SOLF clauses.
- All workflow_domain_operation operation names must be from the allowed runtime list.

---

## 3) Executable Clause Templates

### 3.1 Domain operation wrapper clause

Template:

{{clause_name}}(_ctx) ⦃
    ↲(workflow_domain_operation({
        operation: {{operation_name}},
        operation_payload: _ctx
    }))
⦄

### 3.2 Multi-step composed clause

Template:

{{entry_clause}}(_ctx) ⦃
    ↲(workflow_compose_operations({
        context: _ctx,
        stop_on_error: true,
        operations: [
            {
                action: workflow_domain_operation,
                payload: { operation: {{op1}}, operation_payload: _ctx },
                save_as: {{save_key_1}}
            },
            {
                action: workflow_domain_operation,
                payload: { operation: {{op2}}, operation_payload: _ctx },
                save_as: {{save_key_2}}
            }
        ]
    }))
⦄

### 3.3 Pause template for ambiguity

Template:

{{pause_clause}}(_ctx) ⦃
    (_needs_pause ≔ _ctx[needs_user_confirmation]) ⋀
    (_needs_pause = true) ⋀
    ↲({
        paused: true,
        reason: user_interaction,
        prompt: "Multiple valid matches found. Please confirm the preferred allocation."
    })
⦄

---

## 4) Required Runtime Operation Catalog

When generating executable clauses, the LLM should use only operation names that exist in runtime allowlists.

Current examples already allowed in workflow_domain_operation:

- db_validate_ledger_payload
- resolve_accounting_booking_company
- db_accounting_ingest
- db_accounting_delete
- db_hr_validate_payload
- db_hr_ingest
- db_hr_delete

If new names are needed (for example allocate_split_payments), they must be implemented in domain_function and added to the allowlist in solf_function.workflow_domain_operation before execution.

---

## 5) Prompt You Can Send to the LLM

Use this instruction envelope:

You are generating SOLF artifacts for IDMS.
Return JSON only with keys: rule_name, class_definitions, solf_script.

Rules:
1. Use snake_case for class and attribute names.
2. Include class blocks in "class_name ≔ { ... }" form.
3. Generate executable clauses using only these wrappers:
   - workflow_domain_operation
   - workflow_compose_operations
4. Use only allowed operation names:
   - db_validate_ledger_payload
   - resolve_accounting_booking_company
   - db_accounting_ingest
   - db_accounting_delete
   - db_hr_validate_payload
   - db_hr_ingest
   - db_hr_delete
5. Include at least one entry clause that returns via ↲(...).
6. Do not output commentary or markdown.

Business intent:
{{user_intent_here}}

---

## 6) IDMS Intake and Validation Flow

Recommended flow:

1. Ask LLM for JSON using the contract in section 2.
2. Take returned solf_script.
3. Run business_rules.review_solf_script_rule for syntax and extracted class/clauses preview.
4. If clean, persist with business_rules.create_business_rule_from_solf_script.

This provides a practical closed loop from prompt to executable workflow artifacts.

---

## 7) API Endpoints for Automation

1. Fetch pack:

- GET /api/business-rules/solf-generation-pack
- Optional: `?include_markdown=false`

2. Validate and optionally persist LLM output:

- POST /api/business-rules/solf-generation-pack/ingest

3. One-call generate + validate + optional persist from plain intent:

- POST /api/business-rules/solf-generation-pack/generate-and-ingest

Request body:

```json
{
    "intent": "Reconcile incoming bank payments to invoices with split allocation and pause on ambiguity.",
    "persist": true,
    "created_by": "api:user",
    "is_active": true,
    "default_clause_type": "resolve_policy",
    "temperature": 0.0
}
```

Request body:

```json
{
    "llm_output": {
        "rule_name": "bank_reconciliation_rule",
        "class_definitions": [
            {
                "class_name": "invoice_record",
                "parent_class_name": null,
                "attributes": ["invoice_id", "amount_total", "currency"]
            }
        ],
        "solf_script": "invoice_record ≔ { invoice_id: string }\n\nreconciliation_prepare(_ctx) ⦃ ↲(_ctx) ⦄"
    },
    "persist": true,
    "created_by": "api:user",
    "is_active": true,
    "default_clause_type": "resolve_policy"
}
```

Behavior:

- always performs review validation,
- returns syntax/class-alignment diagnostics,
- persists when `persist=true` and script is valid.

---

## 8) Regression Script

Run the local regression script to call all three endpoints and save request/response JSON artifacts:

```powershell
c:/Project/WebTech/python/.venv/Scripts/python.exe IDMS-Demo/log/run_solf_generation_api_regression.py --base-url http://127.0.0.1:8000
```

Notes:

- default mode uses `persist=false` to avoid accidental writes,
- artifacts are written under `IDMS-Demo/log/solf_generation_api_regression_<timestamp>/`,
- pass `--persist` only when you want the run to create/update stored rules.
