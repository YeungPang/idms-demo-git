# IDMS Feature Inventory

Generated: 2026-07-06

Scope: code-surface inventory for `IDMS-Demo`, based on router registration, top-level modules, schema helpers, and regression/docs already present in the package.

This file summarizes implemented features, not runtime health.

## API Surfaces

- Health and maintenance endpoints from the system router:
  - `/api/health`
  - `/api/maintenance/db-export/tables`
  - `/api/maintenance/db-export/csv`
  - `/api/maintenance/db-import/csv`
  - `/api/maintenance/schema-embeddings/rebuild`
  - `/api/maintenance/tx-match/pending`
  - `/api/maintenance/tx-match/pending/{pending_id}`
  - `/api/maintenance/tx-match/pending/retry`
- Ingestion endpoints:
  - system rules and markdown policy inspection
  - ingest by path and upload
  - reprocess, repair, backfill, markdown cleanup
  - note ingestion
- Interaction endpoints:
  - chat and generic query execution
  - clarification response and retrieval
  - entity similarity, alias linking, entity merge
  - action execution
  - SOLF predicate execution
  - workflow design and resolution helpers
- Notes endpoints:
  - search
  - list by date
  - list by tag
- Business rules and workflow endpoints:
  - business rule CRUD
  - SOLF review, creation, and update flows
  - rule activation and simulation
  - draft review helpers
  - SOLF generation pack ingest and generation
  - process taxonomy list and suggestion
  - workflow registry CRUD, activation, publish, rollback
  - workflow resource aliases and alias resolution
  - workflow extensions, versions, attachments, and preview resolution
  - workflow capability validation and issue output helpers
  - template seeding and description export
  - workflow registry sync/create-with-registry helpers
- Document endpoints:
  - delete, export, and export-save
  - template-reference ingest, list, and generate
  - sales pipeline run helpers and stage updates
  - template rendering
- Schema proposal endpoints:
  - list and summary
  - proposal status updates
  - SOLF patch draft retrieval
  - promotion batch CRUD, export, and audit linking
- Accounting endpoints:
  - journal listing and export
  - VAT return summarization
- Rule ingestion endpoints:
  - classify, ingest, batch ingest
  - supported rule type listing and health checks
- Domain definition endpoints:
  - account definition creation from natural language
  - impact planning
  - create, update, delete, get, and list
  - audit-log retrieval
- Pipeline run endpoints:
  - start, start-by-key, resume, cancel
  - run inspection and listing
  - run-document links and document-driven rerun helpers
  - script generation, policy checks, prompt package creation
  - generated script audit and cleanup helpers
- Matching and natural-language endpoints:
  - matching query and candidate search
  - workflow-aware matching variants
  - natural language catalog and execution

## Core Domain Features

- Document ingestion and normalization:
  - markdown source ingestion
  - upload/path-based ingest
  - reprocessing and repair flows
  - table provenance backfill
  - source path backfill
  - orphan cleanup and markdown status tracking
- Document generation and templating:
  - document template pipeline
  - template references
  - generated document and template rendering support
  - example template generation utilities
- Object and relationship storage:
  - object instances
  - object relationships
  - alias groups
  - temporal attributes
  - temporal relationships
  - semantic terms
  - children/parent traversal helpers
- Search and matching:
  - hybrid search
  - semantic / fuzzy transaction matching
  - entity similarity checks
  - transaction match indexing and retry workers
- Accounting domain:
  - chart of accounts seeding and maintenance
  - double-entry journal validation
  - transaction and ledger-line upsert
  - journal listing and VAT return summaries
  - Swiss VAT code routing
  - accounting schema/domain definition CRUD
- HR domain:
  - HR department master data
  - HR role master data
  - HR employee profiles
  - HR employment contracts
  - HR payload validation and reference checks
- Domain definition management:
  - account, department, and role definitions
  - validation, CRUD, list, and audit logging
  - natural-language account definition creation and impact planning
- SOLF and business-rules layer:
  - SOLF parser and interpreter
  - SOLF function library
  - SOLF clause storage and retrieval
  - business rule creation and workflow binding
  - workflow registry and versioning
  - workflow extension packs, versions, attachments, and aliases
  - process taxonomy catalog
  - generation packs for workflow scripts
- Pipeline orchestration:
  - pipeline runs
  - pipeline step tracking
  - document-to-run links
  - rerun recommendations for changed documents
  - generated script lifecycle management
- Monitoring, reliability, and compliance:
  - alert/reliability management
  - dashboard/metrics endpoints
  - audit/compliance trail management
  - notifier/subscription delivery support
- Maintenance and operations:
  - schema migration and repair helpers
  - DB export/import helpers
  - deduplication utilities for documents, objects, and relationships
  - credentials path resolution
  - scheduled jobs support

## Data and Schema Capabilities

- `domain_db.py` covers:
  - chart of accounts
  - transactions
  - ledger lines
  - HR employee records
  - HR employment contracts
  - HR master data tables
  - domain definition audit log
  - SOLF attribute proposals
  - SOLF schema promotion batches
- `object_db.py` covers:
  - object class / instance storage
  - relationships and temporal history
  - semantic terms
  - business rules
  - workflow registry and versions
  - extension packs and attachments
  - pipeline runs and document links
- `sql_db.py` covers:
  - base node/edge storage
  - document metadata lookup
  - legacy schema repair and bootstrap helpers

## Notes

- The package is broader than the public API alone; many features are exposed through helper modules, CLI-style scripts, and background workers.
- Some functionality is implemented as support code for regression runs, schema maintenance, or workflow generation rather than as user-facing endpoints.