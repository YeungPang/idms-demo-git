# IDMS Ingestion Workflow, LLM Models, and Post-Ingestion Triggers

This document describes how ingestion runs today in `IDMS-Demo`, including:
- step-by-step ingestion workflow,
- all LLM/model touchpoints,
- post-ingestion processes and trigger paths (including booking).

## 1) Entry Points

Primary runtime path:
- `ingest.run_ingest(...)` in `ingest.py`

API entry points that call `run_ingest`:
- `POST /api/ingest/path`
- `POST /api/ingest/upload`
- `POST /api/ingest/reprocess`
- `POST /api/ingest/note` (note path persists a note document directly; it can still influence downstream extraction-style behavior through metadata conventions)

## 2) Model Configuration (OpenRouter)

From `idms_config.py`:
- `ROUTER_MODEL = OPENROUTER_SIMPLE_MODEL` (default: `openai/gpt-4o-mini`)
- `EXTRACT_MODEL = OPENROUTER_MODEL` (default: `google/gemini-2.5-flash`)
- `CHAT_MODEL = OPENROUTER_COMPLEX_MODEL` (default: `google/gemini-2.5-pro`)
- `QDRANT_EMBEDDING_MODEL = OPENROUTER_EMBEDDING_MODEL` (default: `openai/text-embedding-3-large`)

All core generation calls use `generate_content_with_openrouter_fallback(...)` through helpers in `ingest.py`.

## 3) Step-by-Step Ingestion Workflow (`run_ingest`)

1. Boot and runtime setup
- Load SOLF baseline script (`solf_script.txt`)
- Build interpreter (`build_solf_interpreter`) and load active runtime business-rule SOLF into interpreter
- Build OpenRouter client (`get_openrouter_client`)

2. Normalize user context and merge directives
- Normalize user description/tags/metadata
- Merge explicit `workflow_processes` input
- Parse note directives (`_derive_note_directives`) and merge metadata updates
- Merge explicit processing rule names from metadata/note directives

3. Resolve active processing rules by name
- Requested names are resolved against active business rules (`_resolve_active_processing_rules`)
- Processing directives are derived (`_derive_processing_directives`)

4. Resolve source and storage path
- Upload local source to GCS if needed (`upload_if_local`)
- Decide extraction source (raw source vs markdown-first)

5. Router LLM call (classification/routing)
- Build prompt (`build_router_prompt`)
- Call `generate_json(... call_name="router", model=ROUTER_MODEL)`
- Produces routed metadata (`document_type`, hints, language, etc.)

6. Optional localization LLM call
- If routed language is detected and user description exists:
- `localize_user_description(...)` via OpenRouter fallback

7. Extraction LLM call
- Build extraction prompt (`build_extraction_prompt`)
- Call `generate_json(... call_name="extract", model=EXTRACT_MODEL)`
- Produces extracted document/entities/relationships JSON

8. Apply rule/directive shaping pre-persistence
- Apply note directives to extracted payload (`_apply_note_directives_to_extracted`)
- Apply processing directives (`_apply_processing_directives`)
- Evaluate runtime rule triggers against document graph (`evaluate_runtime_rule_triggers`)
  - inspects document type + entities + attributes + relationships
  - computes matched rules and triggered workflow processes
  - merges triggered processes back into metadata

9. Domain action policy and enforcement
- `apply_domain_action_policy(...)`
- `enforce_domain_action_policy(...)`

10. Convert to SOLF objects
- `to_solf_objects(...)`
- Merge user metadata/tags into SOLF document metadata

11. Execute explicit note SOLF clauses (optional)
- `_invoke_note_solf_clauses(...)`

12. Markdown generation/reuse
- Reuse cached markdown or generate markdown (`load_or_generate_markdown`)
- For PDFs/images, markdown-first extraction is favored

13. Enrichment pipeline (non-LLM and optional NLP)
- spaCy enrichment (if available)
- shareholder/person/company enrichment helpers
- pruning unsupported identifiers

