# IDMS-Proto Codebase Structure Reference

**Generated:** 2026-06-16

This document captures key signatures, DB table structures, and API endpoints for IDMS-Proto pattern library, SOLF clauses, schema classes, and data storage mechanisms.

---

## 1. Pattern Library: Creation & Storage

### 1.1 Pattern Creation Signature
**File:** [pattern_library.py](pattern_library.py#L511)

```python
def add_pattern(
    self,
    pattern_text: str,
    semantic_concept: str,
    mapped_attributes: dict[str, Any],
    computation_rule: Optional[str] = None,
    entity_class: Optional[str] = None,
    source_type: str = "learned",  # "seeded", "learned", or "manual"
    confidence: float = 0.80,
    pattern_language: str = "en",
    metadata: Optional[dict[str, Any]] = None,
    created_by: Optional[str] = None,
) -> Optional[int]:
```

**Returns:** `pattern_id` (BIGINT) if successful, None on error

**Behavior:**
- Upserts into `semantic_patterns` table using pattern_text, pattern_language, and entity_class as uniqueness key
- Normalizes text to lowercase before storage
- Keeps higher confidence value between existing and new
- Stores metadata and computation rules as JSONB
- Returns generated `pattern_id`

---

### 1.2 Semantic Patterns Table
**File:** [object_db.py#L370](object_db.py#L370)

```sql
CREATE TABLE IF NOT EXISTS semantic_patterns (
    pattern_id           BIGSERIAL PRIMARY KEY,
    pattern_text         VARCHAR(512) NOT NULL,
    semantic_concept     VARCHAR(128) NOT NULL,
    mapped_attributes    JSONB NOT NULL DEFAULT '{}'::jsonb,
    computation_rule     TEXT,
    entity_class         VARCHAR(128),
    source_type          VARCHAR(32) NOT NULL DEFAULT 'seeded' 
        CHECK (source_type IN ('seeded','learned','manual')),
    confidence           NUMERIC(5,4) NOT NULL DEFAULT 1.0 
        CHECK (confidence >= 0 AND confidence <= 1),
    pattern_language     VARCHAR(32) NOT NULL DEFAULT 'en',
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by           VARCHAR(128),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pattern_text, entity_class, pattern_language)
);
```

**Indexes:**
- `idx_semantic_patterns_text` - pattern_text
- `idx_semantic_patterns_text_lower` - LOWER(pattern_text)
- `idx_semantic_patterns_concept` - semantic_concept
- `idx_semantic_patterns_entity_class` - entity_class
- `idx_semantic_patterns_source_type` - source_type
- `idx_semantic_patterns_confidence` - confidence
- `idx_semantic_patterns_language` - pattern_language
- `idx_semantic_patterns_metadata_gin` - GIN(metadata)
- `idx_semantic_patterns_mapped_attr_gin` - GIN(mapped_attributes)

---

### 1.3 Pattern Synonyms Table
**File:** [object_db.py#L398](object_db.py#L398)

```sql
CREATE TABLE IF NOT EXISTS pattern_synonyms (
    synonym_id           BIGSERIAL PRIMARY KEY,
    pattern_id           BIGINT NOT NULL REFERENCES semantic_patterns(pattern_id) ON DELETE CASCADE,
    synonym_text         VARCHAR(512) NOT NULL,
    language             VARCHAR(32) NOT NULL DEFAULT 'en',
    semantic_distance    NUMERIC(5,4) NOT NULL DEFAULT 0.0 
        CHECK (semantic_distance >= 0 AND semantic_distance <= 1),
    match_type           VARCHAR(32) NOT NULL DEFAULT 'exact' 
        CHECK (match_type IN ('exact','template','fuzzy','semantic')),
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pattern_id, synonym_text, language)
);
```

**Method for adding synonyms:**
```python
def add_synonym(
    self,
    pattern_id: int,
    synonym_text: str,
    language: str = "en",
    semantic_distance: float = 0.0,
    match_type: str = "exact",  # "exact", "template", "fuzzy", "semantic"
) -> bool:
```

---

## 2. SOLF Clauses: Creation & Storage

### 2.1 SOLF Clause Upsert Signature
**File:** [pattern_library.py#L49](pattern_library.py#L49)

```python
def upsert_solf_clause(
    self,
    clause_name: str,
    clause_type: str,
    clause_body: str,
    entity_class: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    is_active: bool = True,
    created_by: Optional[str] = None,
) -> Optional[int]:
```

**Parameters:**
- `clause_name` - Unique identifier for the clause
- `clause_type` - One of: `"query_pattern"`, `"resolve_policy"`, `"ingest_rule"`, `"computation_rule"`
- `clause_body` - SOLF script content (plain text)
- `entity_class` - Optional scope (e.g., "invoice", "employee", "company")
- `metadata` - Optional JSONB metadata (e.g., `{"semantic_concept": "..."}`)
- `is_active` - Boolean activation flag

**Returns:** `clause_id` (BIGINT) if successful, None on error

**Behavior:**
- Checks existence by (clause_name, entity_class)
- Updates if exists, inserts if new
- Tracks created_at and modified_at timestamps

---

### 2.2 SOLF Clauses Table
**File:** [object_db.py#L437](object_db.py#L437)

```sql
CREATE TABLE IF NOT EXISTS solf_clauses (
    clause_id            BIGSERIAL PRIMARY KEY,
    clause_name          VARCHAR(256) NOT NULL,
    clause_type          VARCHAR(32) NOT NULL 
        CHECK (clause_type IN ('query_pattern','resolve_policy','ingest_rule','computation_rule')),
    entity_class         VARCHAR(128),
    clause_body          TEXT NOT NULL,
    referenced_patterns  BIGINT[] DEFAULT '{}',
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active            BOOLEAN NOT NULL DEFAULT true,
    created_by           VARCHAR(128),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (clause_name, entity_class)
);
```

**Indexes:**
- `idx_solf_clauses_name` - clause_name
- `idx_solf_clauses_type` - clause_type
- `idx_solf_clauses_entity_class` - entity_class
- `idx_solf_clauses_is_active` - is_active
- `idx_solf_clauses_created_at` - created_at
- `idx_solf_clauses_metadata_gin` - GIN(metadata)

---

### 2.3 Retrieving Active Computation Rules
**File:** [pattern_library.py#L135](pattern_library.py#L135)

```python
def get_active_computation_rule(
    self,
    semantic_concept: str,
    entity_class: Optional[str] = None,
) -> Optional[str]:
```

**Returns:** clause_body (plain SOLF text) or None

**Logic:**
- Matches by `clause_name` or `metadata->semantic_concept`
- Filters: `clause_type = 'computation_rule'` AND `is_active = TRUE`
- Prioritizes exact entity_class match over NULL
- Orders by modified_at DESC (most recent first)

---

## 3. Schema Classes: Definition & Storage

### 3.1 Schema Class Definition (SOLF Parser)
**File:** [ingest.py#L187](ingest.py#L187)

```python
@dataclass
class SolfClassDef:
    class_name: str
    parent_class_name: str | None
    allowed_attributes: set[str]
    callable_fields: set[str]
```

**Parsing:**
- Extracted from SOLF script via [parse_solf_classes](ingest.py) function
- Class definition syntax: `ClassName ≔ { attr1: Type, attr2: Type, ... }`
- Callable fields marked with function notation (e.g., `hr_tenure_days()`)

---

### 3.2 Ensure Classes in DB (Upsert)
**File:** [ingest.py#L1105](ingest.py#L1105)

```python
def ensure_solf_classes_in_db(
    connection: Any, 
    class_defs: dict[str, SolfClassDef]
) -> None:
```

**SQL Used:**
```sql
INSERT INTO object_class (class_name, parent_class_id, metadata)
VALUES (
    %s,
    (SELECT class_id FROM object_class WHERE class_name = %s LIMIT 1),
    %s::jsonb
)
ON CONFLICT (class_name)
DO UPDATE SET
    parent_class_id = EXCLUDED.parent_class_id,
    metadata = COALESCE(object_class.metadata, '{}'::jsonb) || EXCLUDED.metadata
```

**Metadata stored:**
```json
{
    "allowed_attributes": ["attr1", "attr2", ...],
    "callable_fields": ["callable_attr1", ...],
    "source": "solf_script"
}
```

---

### 3.3 Object Class Table
**File:** [object_db.py#L161](object_db.py#L161)

```sql
CREATE TABLE IF NOT EXISTS object_class (
    class_id            BIGSERIAL PRIMARY KEY,
    class_name          VARCHAR(256) NOT NULL UNIQUE,
    parent_class_id     BIGINT REFERENCES object_class(class_id) ON DELETE SET NULL,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    entry_date          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Indexes:**
- `idx_object_class_name` - class_name
- `idx_object_class_parent` - parent_class_id
- `idx_object_class_metadata_gin` - GIN(metadata)

**Hierarchy:** Classes can have parent_class_id (inheritance)

---

## 4. Entity Storage Tables

### 4.1 Object Instance Table (Entities/Objects)
**File:** [object_db.py#L176](object_db.py#L176)

```sql
CREATE TABLE IF NOT EXISTS object_instance (
    object_id           BIGSERIAL PRIMARY KEY,
    object_name         VARCHAR(256) NOT NULL,
    canonical_full_name VARCHAR(256),
    class_name          VARCHAR(256) NOT NULL REFERENCES object_class(class_name) 
        ON DELETE RESTRICT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              VARCHAR(32) NOT NULL DEFAULT 'active',
    valid_from          DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_until         DATE,
    valid_daterange     daterange GENERATED ALWAYS AS (daterange(valid_from, valid_until, '[)')) STORED,
    entry_date          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (object_name, class_name)
);
```

**Indexes:**
- `idx_object_instance_name` - object_name
- `idx_object_instance_name_lower` - LOWER(object_name)
- `idx_object_instance_canonical_full_name` - canonical_full_name
- `idx_object_instance_class_name` - class_name
- `idx_object_instance_status` - status
- `idx_object_instance_name_trgm` - GIN (LOWER(object_name) gin_trgm_ops) [trigram]
- `idx_object_instance_valid_daterange` - GIST (valid_daterange)

**Temporal Support:** Valid date ranges with auto-generated daterange

---

### 4.2 Upsert Object Instance Signature
**File:** [object_db.py#L2159](object_db.py#L2159)

```python
def upsert_object_instance(
    connection: psycopg2.extensions.connection,
    object_name: str,
    class_name: str,
    metadata: dict[str, Any] | None = None,
    status: str = "active",
    valid_from: date | str | None = None,
    valid_until: date | str | None = None,
) -> dict[str, Any]:
```

**Returns:**
```python
{
    "object_id": int,
    "object_name": str,
    "canonical_full_name": str | None,
    "class_name": str,
    "metadata": dict,
    "status": str,
    "valid_from": date,
    "valid_until": date | None,
    "entry_date": datetime
}
```

**Behavior:**
- ON CONFLICT: merges metadata (union of existing + new)
- Canonical name auto-generated for "person" class
- Uses CURRENT_DATE if valid_from not provided

---

### 4.3 Object Relationship Table
**File:** [object_db.py#L211](object_db.py#L211)

```sql
CREATE TABLE IF NOT EXISTS object_relationship (
    relationship_id     BIGSERIAL PRIMARY KEY,
    relationship_name   VARCHAR(64) NOT NULL,
    relationship_cat    VARCHAR(64) NOT NULL,
    src_object_id       BIGINT NOT NULL REFERENCES object_instance(object_id) ON DELETE CASCADE,
    tar_object_id       BIGINT NOT NULL REFERENCES object_instance(object_id) ON DELETE CASCADE,
    confidence          NUMERIC(5,4),
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    valid_from          DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_until         DATE,
    valid_daterange     daterange GENERATED ALWAYS AS (daterange(valid_from, valid_until, '[)')) STORED,
    entry_date          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (relationship_name, src_object_id, tar_object_id)
);
```

**Indexes:**
- `idx_object_relationship_name` - relationship_name
- `idx_object_relationship_src` - src_object_id
- `idx_object_relationship_tar` - tar_object_id
- `idx_object_relationship_cat` - relationship_cat
- `idx_object_relationship_valid_daterange` - GIST (valid_daterange)

---

### 4.4 Upsert Object Relationship Signature
**File:** [object_db.py#L2251](object_db.py#L2251)

```python
def upsert_object_relationship(
    connection: psycopg2.extensions.connection,
    relationship_name: str,
    relationship_cat: str,
    src_object_id: int,
    tar_object_id: int,
    confidence: float | None = None,
    metadata: dict[str, Any] | None = None,
    valid_from: date | str | None = None,
    valid_until: date | str | None = None,
) -> dict[str, Any]:
```

**Returns:** dict with relationship_id and all stored fields

**Behavior:**
- Canonicalizes relationship_name (e.g., "shareholder" → "shareholder_of")
- Finds semantic equivalents before upserting
- Supports temporal validity ranges

---

### 4.5 Attribute Table (Generic Attributes)
**File:** [object_db.py#L325](object_db.py#L325)

```sql
CREATE TABLE IF NOT EXISTS attribute (
    attr_id              BIGSERIAL PRIMARY KEY,
    src_id               BIGINT NOT NULL,
    src_type             VARCHAR(16) NOT NULL 
        CHECK (src_type IN ('part','document','class','object','relationship')),
    attr_type            VARCHAR(64) NOT NULL,
    valid_from           DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_until          DATE,
    valid_range tsrange GENERATED ALWAYS AS (tsrange(valid_from, valid_until)) STORED,
    valid_daterange      daterange GENERATED ALWAYS AS (daterange(valid_from, valid_until, '[)')) STORED,
    attr_json            JSONB NOT NULL,
    search_txt           TEXT,
    search_vec           TSVECTOR,
    entry_date           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Indexes:**
- `idx_attr_src` - (src_type, src_id)
- `idx_attr_type` - attr_type
- `idx_attr_json_gin` - GIN(attr_json)
- `idx_attr_search_vec` - GIN(search_vec)
- `idx_attributes_valid_range` - GIST (valid_range)
- `idx_attributes_valid_daterange` - GIST (valid_daterange)

---

### 4.6 Document Table
**File:** [object_db.py#L262](object_db.py#L262)

```sql
CREATE TABLE IF NOT EXISTS document (
    doc_id                BIGSERIAL PRIMARY KEY,
    doc_key               VARCHAR(512),
    doc_path              VARCHAR(1024),
    doc_cat               VARCHAR(128),  -- "invoice", "email", "note", etc.
    doc_type              VARCHAR(128),  -- "document", "spreadsheet", etc.
    doc_date              DATE,
    doc_theme             TEXT,
    keyword_text          TEXT,
    keyword_vec           TSVECTOR,
    identifiers_kv        JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    status                VARCHAR(32) NOT NULL DEFAULT 'active',
    valid_from            DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_until           DATE,
    valid_daterange       daterange GENERATED ALWAYS AS (daterange(valid_from, valid_until, '[)')) STORED,
    entry_date            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Indexes:**
- `idx_docs_cat`, `idx_docs_type`, `idx_docs_date` - filtering
- `idx_docs_key_trgm`, `idx_docs_keyword_trgm` - trigram search
- `idx_docs_identifiers_gin`, `idx_docs_metadata_gin` - JSONB queries
- `idx_docs_valid_daterange` - GIST (temporal queries)

---

### 4.7 Part Table (Document Parts to Entities)
**File:** [object_db.py#L307](object_db.py#L307)

```sql
CREATE TABLE IF NOT EXISTS part (
    part_id               BIGSERIAL PRIMARY KEY,
    doc_id                BIGINT NOT NULL REFERENCES document(doc_id) ON DELETE CASCADE,
    part_key              VARCHAR(128) NOT NULL,
    class_id              BIGINT REFERENCES object_class(class_id) ON DELETE CASCADE,
    object_id             BIGINT REFERENCES object_instance(object_id) ON DELETE CASCADE,
    relationship_id       BIGINT REFERENCES object_relationship(relationship_id) ON DELETE CASCADE,
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    entry_date            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (doc_id, part_key)
);
```

**Purpose:** Links document extraction results to schema entities (class/object/relationship)

---

## 5. Current API Structure

### 5.1 API Application
**File:** [idms_api_server/application.py](idms_api_server/application.py)

```python
app = FastAPI(
    title="IDMS API",
    version="1.0.0",
    description="Unified API for IDMS ingestion, chat, actions, and grounded queries.",
)

# Registered routers:
app.include_router(system_router)           # /api/system/*
app.include_router(ingestion_router)        # /api/ingest/*
app.include_router(interaction_router)      # /api/*
app.include_router(notes_router)            # /api/notes/*
app.include_router(business_rules_router)   # /api/business-rules/*
app.include_router(schema_proposals_router) # /api/schema-proposals/*
app.include_router(accounting_router)       # /api/accounting/*
```

---

### 5.2 Ingestion Router
**File:** [idms_api_server/routers/ingestion.py](idms_api_server/routers/ingestion.py)

**Prefix:** `/api/ingest`

Key endpoints (sample):
- `POST /api/ingest/path` - Ingest from file path (with optional processing rules)
- `POST /api/ingest/reprocess` - Reprocess previously ingested document

**Key method signatures:**

```python
@router.post("/path")
def ingest_path(payload: IngestPathRequest) -> dict[str, Any]:
    """
    payload:
        - source_path: str (local or gs://)
        - file_name: str (optional)
        - note: str (optional processing instructions)
        - processing_rule_names: list[str] (business rules to apply)
        - tags: list[str]
        - metadata: dict
        - prefer_markdown_input: bool (use cached markdown vs original)
    """

@router.post("/reprocess")
def reprocess_document(payload: ReprocessRequest) -> dict[str, Any]:
    """
    payload:
        - doc_id: int
        - prefer_markdown_input: bool | None
        - reuse_markdown: bool | None
        - persist_markdown: bool | None
    """
```

---

### 5.3 Interaction Router
**File:** [idms_api_server/routers/interaction.py](idms_api_server/routers/interaction.py)

**Prefix:** `/api`

```python
@router.post("/chat")
def chat(payload: ChatRequest) -> dict[str, Any]:
    """
    payload:
        - message: str
        - use_query_tool: bool (default: True)
    
    Returns:
        - success: bool
        - result: dict (varies by message type, may include note ingestion result)
    """

@router.post("/query")
def query(payload: QueryRequest) -> dict[str, Any]:
    """
    payload:
        - question: str
        - max_steps: int (default: 3)
    
    Executes grounded query logic against IDMS data
    """

@router.post("/action")
def action(payload: ActionRequest) -> dict[str, Any]:
    """
    payload:
        - action_name: str
        - payload: dict[str, Any]
    """
```

---

### 5.4 Schema Proposals Router
**File:** [idms_api_server/routers/schema_proposals.py](idms_api_server/routers/schema_proposals.py)

**Prefix:** `/api/schema-proposals`

Key endpoints (sample):
- `GET /list` - List active schema proposals
- `POST /attribute-proposal/{proposal_id}/status` - Update proposal status
- `POST /promotion-batch/create` - Create promotion batch from approved proposals
- `POST /promotion-batch/{batch_id}/status` - Update batch status

**Request schemas:**

```python
class SolfAttributeProposalStatusRequest(BaseModel):
    status: str  # "proposed", "approved", "rejected"
    reviewed_by: str = "api:user"
    note: str = ""

class SolfSchemaPromotionBatchCreateRequest(BaseModel):
    batch_name: str = ""
    class_name: str = ""  # optional filter
    proposal_ids: list[int] = []  # optional explicit ids
    created_by: str = "api:user"
    note: str = ""

class SolfSchemaPromotionBatchStatusRequest(BaseModel):
    status: str  # "draft", "reviewed", "exported", "applied"
    reviewed_by: str = "api:user"
    note: str = ""
```

---

### 5.5 Business Rules Router
**File:** [idms_api_server/routers/business_rules.py](idms_api_server/routers/business_rules.py)

**Prefix:** `/api/business-rules`

```python
class BusinessRuleCreateRequest(BaseModel):
    rule_text: str  # Natural language rule spec
    rule_name: str = ""
    created_by: str = "api:user"
    is_active: bool = True

class BusinessRuleUpdateRequest(BaseModel):
    rule_text: str
    rule_name: str = ""
    updated_by: str = "api:user"
    is_active: bool | None = None

class BusinessRuleSimulationRequest(BaseModel):
    requested_attribute: str  # Attribute to resolve
    context: dict[str, Any]  # country, document_type, entity_class, etc.
    sample_attributes: dict[str, Any]
    rule_id: int | None = None
    include_inactive: bool = True
```

---

### 5.6 Notes Router
**File:** [idms_api_server/routers/notes.py](idms_api_server/routers/notes.py)

**Prefix:** `/api/notes`

```python
class NoteIngestRequest(BaseModel):
    title: str
    content: str
    note_date: str = ""  # YYYY-MM-DD
    tags: list[str] = []
    metadata: dict[str, Any] = {}
    source: str = "web"
```

---

## 6. Related Query Methods

### 6.1 Search Objects
**File:** [object_db.py#L510](object_db.py#L510)

```python
def search_objects(
    connection: psycopg2.extensions.connection,
    name_query: str,
    class_name: str | None = None,
    attributes: dict[str, Any] | None = None,
    limit: int = 20,
    solf_matcher: Callable[[dict[str, Any]], bool] | None = None,
    as_of_date: date | None = None,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
```

**Returns:** List of object instances with similarity scores

---

### 6.2 Get Object Instance
**File:** [object_db.py#L2061](object_db.py#L2061)

```python
def get_object_instance(
    connection: psycopg2.extensions.connection,
    object_id: int,
) -> dict[str, Any] | None:
```

---

### 6.3 Get Object Relationships
**File:** [object_db.py#L1056](object_db.py#L1056)

```python
def get_object_relationship(
    connection: psycopg2.extensions.connection,
    relationship_id: int,
) -> dict[str, Any] | None:
```

---

## 7. Key Supporting Tables

### 7.1 Business Rules Table
**File:** [object_db.py#L466](object_db.py#L466)

```sql
CREATE TABLE IF NOT EXISTS business_rules (
    rule_id               BIGSERIAL PRIMARY KEY,
    rule_name             VARCHAR(256) NOT NULL,
    rule_text             TEXT NOT NULL,
    structured_rule       JSONB NOT NULL DEFAULT '{}'::jsonb,
    solf_script           TEXT NOT NULL DEFAULT '',
    scope                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active             BOOLEAN NOT NULL DEFAULT true,
    created_by            VARCHAR(128),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

### 7.2 Resolution Ambiguity Queue
**File:** [object_db.py#L348](object_db.py#L348)

```sql
CREATE TABLE IF NOT EXISTS resolution_ambiguity_queue (
    queue_id             BIGSERIAL PRIMARY KEY,
    entity_id            VARCHAR(128),
    object_name          VARCHAR(256) NOT NULL,
    class_name           VARCHAR(256) NOT NULL,
    doc_id               BIGINT REFERENCES document(doc_id) ON DELETE SET NULL,
    payload              JSONB NOT NULL DEFAULT '{}'::jsonb,
    resolution_result    JSONB NOT NULL DEFAULT '{}'::jsonb,
    status               VARCHAR(32) NOT NULL DEFAULT 'pending',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Purpose:** Queue for resolving ambiguous entity references during extraction

---

## 8. Configuration

### 8.1 Environment Variables (idms_config.py)

Key database settings:
```python
DB_NAME: str = os.getenv("IDMS_DB_NAME", "idms_proto")
DB_HOST: str = os.getenv("IDMS_DB_HOST", "localhost")
DB_USER: str = os.getenv("IDMS_DB_USER", "postgres")
DB_PASSWORD: str = os.getenv("IDMS_DB_PASSWORD", "")
DB_PORT: str = os.getenv("IDMS_DB_PORT", "5432")
```

SOLF governance thresholds:
```python
SOLF_PROPOSAL_PRIORITY_HIGH_THRESHOLD: int = 8
SOLF_PROPOSAL_PRIORITY_MEDIUM_THRESHOLD: int = 4
SOLF_PROPOSAL_SUGGEST_OCCURRENCE_THRESHOLD: int = 3
SOLF_PROPOSAL_SUGGEST_PRIORITY_THRESHOLD: int = 8
```

---

## Summary Table

| Component | Key File | Primary Type | Key Method |
|-----------|----------|--------------|------------|
| Patterns | pattern_library.py | Class: PatternLibrary | add_pattern() |
| SOLF Clauses | pattern_library.py | Class: PatternLibrary | upsert_solf_clause() |
| Schema Classes | ingest.py | Dataclass: SolfClassDef | ensure_solf_classes_in_db() |
| Objects/Entities | object_db.py | Functions | upsert_object_instance() |
| Relationships | object_db.py | Functions | upsert_object_relationship() |
| API | idms_api_server/application.py | FastAPI | Chat, Query, Action, Ingest, Schema |
| Database | sql_db.py + object_db.py | Schema DDL | Connection: get_connection() |

---

**Last Updated:** 2026-06-16  
**Status:** Current with Phase 4 implementation
