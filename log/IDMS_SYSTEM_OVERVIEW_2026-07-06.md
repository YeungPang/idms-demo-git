# IDMS System Overview

Generated: 2026-07-06

## What IDMS Is

IDMS (Intelligent Information and Document Management System) is a document-centric knowledge and operations platform that combines:

- Document ingestion and normalization
- Structured object/relationship storage
- Retrieval and query orchestration
- Rule and workflow execution
- Domain operations (accounting, HR, compliance)

In practical terms, IDMS turns files, notes, and business records into a queryable graph of entities, facts, relationships, and process state, then exposes those capabilities through APIs and workflow tools.

## What IDMS Is For

IDMS is designed to support organizations that need to:

- Ingest high-volume business documents and notes
- Extract and persist operational facts in a structured model
- Search and answer questions using both structured SQL and semantic retrieval
- Apply policy/rule logic consistently across ingestion and query flows
- Run workflow-driven business processes with versioned definitions and auditability
- Keep accounting and domain records aligned with source documents
- Maintain governance, traceability, and compliance evidence

## Core System Capabilities

## 1) Ingestion and Normalization

- Ingests documents by local path and file upload
- Supports markdown and note ingestion
- Supports reprocessing, repair, and backfill flows
- Tracks source references and provenance metadata
- Performs cleanup and maintenance for ingestion artifacts

## 2) Knowledge Modeling and Persistence

- Stores object instances and typed relationships
- Supports temporal attributes and temporal relationships
- Maintains semantic terms and classification metadata
- Supports alias groups and entity linkage patterns
- Preserves lineage from source document to extracted facts

## 3) Query and Retrieval

- Supports chat and direct query endpoints
- Uses SQL-first retrieval with semantic/vector fallback strategies
- Performs criteria-based search for filtered list retrieval
- Supports clarification and ambiguity resolution flows
- Returns source-aware grounding and trace data for explainability

## 4) Rule and Policy Layer (SOLF + Business Rules)

- SOLF parser/interpreter and function library
- Rule creation, activation, simulation, and review APIs
- Rule-to-workflow binding and policy checks
- Rule ingestion and classification tooling
- Runtime policy controls for query style and answer generation

## 5) Workflow and Pipeline Orchestration

- Workflow registry with versions, publication, rollback, and aliases
- Workflow extensions and attachments
- Pipeline run lifecycle: start, resume, cancel, inspect
- Document-to-run linking and rerun recommendations
- Generated script lifecycle with audit and cleanup support

## 6) Document Generation and Template Operations

- Template pipeline and template-reference management
- Template-driven document rendering and generation
- Sales/document process helpers and staged updates
- Export and save flows for generated outputs

## 7) Domain Operations

### Accounting

- Chart of accounts management
- Transaction and ledger-line upsert/validation
- Journal listing/export and VAT return summaries
- Domain definition and impact planning helpers

### HR

- Master data for departments and roles
- Employee profile and employment contract models
- Validation and reference integrity checks

## 8) Entity Matching and Data Quality

- Entity similarity checks and alias linking
- Candidate merge support for duplicate entities
- Fuzzy/semantic transaction matching workflows
- Retry and pending-queue handling for match pipelines

## 9) Governance, Monitoring, and Reliability

- Health and maintenance endpoints
- Audit/compliance trail support
- Dashboard and reliability/alerting integration points
- DB export/import and schema repair helpers
- Deduplication utilities for documents, objects, and relationships

## Typical End-to-End Use Cases

- Ingest invoices/contracts/notes, extract entities and amounts, and query them in natural language
- Run policy-driven workflows over extracted data and publish versioned process definitions
- Keep accounting ledgers and document evidence synchronized for audit-ready reporting
- Search across documents and structured entities to answer operational questions quickly

## System Characteristics

- API-first architecture with modular routers
- Hybrid retrieval combining deterministic SQL and semantic search
- Rule-governed runtime behavior
- Strong support for operational maintenance and schema evolution
- Built to be generic across domains rather than hardcoded to one tenant/person

## Summary

IDMS is a unified platform for document intelligence, structured knowledge management, and workflow-enabled operations. It is capable of moving from raw document ingestion to grounded query answering, policy-controlled decisions, and auditable domain execution in one integrated system.