14. Persist to DB
- Create/ensure tables and schema
- Insert document record
- Persist entities/relationships via `persist_solf_objects(...)`
  - invoke SOLF validation and action clauses per entity
  - fallback to DB ingest/update/delete when SOLF does not return object row
  - upsert temporal relationships
  - upsert semantic terms
  - derive workflow runtime hook outputs in DB summary

15. Indexing (post-persistence)
- Qdrant indexing (if enabled and markdown available)
  - chunk markdown
  - embed chunks (`embed_text_chunks`, embedding model from config)
  - upsert vectors (`index_chunks_in_qdrant`)
- Discovery indexing path exists but is disabled in demo config (`ENABLE_DISCOVERY_INDEX = False`)

16. Final post-ingestion action execution (optional)
- Execute structured note action requests (`_execute_note_action_requests`) when enabled
- Return result payload including:
  - `workflow_trace`
  - `runtime_rule_trigger`
  - `note_solf_clause_results`
  - `note_action_execution`

## 4) LLM Calls and Models Used

A) Router classification
- Function: `generate_json(... call_name="router")`
- Model: `ROUTER_MODEL` (default `openai/gpt-4o-mini`)

B) Main extraction
- Function: `generate_json(... call_name="extract")`
- Model: `EXTRACT_MODEL` (default `google/gemini-2.5-flash`)

C) Optional user-description localization
- Function: `localize_user_description(...)`
- Uses OpenRouter fallback helper
- Model behavior follows call-level configuration in helper

D) Markdown generation (for some paths)
- Markdown extraction/generation can call OpenRouter fallback helpers and/or markdown module helpers

E) Embeddings for Qdrant
- Function: `embed_text_chunks(...)`
- Embedding model: `QDRANT_EMBEDDING_MODEL` (default `openai/text-embedding-3-large`)

## 5) Post-Ingestion Triggers and Booking Flow

### 5.1 Generic workflow process trigger path

Inside `persist_solf_objects(...)`:
1. Derive process list (`_derive_ingest_processes`)
2. For each process, invoke SOLF clauses:
- `business_rule_workflow_hint`
- `business_rule_workflow_execute`
- optional control-flow plans (`selection/sequence/iteration/backtracking`)
3. Record hooks in `db_summary.workflow_hooks`

### 5.2 Rule-triggered process injection (runtime)

Before persistence, `evaluate_runtime_rule_triggers(...)` inspects:
- document type,
- extracted entity classes,
- attribute keys,
- relationship types,
- active process context.

Matched rules can contribute workflow hints whose process names are merged into `workflow_processes`, so they are picked up by the workflow hook path above.

### 5.3 Booking-specific path

Booking today is a hybrid:

Runtime-driven parts:
- SOLF workflow hint/execute clauses for booking-related processes
- Runtime rule trigger engine can add workflow processes dynamically

Python-coded parts still present:
- In `_derive_ingest_processes`, if `doc_type in {invoice, bill, receipt}`, processes are auto-added:
  - `booking_process`, `booking_validation`, `account_selection`, `vat_classification`, `posting_finalize`
- Domain fallback ledger derivation in `domain_function.py` uses fixed defaults when needed
  - hardcoded debit/credit account defaults in `_derive_summary_ledger_lines_from_amounts`

Persistence of booking outcome:
- `domain_function.upsert_domain_object_metadata(...)` calls `domain_db.upsert_transaction_and_lines(...)`
- Writes to:
  - `transactions`
  - `ledger_lines`

## 6) Runtime Outputs to Inspect for Trigger Debugging

In the `run_ingest` result JSON, inspect:
- `workflow_trace`
- `runtime_rule_trigger`
- `db_summary.workflow_processes`
- `db_summary.workflow_hooks`
- `note_solf_clause_results`
- `note_action_execution`

## 7) Practical Notes

- Business rules (NL or SOLF script) are loaded at runtime and can influence trigger matching/workflow hints.
- Trigger matching is currently generic and data-driven over extracted graph features.
- Full removal of Python hardcoded booking defaults would require moving remaining fallback account mapping logic into runtime rule directives/SOLF clauses.

## 8) Concrete Walkthrough: Receipt Booking Trigger

This walkthrough shows one practical runtime flow for a receipt document.

### 8.1 Example ingest request (conceptual)

Use the ingestion API with metadata that allows runtime rule matching and process injection.

```json
{
  "path": "gs://your-bucket/docs/receipt-2026-06-17.pdf",
  "metadata": {
    "source": "ap_upload",
    "department": "finance",
    "workflow_processes": ["document_validation"],
    "processing_rule_names": ["receipt_booking_rule_v1"]
  },
  "tags": ["receipt", "booking-candidate"]
}
```

### 8.2 Example runtime rule shape (conceptual)

An active business rule can contribute workflow hints when the extracted graph matches expected signals.

```json
{
  "rule_name": "receipt_booking_rule_v1",
  "active": true,
  "match": {
    "document_types": ["receipt"],
    "entity_classes_any": ["supplier", "invoice", "payment"],
    "attribute_keys_any": ["gross_amount", "net_amount", "vat_amount", "currency"],
    "relationship_types_any": ["issued_by", "paid_to", "has_line_item"]
  },
  "workflow_hint": {
    "processes": [
      "booking_process",
      "booking_validation",
      "account_selection",
      "vat_classification",
      "posting_finalize"
    ]
  }
}
```

### 8.3 Step trace for this receipt scenario

1. Router classifies the document as receipt.
2. Extractor returns entities/attributes/relationships containing amount and vendor/payment signals.
3. Runtime trigger evaluator matches the active rule against the extracted graph.
4. Triggered processes are merged into metadata `workflow_processes`.
5. `persist_solf_objects(...)` runs workflow hint/execute clauses per process.
6. Domain booking persistence writes transaction and ledger lines when booking actions are produced.

### 8.4 Expected result snippets

Representative shape in ingestion response (values vary by rule/data):

```json
{
  "runtime_rule_trigger": {
    "matched_rules": ["receipt_booking_rule_v1"],
    "triggered_processes": [
      "booking_process",
      "booking_validation",
      "account_selection",
      "vat_classification",
      "posting_finalize"
    ]
  },
  "db_summary": {
    "workflow_processes": [
      "document_validation",
      "booking_process",
      "booking_validation",
      "account_selection",
      "vat_classification",
      "posting_finalize"
    ],
    "workflow_hooks": [
      {
        "process": "booking_process",
        "hint_clause": "business_rule_workflow_hint",
        "execute_clause": "business_rule_workflow_execute",
        "status": "executed"
      }
    ]
  }
}
```

### 8.5 What to verify for successful booking

- `runtime_rule_trigger.matched_rules` is non-empty.
- `db_summary.workflow_processes` contains booking stages.
- `db_summary.workflow_hooks` contains executed booking hook entries.
- Domain tables contain new/updated rows in `transactions` and `ledger_lines`.

## 9) Sparse Indexing Coverage During Ingestion

Short answer: yes for chunk text, partially for keywords as chunk content, and no direct sparse index for schema names.

### 9.1 What is indexed as sparse vectors

- During Qdrant indexing, each markdown/text chunk is encoded to sparse token vectors in `encode_chunks_sparse(...)`.
- `index_chunks_in_qdrant(...)` stores hybrid vectors per chunk:
  - dense vector under `dense`
  - sparse vector under `sparse` (when sparse support is available)

This means keywords that appear in chunk text are represented in sparse vectors.

### 9.2 What is NOT separately sparse-indexed

- `doc_keywords` and `entity_names` are stored as Qdrant payload fields, not as their own sparse vector stream.
- Attribute names and relationship names are not written into chunk sparse vectors as a dedicated schema sparse index.

### 9.3 Where attribute/relationship names are indexed instead

- In persistence, ingestion upserts semantic term rows via `object_db.upsert_semantic_terms(...)` with kinds:
  - `attribute`
  - `relationship`
- After ingestion, best-effort schema-term embedding refresh runs via `_build_attribute_embedding_index_if_available(...)`, which calls:
  - `upsert_dynamic_attribute_terms(...)`
  - `upsert_relationship_terms(...)`

These schema-term indexes are handled in the attribute embedding index path (vector schema-term matching), separate from chunk sparse vectors.
