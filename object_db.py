import logging
import json
import re
import tempfile
import zipfile
from datetime import date, datetime
from collections import defaultdict
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Callable

import psycopg2
from psycopg2.extras import Json
from idms_config import DB_NAME, DB_HOST, DB_USER, DB_PASSWORD, DB_PORT


def get_connection() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        database=DB_NAME,
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT,
    )


def _canonical_person_full_name(value: Any) -> str:
    """Normalize a person name into a canonical full-name form."""
    text = str(value or "").strip()
    if not text:
        return ""

    text = re.sub(
        r"^(?:dr\.?|prof\.?|mr\.?|mrs\.?|ms\.?|herr|frau)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    if "," in text:
        last_name, first_names = [part.strip() for part in text.split(",", 1)]
        if last_name and first_names:
            text = f"{first_names} {last_name}"

    text = re.sub(r"[.,;:_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_RELATIONSHIP_CANONICAL_MAP = {
    "shareholder": "shareholder_of",
    "shareholders": "shareholder_of",
    "shareholder_of": "shareholder_of",
    "stockholder": "shareholder_of",
    "stockholders": "shareholder_of",
    "stockholder_of": "shareholder_of",
    "president": "president_of",
    "presidents": "president_of",
    "president_of": "president_of",
}


def canonicalize_relationship_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "related_to"

    text = text.replace("&", " and ")
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        return "related_to"

    return _RELATIONSHIP_CANONICAL_MAP.get(text, text)


def expand_relationship_names(value: Any) -> list[str]:
    text = str(value or "").strip().lower()
    if not text:
        return ["related_to"]

    normalized = text.replace("&", " and ")
    normalized = re.sub(r"[\s\-]+", "_", normalized)
    normalized = re.sub(r"[^a-z0-9_]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        return ["related_to"]

    parts = [normalized]
    if "_and_" in normalized:
        split_parts = [part.strip("_") for part in normalized.split("_and_") if part.strip("_")]
        if split_parts:
            parts = split_parts

    expanded: list[str] = []
    for part in parts:
        canonical = canonicalize_relationship_name(part)
        if canonical and canonical not in expanded:
            expanded.append(canonical)

    return expanded or ["related_to"]


def _relationship_semantic_aliases(relationship_name: str) -> list[str]:
    canonical = canonicalize_relationship_name(relationship_name)
    aliases = {canonical}

    for alias, mapped in _RELATIONSHIP_CANONICAL_MAP.items():
        if mapped == canonical:
            aliases.add(alias)

    if canonical.endswith("_of"):
        base = canonical[:-3]
        aliases.add(base)
        aliases.add(f"{base}s")

    return sorted(alias for alias in aliases if alias)


def _find_semantic_equivalent_relationship(
    connection: psycopg2.extensions.connection,
    relationship_name: str,
    relationship_cat: str,
    src_object_id: int,
    tar_object_id: int,
) -> dict[str, Any] | None:
    aliases = _relationship_semantic_aliases(relationship_name)
    if not aliases:
        return None

    sql = """
    SELECT relationship_id, relationship_name, relationship_cat, src_object_id, tar_object_id, confidence, metadata, valid_from, valid_until, entry_date
    FROM object_relationship
    WHERE src_object_id = %s
      AND tar_object_id = %s
      AND relationship_cat = %s
      AND LOWER(relationship_name) = ANY(%s)
    ORDER BY CASE WHEN LOWER(relationship_name) = %s THEN 0 ELSE 1 END, relationship_id
    LIMIT 1
    """

    canonical = canonicalize_relationship_name(relationship_name)
    with connection.cursor() as cursor:
        cursor.execute(sql, (src_object_id, tar_object_id, relationship_cat, aliases, canonical))
        row = cursor.fetchone()

    if not row:
        return None

    return {
        "relationship_id": row[0],
        "relationship_name": row[1],
        "relationship_cat": row[2],
        "src_object_id": row[3],
        "tar_object_id": row[4],
        "confidence": row[5],
        "metadata": row[6] or {},
        "valid_from": row[7],
        "valid_until": row[8],
        "entry_date": row[9],
    }


def _table_exists(connection: psycopg2.extensions.connection, table_name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
        return cursor.fetchone()[0] is not None


def _ensure_object_alias_table(connection: psycopg2.extensions.connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(create_object_alias_table)


create_class_table = """
CREATE TABLE IF NOT EXISTS object_class (
    class_id            BIGSERIAL PRIMARY KEY,
    class_name          VARCHAR(256) NOT NULL UNIQUE,
    parent_class_id     BIGINT REFERENCES object_class(class_id) ON DELETE SET NULL,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    entry_date          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_object_class_name ON object_class(class_name);
CREATE INDEX IF NOT EXISTS idx_object_class_parent ON object_class(parent_class_id);
CREATE INDEX IF NOT EXISTS idx_object_class_metadata_gin ON object_class USING GIN(metadata);
"""


create_object_table = """
CREATE TABLE IF NOT EXISTS object_instance (
    object_id           BIGSERIAL PRIMARY KEY,
    object_name         VARCHAR(256) NOT NULL,
    canonical_full_name VARCHAR(256),
    class_name          VARCHAR(256) NOT NULL REFERENCES object_class(class_name) ON DELETE RESTRICT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              VARCHAR(32) NOT NULL DEFAULT 'active',
    valid_from          DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_until         DATE,
    valid_daterange     daterange GENERATED ALWAYS AS (daterange(valid_from, valid_until, '[)')) STORED,
    entry_date          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (object_name, class_name)
);

CREATE INDEX IF NOT EXISTS idx_object_instance_name ON object_instance(object_name);
CREATE INDEX IF NOT EXISTS idx_object_instance_name_lower ON object_instance (LOWER(object_name));
CREATE INDEX IF NOT EXISTS idx_object_instance_canonical_full_name ON object_instance(canonical_full_name);
CREATE INDEX IF NOT EXISTS idx_object_instance_canonical_full_name_lower ON object_instance (LOWER(canonical_full_name));
CREATE INDEX IF NOT EXISTS idx_object_instance_class_name ON object_instance(class_name);
CREATE INDEX IF NOT EXISTS idx_object_instance_metadata_gin ON object_instance USING GIN(metadata);
CREATE INDEX IF NOT EXISTS idx_object_instance_name_trgm ON object_instance USING GIN (LOWER(object_name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_object_instance_canonical_full_name_trgm ON object_instance USING GIN (LOWER(canonical_full_name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_object_instance_status ON object_instance(status);
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'object_instance' AND column_name = 'valid_daterange'
    ) THEN
        CREATE INDEX IF NOT EXISTS idx_object_instance_valid_daterange ON object_instance USING GIST (valid_daterange);
    END IF;
END $$;
"""


create_relationship_table = """
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

CREATE INDEX IF NOT EXISTS idx_object_relationship_name ON object_relationship(relationship_name);
CREATE INDEX IF NOT EXISTS idx_object_relationship_src ON object_relationship(src_object_id);
CREATE INDEX IF NOT EXISTS idx_object_relationship_tar ON object_relationship(tar_object_id);
CREATE INDEX IF NOT EXISTS idx_object_relationship_cat ON object_relationship(relationship_cat);
CREATE INDEX IF NOT EXISTS idx_object_relationship_metadata_gin ON object_relationship USING GIN(metadata);
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'object_relationship' AND column_name = 'valid_daterange'
    ) THEN
        CREATE INDEX IF NOT EXISTS idx_object_relationship_valid_daterange ON object_relationship USING GIST (valid_daterange);
    END IF;
END $$;
"""


create_document_table = """
CREATE TABLE IF NOT EXISTS document (
    doc_id                BIGSERIAL PRIMARY KEY,
    doc_name              VARCHAR(512),
    doc_key               VARCHAR(512),
    doc_path              VARCHAR(1024),
    markdown_path         VARCHAR(1024),
    doc_cat               VARCHAR(128),
    doc_type              VARCHAR(128),
    doc_date              DATE,
    doc_desc              TEXT,
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

CREATE INDEX IF NOT EXISTS idx_docs_cat ON document(doc_cat);
CREATE INDEX IF NOT EXISTS idx_docs_type ON document(doc_type);
CREATE INDEX IF NOT EXISTS idx_docs_key ON document(doc_key);
CREATE INDEX IF NOT EXISTS idx_docs_key_lower ON document (LOWER(doc_key));
CREATE INDEX IF NOT EXISTS idx_docs_key_trgm ON document USING GIN (LOWER(doc_key) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_docs_path ON document(doc_path);
CREATE INDEX IF NOT EXISTS idx_docs_markdown_path ON document(markdown_path);
CREATE UNIQUE INDEX IF NOT EXISTS uq_document_doc_path_nonnull ON document(doc_path) WHERE doc_path IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_docs_date ON document(doc_date);
CREATE INDEX IF NOT EXISTS idx_docs_identifiers_gin ON document USING GIN(identifiers_kv);
CREATE INDEX IF NOT EXISTS idx_docs_metadata_gin ON document USING GIN(metadata);
CREATE INDEX IF NOT EXISTS idx_docs_keyword_vec ON document USING GIN(keyword_vec);
CREATE INDEX IF NOT EXISTS idx_docs_keyword_trgm ON document USING GIN (LOWER(keyword_text) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_docs_user_description_trgm ON document USING GIN (LOWER(metadata->>'user_description') gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_docs_user_tags_trgm ON document USING GIN (LOWER(metadata->>'user_tags') gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_docs_status ON document(status);
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'document' AND column_name = 'valid_daterange'
    ) THEN
        CREATE INDEX IF NOT EXISTS idx_docs_valid_daterange ON document USING GIST (valid_daterange);
    END IF;
END $$;
"""

create_part_table = """
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

CREATE INDEX IF NOT EXISTS idx_part_doc ON part(doc_id);
CREATE INDEX IF NOT EXISTS idx_part_class ON part(class_id);
CREATE INDEX IF NOT EXISTS idx_part_object ON part(object_id);
CREATE INDEX IF NOT EXISTS idx_part_relationship ON part(relationship_id);
CREATE INDEX IF NOT EXISTS idx_part_metadata_gin ON part USING GIN(metadata);
"""


create_document_table_cell_table = """
CREATE TABLE IF NOT EXISTS document_table_cell (
    cell_id               BIGSERIAL PRIMARY KEY,
    doc_id                BIGINT NOT NULL REFERENCES document(doc_id) ON DELETE CASCADE,
    table_index           INTEGER NOT NULL,
    row_index             INTEGER NOT NULL,
    row_label             TEXT,
    column_index          INTEGER NOT NULL,
    column_label          TEXT,
    cell_value            TEXT,
    source_kind           VARCHAR(32) NOT NULL DEFAULT 'document',
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (doc_id, table_index, row_index, column_index)
);

CREATE INDEX IF NOT EXISTS idx_doc_table_cell_doc_id ON document_table_cell(doc_id);
CREATE INDEX IF NOT EXISTS idx_doc_table_cell_row_label_trgm ON document_table_cell USING GIN (LOWER(COALESCE(row_label, '')) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_doc_table_cell_column_label_trgm ON document_table_cell USING GIN (LOWER(COALESCE(column_label, '')) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_doc_table_cell_metadata_gin ON document_table_cell USING GIN(metadata);
"""


# Extended attribute table variant supporting class/object/relationship in addition to graph types
create_attribute_table = """
CREATE TABLE IF NOT EXISTS attribute (
    attr_id              BIGSERIAL PRIMARY KEY,
    src_id               BIGINT NOT NULL,
    src_type             VARCHAR(16) NOT NULL CHECK (src_type IN ('part','document','class','object','relationship')),
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

CREATE INDEX IF NOT EXISTS idx_attr_src ON attribute(src_type, src_id);
CREATE INDEX IF NOT EXISTS idx_attr_type ON attribute(attr_type);
CREATE INDEX IF NOT EXISTS idx_attr_json_gin ON attribute USING GIN(attr_json);
CREATE INDEX IF NOT EXISTS idx_attr_search_vec ON attribute USING GIN(search_vec);
CREATE INDEX IF NOT EXISTS idx_attributes_valid_range ON attribute USING GIST (valid_range);
CREATE INDEX IF NOT EXISTS idx_attributes_valid_daterange ON attribute USING GIST (valid_daterange);
"""


create_resolution_ambiguity_queue_table = """
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

CREATE INDEX IF NOT EXISTS idx_resolution_ambiguity_status ON resolution_ambiguity_queue(status);
CREATE INDEX IF NOT EXISTS idx_resolution_ambiguity_doc_id ON resolution_ambiguity_queue(doc_id);
CREATE INDEX IF NOT EXISTS idx_resolution_ambiguity_class_name ON resolution_ambiguity_queue(class_name);
CREATE INDEX IF NOT EXISTS idx_resolution_ambiguity_created_at ON resolution_ambiguity_queue(created_at);
CREATE INDEX IF NOT EXISTS idx_resolution_ambiguity_payload_gin ON resolution_ambiguity_queue USING GIN(payload);
CREATE INDEX IF NOT EXISTS idx_resolution_ambiguity_result_gin ON resolution_ambiguity_queue USING GIN(resolution_result);
"""


create_tx_match_index_pending_table = """
CREATE TABLE IF NOT EXISTS tx_match_index_pending (
    pending_id            BIGSERIAL PRIMARY KEY,
    doc_id                BIGINT REFERENCES document(doc_id) ON DELETE SET NULL,
    source                VARCHAR(64) NOT NULL DEFAULT 'ingestion',
    status                VARCHAR(32) NOT NULL DEFAULT 'pending',
    payload               JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message         TEXT,
    retry_count           INTEGER NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tx_match_pending_status ON tx_match_index_pending(status);
CREATE INDEX IF NOT EXISTS idx_tx_match_pending_doc_id ON tx_match_index_pending(doc_id);
CREATE INDEX IF NOT EXISTS idx_tx_match_pending_created_at ON tx_match_index_pending(created_at);
CREATE INDEX IF NOT EXISTS idx_tx_match_pending_payload_gin ON tx_match_index_pending USING GIN(payload);
"""


create_object_alias_table = """
CREATE TABLE IF NOT EXISTS object_alias (
    alias_id              BIGSERIAL PRIMARY KEY,
    primary_object_id     BIGINT NOT NULL REFERENCES object_instance(object_id) ON DELETE CASCADE,
    alias_object_id       BIGINT NOT NULL REFERENCES object_instance(object_id) ON DELETE CASCADE,
    alias_type            VARCHAR(64) NOT NULL DEFAULT 'same_entity',
    confidence            NUMERIC(5,4),
    status                VARCHAR(32) NOT NULL DEFAULT 'active',
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (primary_object_id <> alias_object_id),
    UNIQUE (primary_object_id, alias_object_id)
);

CREATE INDEX IF NOT EXISTS idx_object_alias_primary ON object_alias(primary_object_id);
CREATE INDEX IF NOT EXISTS idx_object_alias_alias ON object_alias(alias_object_id);
CREATE INDEX IF NOT EXISTS idx_object_alias_status ON object_alias(status);
CREATE INDEX IF NOT EXISTS idx_object_alias_type ON object_alias(alias_type);
CREATE INDEX IF NOT EXISTS idx_object_alias_metadata_gin ON object_alias USING GIN(metadata);
"""


create_semantic_patterns_table = """
CREATE TABLE IF NOT EXISTS semantic_patterns (
    pattern_id           BIGSERIAL PRIMARY KEY,
    pattern_text         VARCHAR(512) NOT NULL,
    semantic_concept     VARCHAR(128) NOT NULL,
    mapped_attributes    JSONB NOT NULL DEFAULT '{}'::jsonb,
    computation_rule     TEXT,
    entity_class         VARCHAR(128),
    source_type          VARCHAR(32) NOT NULL DEFAULT 'seeded' CHECK (source_type IN ('seeded','learned','manual')),
    confidence           NUMERIC(5,4) NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    pattern_language     VARCHAR(32) NOT NULL DEFAULT 'en',
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by           VARCHAR(128),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pattern_text, entity_class, pattern_language)
);

CREATE INDEX IF NOT EXISTS idx_semantic_patterns_text ON semantic_patterns(pattern_text);
CREATE INDEX IF NOT EXISTS idx_semantic_patterns_text_lower ON semantic_patterns (LOWER(pattern_text));
CREATE INDEX IF NOT EXISTS idx_semantic_patterns_concept ON semantic_patterns(semantic_concept);
CREATE INDEX IF NOT EXISTS idx_semantic_patterns_entity_class ON semantic_patterns(entity_class);
CREATE INDEX IF NOT EXISTS idx_semantic_patterns_source_type ON semantic_patterns(source_type);
CREATE INDEX IF NOT EXISTS idx_semantic_patterns_confidence ON semantic_patterns(confidence);
CREATE INDEX IF NOT EXISTS idx_semantic_patterns_language ON semantic_patterns(pattern_language);
CREATE INDEX IF NOT EXISTS idx_semantic_patterns_metadata_gin ON semantic_patterns USING GIN(metadata);
CREATE INDEX IF NOT EXISTS idx_semantic_patterns_mapped_attr_gin ON semantic_patterns USING GIN(mapped_attributes);
"""


create_pattern_synonyms_table = """
CREATE TABLE IF NOT EXISTS pattern_synonyms (
    synonym_id           BIGSERIAL PRIMARY KEY,
    pattern_id           BIGINT NOT NULL REFERENCES semantic_patterns(pattern_id) ON DELETE CASCADE,
    synonym_text         VARCHAR(512) NOT NULL,
    language             VARCHAR(32) NOT NULL DEFAULT 'en',
    semantic_distance    NUMERIC(5,4) NOT NULL DEFAULT 0.0 CHECK (semantic_distance >= 0 AND semantic_distance <= 1),
    match_type           VARCHAR(32) NOT NULL DEFAULT 'exact' CHECK (match_type IN ('exact','template','fuzzy','semantic')),
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pattern_id, synonym_text, language)
);

CREATE INDEX IF NOT EXISTS idx_pattern_synonyms_pattern_id ON pattern_synonyms(pattern_id);
CREATE INDEX IF NOT EXISTS idx_pattern_synonyms_text ON pattern_synonyms(synonym_text);
CREATE INDEX IF NOT EXISTS idx_pattern_synonyms_text_lower ON pattern_synonyms (LOWER(synonym_text));
CREATE INDEX IF NOT EXISTS idx_pattern_synonyms_language ON pattern_synonyms(language);
CREATE INDEX IF NOT EXISTS idx_pattern_synonyms_match_type ON pattern_synonyms(match_type);
CREATE INDEX IF NOT EXISTS idx_pattern_synonyms_semantic_distance ON pattern_synonyms(semantic_distance);
"""


create_semantic_terms_table = """
CREATE TABLE IF NOT EXISTS semantic_terms (
    term_id              BIGSERIAL PRIMARY KEY,
    kind                 VARCHAR(32) NOT NULL CHECK (kind IN ('attribute','relationship')),
    canonical_name       VARCHAR(256) NOT NULL,
    term_text            VARCHAR(512) NOT NULL,
    language             VARCHAR(32) NOT NULL DEFAULT 'und',
    source_type          VARCHAR(64) NOT NULL DEFAULT 'ingest' CHECK (source_type IN ('seeded','ingest','solf_clause','manual','llm_generated')),
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (kind, canonical_name, term_text, language)
);

CREATE INDEX IF NOT EXISTS idx_semantic_terms_kind ON semantic_terms(kind);
CREATE INDEX IF NOT EXISTS idx_semantic_terms_canonical_name ON semantic_terms(canonical_name);
CREATE INDEX IF NOT EXISTS idx_semantic_terms_term_text ON semantic_terms(term_text);
CREATE INDEX IF NOT EXISTS idx_semantic_terms_term_text_lower ON semantic_terms (LOWER(term_text));
CREATE INDEX IF NOT EXISTS idx_semantic_terms_source_type ON semantic_terms(source_type);
CREATE INDEX IF NOT EXISTS idx_semantic_terms_metadata_gin ON semantic_terms USING GIN(metadata);
"""


create_query_term_alias_table = """
CREATE TABLE IF NOT EXISTS query_term_alias (
    alias_id              BIGSERIAL PRIMARY KEY,
    alias_text            VARCHAR(512) NOT NULL,
    canonical_name        VARCHAR(256) NOT NULL,
    kind                  VARCHAR(32) NOT NULL CHECK (kind IN ('attribute','relationship','entity_type')),
    language              VARCHAR(32) NOT NULL DEFAULT 'und',
    priority              INTEGER NOT NULL DEFAULT 100,
    source_type           VARCHAR(64) NOT NULL DEFAULT 'manual',
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active             BOOLEAN NOT NULL DEFAULT true,
    created_by            VARCHAR(128),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (alias_text, kind, language)
);

CREATE INDEX IF NOT EXISTS idx_query_term_alias_text ON query_term_alias(alias_text);
CREATE INDEX IF NOT EXISTS idx_query_term_alias_text_lower ON query_term_alias (LOWER(alias_text));
CREATE INDEX IF NOT EXISTS idx_query_term_alias_canonical_name ON query_term_alias(canonical_name);
CREATE INDEX IF NOT EXISTS idx_query_term_alias_kind ON query_term_alias(kind);
CREATE INDEX IF NOT EXISTS idx_query_term_alias_is_active ON query_term_alias(is_active);
CREATE INDEX IF NOT EXISTS idx_query_term_alias_priority ON query_term_alias(priority);
CREATE INDEX IF NOT EXISTS idx_query_term_alias_metadata_gin ON query_term_alias USING GIN(metadata);
"""


create_solf_clauses_table = """
CREATE TABLE IF NOT EXISTS solf_clauses (
    clause_id            BIGSERIAL PRIMARY KEY,
    clause_name          VARCHAR(256) NOT NULL,
    clause_type          VARCHAR(32) NOT NULL CHECK (clause_type IN ('query_pattern','resolve_policy','ingest_rule','computation_rule')),
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

CREATE INDEX IF NOT EXISTS idx_solf_clauses_name ON solf_clauses(clause_name);
CREATE INDEX IF NOT EXISTS idx_solf_clauses_type ON solf_clauses(clause_type);
CREATE INDEX IF NOT EXISTS idx_solf_clauses_entity_class ON solf_clauses(entity_class);
CREATE INDEX IF NOT EXISTS idx_solf_clauses_is_active ON solf_clauses(is_active);
CREATE INDEX IF NOT EXISTS idx_solf_clauses_created_at ON solf_clauses(created_at);
CREATE INDEX IF NOT EXISTS idx_solf_clauses_metadata_gin ON solf_clauses USING GIN(metadata);
"""


create_business_rules_table = """
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

CREATE INDEX IF NOT EXISTS idx_business_rules_active ON business_rules(is_active);
CREATE INDEX IF NOT EXISTS idx_business_rules_name ON business_rules(rule_name);
CREATE INDEX IF NOT EXISTS idx_business_rules_scope_gin ON business_rules USING GIN(scope);
CREATE INDEX IF NOT EXISTS idx_business_rules_structured_gin ON business_rules USING GIN(structured_rule);
CREATE INDEX IF NOT EXISTS idx_business_rules_metadata_gin ON business_rules USING GIN(metadata);
"""


create_solf_workflow_registry_table = """
CREATE TABLE IF NOT EXISTS solf_workflow_registry (
    workflow_id           BIGSERIAL PRIMARY KEY,
    workflow_key          VARCHAR(256) NOT NULL UNIQUE,
    workflow_name         VARCHAR(256) NOT NULL,
    description           TEXT NOT NULL DEFAULT '',
    domain                VARCHAR(64),
    status                VARCHAR(32) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','review','published','deprecated','archived')),
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active             BOOLEAN NOT NULL DEFAULT TRUE,
    created_by            VARCHAR(128),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_solf_workflow_registry_status ON solf_workflow_registry(status);
CREATE INDEX IF NOT EXISTS idx_solf_workflow_registry_domain ON solf_workflow_registry(domain);
CREATE INDEX IF NOT EXISTS idx_solf_workflow_registry_active ON solf_workflow_registry(is_active);
CREATE INDEX IF NOT EXISTS idx_solf_workflow_registry_metadata_gin ON solf_workflow_registry USING GIN(metadata);
"""


create_solf_workflow_versions_table = """
CREATE TABLE IF NOT EXISTS solf_workflow_versions (
    workflow_version_id   BIGSERIAL PRIMARY KEY,
    workflow_id           BIGINT NOT NULL REFERENCES solf_workflow_registry(workflow_id) ON DELETE CASCADE,
    version_no            INTEGER NOT NULL,
    rule_id               BIGINT REFERENCES business_rules(rule_id) ON DELETE SET NULL,
    graph_spec            JSONB NOT NULL DEFAULT '{}'::jsonb,
    input_contract        JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_contract       JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active             BOOLEAN NOT NULL DEFAULT TRUE,
    created_by            VARCHAR(128),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workflow_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_solf_workflow_versions_workflow_id ON solf_workflow_versions(workflow_id);
CREATE INDEX IF NOT EXISTS idx_solf_workflow_versions_rule_id ON solf_workflow_versions(rule_id);
CREATE INDEX IF NOT EXISTS idx_solf_workflow_versions_active ON solf_workflow_versions(is_active);
CREATE INDEX IF NOT EXISTS idx_solf_workflow_versions_metadata_gin ON solf_workflow_versions USING GIN(metadata);
"""


create_solf_workflow_steps_table = """
CREATE TABLE IF NOT EXISTS solf_workflow_steps (
    step_id               BIGSERIAL PRIMARY KEY,
    workflow_version_id   BIGINT NOT NULL REFERENCES solf_workflow_versions(workflow_version_id) ON DELETE CASCADE,
    step_order            INTEGER NOT NULL,
    step_key              VARCHAR(256) NOT NULL,
    step_kind             VARCHAR(32) NOT NULL CHECK (step_kind IN ('clause','class_transform','class_generate','class_iterate','python_binding')),
    clause_id             BIGINT REFERENCES solf_clauses(clause_id) ON DELETE SET NULL,
    clause_name           VARCHAR(256),
    input_class           VARCHAR(128),
    output_class          VARCHAR(128),
    operation             VARCHAR(64),
    python_module         VARCHAR(256),
    python_function       VARCHAR(256),
    config                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workflow_version_id, step_order),
    UNIQUE (workflow_version_id, step_key)
);

CREATE INDEX IF NOT EXISTS idx_solf_workflow_steps_version_id ON solf_workflow_steps(workflow_version_id);
CREATE INDEX IF NOT EXISTS idx_solf_workflow_steps_kind ON solf_workflow_steps(step_kind);
CREATE INDEX IF NOT EXISTS idx_solf_workflow_steps_clause_id ON solf_workflow_steps(clause_id);
CREATE INDEX IF NOT EXISTS idx_solf_workflow_steps_config_gin ON solf_workflow_steps USING GIN(config);
"""


create_workflow_pipeline_run_tables = """
CREATE TABLE IF NOT EXISTS workflow_pipeline_run (
    run_id              BIGSERIAL PRIMARY KEY,
    workflow_version_id BIGINT REFERENCES solf_workflow_versions(workflow_version_id) ON DELETE SET NULL,
    workflow_key        VARCHAR(256),
    run_status          VARCHAR(32) NOT NULL DEFAULT 'pending'
                        CHECK (run_status IN ('pending','running','paused','completed','failed','cancelled')),
    paused_at_step_key  VARCHAR(256),
    input_context       JSONB NOT NULL DEFAULT '{}'::jsonb,
    current_context     JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_context      JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_by          VARCHAR(128),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_run_status ON workflow_pipeline_run(run_status);
CREATE INDEX IF NOT EXISTS idx_pipeline_run_workflow_version ON workflow_pipeline_run(workflow_version_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_run_workflow_key ON workflow_pipeline_run(workflow_key);
CREATE INDEX IF NOT EXISTS idx_pipeline_run_created_at ON workflow_pipeline_run(created_at);

CREATE TABLE IF NOT EXISTS workflow_pipeline_run_step (
    step_run_id         BIGSERIAL PRIMARY KEY,
    run_id              BIGINT NOT NULL REFERENCES workflow_pipeline_run(run_id) ON DELETE CASCADE,
    step_key            VARCHAR(256) NOT NULL,
    step_order          INTEGER NOT NULL,
    step_status         VARCHAR(32) NOT NULL DEFAULT 'pending'
                        CHECK (step_status IN ('pending','running','completed','paused','failed','skipped')),
    pause_reason        VARCHAR(64),
    interaction_prompt  TEXT,
    missing_data_desc   TEXT,
    required_doc_types  JSONB NOT NULL DEFAULT '[]'::jsonb,
    input_snapshot      JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_snapshot     JSONB NOT NULL DEFAULT '{}'::jsonb,
    user_response       JSONB NOT NULL DEFAULT '{}'::jsonb,
    doc_refs            JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_message       TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, step_key)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_step_run_id ON workflow_pipeline_run_step(run_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_step_status ON workflow_pipeline_run_step(step_status);
CREATE INDEX IF NOT EXISTS idx_pipeline_step_order ON workflow_pipeline_run_step(run_id, step_order);
"""


create_workflow_pipeline_run_document_links_table = """
CREATE TABLE IF NOT EXISTS workflow_pipeline_run_document_link (
    link_id              BIGSERIAL PRIMARY KEY,
    run_id               BIGINT NOT NULL REFERENCES workflow_pipeline_run(run_id) ON DELETE CASCADE,
    workflow_version_id  BIGINT REFERENCES solf_workflow_versions(workflow_version_id) ON DELETE SET NULL,
    workflow_key         VARCHAR(256),
    document_id          BIGINT NOT NULL,
    source               VARCHAR(32) NOT NULL DEFAULT 'system'
                         CHECK (source IN ('input_context','resume','step_response','system')),
    step_key             VARCHAR(256) NOT NULL DEFAULT '',
    linked_by            VARCHAR(128),
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, document_id, source, step_key)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_doc_link_run_id ON workflow_pipeline_run_document_link(run_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_doc_link_doc_id ON workflow_pipeline_run_document_link(document_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_doc_link_workflow_key ON workflow_pipeline_run_document_link(workflow_key);
CREATE INDEX IF NOT EXISTS idx_pipeline_doc_link_source ON workflow_pipeline_run_document_link(source);
CREATE INDEX IF NOT EXISTS idx_pipeline_doc_link_metadata_gin ON workflow_pipeline_run_document_link USING GIN(metadata);
"""


create_business_rule_workflow_links_table = """
CREATE TABLE IF NOT EXISTS business_rule_workflow_links (
    link_id               BIGSERIAL PRIMARY KEY,
    rule_id               BIGINT NOT NULL REFERENCES business_rules(rule_id) ON DELETE CASCADE,
    workflow_id           BIGINT NOT NULL REFERENCES solf_workflow_registry(workflow_id) ON DELETE CASCADE,
    workflow_version_id   BIGINT REFERENCES solf_workflow_versions(workflow_version_id) ON DELETE CASCADE,
    link_type             VARCHAR(32) NOT NULL DEFAULT 'uses' CHECK (link_type IN ('uses','creates','extends','overrides')),
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by            VARCHAR(128),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (rule_id, workflow_id, workflow_version_id, link_type)
);

CREATE INDEX IF NOT EXISTS idx_business_rule_workflow_links_rule_id ON business_rule_workflow_links(rule_id);
CREATE INDEX IF NOT EXISTS idx_business_rule_workflow_links_workflow_id ON business_rule_workflow_links(workflow_id);
CREATE INDEX IF NOT EXISTS idx_business_rule_workflow_links_version_id ON business_rule_workflow_links(workflow_version_id);
CREATE INDEX IF NOT EXISTS idx_business_rule_workflow_links_metadata_gin ON business_rule_workflow_links USING GIN(metadata);
"""


create_workflow_resource_alias_table = """
CREATE TABLE IF NOT EXISTS workflow_resource_alias (
    alias_id               BIGSERIAL PRIMARY KEY,
    workflow_id            BIGINT NOT NULL REFERENCES solf_workflow_registry(workflow_id) ON DELETE CASCADE,
    workflow_version_id    BIGINT NOT NULL REFERENCES solf_workflow_versions(workflow_version_id) ON DELETE CASCADE,
    resource_type          VARCHAR(32) NOT NULL CHECK (resource_type IN ('endpoint','queue','collection','purpose','scope','policy')),
    alias_name             VARCHAR(256) NOT NULL,
    canonical_name         VARCHAR(256) NOT NULL,
    metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by             VARCHAR(128),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workflow_version_id, resource_type, alias_name)
);

CREATE INDEX IF NOT EXISTS idx_workflow_resource_alias_workflow_id ON workflow_resource_alias(workflow_id);
CREATE INDEX IF NOT EXISTS idx_workflow_resource_alias_version_id ON workflow_resource_alias(workflow_version_id);
CREATE INDEX IF NOT EXISTS idx_workflow_resource_alias_type ON workflow_resource_alias(resource_type);
CREATE INDEX IF NOT EXISTS idx_workflow_resource_alias_alias_name ON workflow_resource_alias(alias_name);
CREATE INDEX IF NOT EXISTS idx_workflow_resource_alias_metadata_gin ON workflow_resource_alias USING GIN(metadata);
"""


create_workflow_extension_tables = """
CREATE TABLE IF NOT EXISTS workflow_extension_pack (
    extension_pack_id      BIGSERIAL PRIMARY KEY,
    extension_key          VARCHAR(256) NOT NULL UNIQUE,
    extension_name         VARCHAR(256) NOT NULL,
    description            TEXT NOT NULL DEFAULT '',
    scope_json             JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_by             VARCHAR(128),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_workflow_extension_pack_active ON workflow_extension_pack(is_active);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_pack_scope_gin ON workflow_extension_pack USING GIN(scope_json);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_pack_metadata_gin ON workflow_extension_pack USING GIN(metadata);

CREATE TABLE IF NOT EXISTS workflow_extension_version (
    extension_version_id   BIGSERIAL PRIMARY KEY,
    extension_pack_id      BIGINT NOT NULL REFERENCES workflow_extension_pack(extension_pack_id) ON DELETE CASCADE,
    version_no             INTEGER NOT NULL,
    status                 VARCHAR(32) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','review','published','deprecated','archived')),
    conditions_schema_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_by             VARCHAR(128),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (extension_pack_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_workflow_extension_version_pack_id ON workflow_extension_version(extension_pack_id);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_version_status ON workflow_extension_version(status);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_version_active ON workflow_extension_version(is_active);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_version_metadata_gin ON workflow_extension_version USING GIN(metadata);

CREATE TABLE IF NOT EXISTS workflow_extension_rule (
    extension_rule_id      BIGSERIAL PRIMARY KEY,
    extension_version_id   BIGINT NOT NULL REFERENCES workflow_extension_version(extension_version_id) ON DELETE CASCADE,
    rule_key               VARCHAR(256) NOT NULL,
    hook_point             VARCHAR(32) NOT NULL CHECK (hook_point IN ('on_run_start','before_step','after_step','on_pause','on_failure','on_complete')),
    target_step_key        VARCHAR(256),
    condition_json         JSONB NOT NULL DEFAULT '{}'::jsonb,
    precedence             INTEGER NOT NULL DEFAULT 100,
    conflict_policy        VARCHAR(16) NOT NULL DEFAULT 'skip' CHECK (conflict_policy IN ('skip','replace','merge','fail')),
    inserted_steps_json    JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (extension_version_id, rule_key)
);

CREATE INDEX IF NOT EXISTS idx_workflow_extension_rule_version_id ON workflow_extension_rule(extension_version_id);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_rule_hook_point ON workflow_extension_rule(hook_point);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_rule_condition_gin ON workflow_extension_rule USING GIN(condition_json);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_rule_steps_gin ON workflow_extension_rule USING GIN(inserted_steps_json);

CREATE TABLE IF NOT EXISTS workflow_extension_attachment (
    attachment_id          BIGSERIAL PRIMARY KEY,
    workflow_key           VARCHAR(256) NOT NULL,
    workflow_version_id    BIGINT REFERENCES solf_workflow_versions(workflow_version_id) ON DELETE CASCADE,
    extension_pack_id      BIGINT NOT NULL REFERENCES workflow_extension_pack(extension_pack_id) ON DELETE CASCADE,
    extension_version_id   BIGINT REFERENCES workflow_extension_version(extension_version_id) ON DELETE CASCADE,
    effective_from         DATE NOT NULL DEFAULT CURRENT_DATE,
    effective_until        DATE,
    priority               INTEGER NOT NULL DEFAULT 100,
    tenant_scope           VARCHAR(128),
    metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_by             VARCHAR(128),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_workflow_extension_attachment_workflow_key ON workflow_extension_attachment(workflow_key);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_attachment_workflow_version_id ON workflow_extension_attachment(workflow_version_id);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_attachment_pack_id ON workflow_extension_attachment(extension_pack_id);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_attachment_version_id ON workflow_extension_attachment(extension_version_id);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_attachment_tenant_scope ON workflow_extension_attachment(tenant_scope);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_attachment_active ON workflow_extension_attachment(is_active);
CREATE INDEX IF NOT EXISTS idx_workflow_extension_attachment_metadata_gin ON workflow_extension_attachment USING GIN(metadata);
"""


create_extensions = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
"""


def _to_search_tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    return [str(value).strip().lower()] if str(value).strip() else []


def _load_object_attributes(
    connection: psycopg2.extensions.connection,
    object_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    if not object_ids:
        return {}

    sql = """
    SELECT src_id, attr_type, attr_json
    FROM attribute
    WHERE src_type = 'object'
      AND src_id = ANY(%s)
    """

    attribute_map: dict[int, list[dict[str, Any]]] = defaultdict(list)
    with connection.cursor() as cursor:
        cursor.execute(sql, (object_ids,))
        for src_id, attr_type, attr_json in cursor.fetchall():
            attribute_map[int(src_id)].append(
                {
                    "attr_type": str(attr_type).lower(),
                    "attr_json_text": str(attr_json).lower(),
                }
            )
    return dict(attribute_map)


def _attribute_match_count(
    object_attrs: list[dict[str, Any]],
    expected_attributes: dict[str, Any],
) -> int:
    if not expected_attributes:
        return 0

    score = 0
    normalized_expectations = {
        str(attr_type).lower(): _to_search_tokens(attr_value)
        for attr_type, attr_value in expected_attributes.items()
    }

    for attr_type, expected_tokens in normalized_expectations.items():
        if not expected_tokens:
            continue

        has_match = any(
            row["attr_type"] == attr_type
            and all(token in row["attr_json_text"] for token in expected_tokens)
            for row in object_attrs
        )
        if has_match:
            score += 1
    return score


def search_objects(
    connection: psycopg2.extensions.connection,
    name_query: str,
    class_name: str | None = None,
    attributes: dict[str, Any] | None = None,
    limit: int = 20,
    solf_matcher: Callable[[dict[str, Any]], bool] | None = None,
    as_of_date: date | None = None,
    include_inactive: bool = False,
    include_merged_variants: bool = True,
) -> list[dict[str, Any]]:
    """Search objects by partial name and optionally disambiguate with attributes.

    When multiple candidates are returned, attribute matching boosts and reorders results.
    Optionally pass a SOLF matcher callback for clause-based filtering.
    When include_merged_variants=True (default), also searches for merged objects that match
    the name and resolves them to their canonical forms transparently.
    """
    if not name_query.strip():
        return []

    # Build the LIKE pattern in Python to avoid %s-in-string confusion
    like_prefix = name_query + "%"
    like_contains = "%" + name_query + "%"
    name_lower = name_query.lower()
    like_prefix_lower = name_lower + "%"
    like_contains_lower = "%" + name_lower + "%"

    sql = """
    SELECT
        oi.object_id,
        oi.object_name,
        oi.canonical_full_name,
        oi.class_name,
        oi.metadata,
        oi.status,
        similarity(LOWER(COALESCE(oi.canonical_full_name, oi.object_name)), LOWER(%s)) AS sim_score,
        CASE
            WHEN LOWER(oi.object_name) = %s OR LOWER(COALESCE(oi.canonical_full_name, '')) = %s THEN 1
            WHEN LOWER(oi.object_name) LIKE %s OR LOWER(COALESCE(oi.canonical_full_name, '')) LIKE %s THEN 2
            WHEN LOWER(oi.object_name) LIKE %s OR LOWER(COALESCE(oi.canonical_full_name, '')) LIKE %s THEN 3
            ELSE 4
        END AS name_rank
    FROM object_instance oi
    WHERE (%s IS NULL OR oi.class_name = %s)
            AND (%s OR COALESCE(oi.status, 'active') = 'active')
            AND (oi.valid_from <= %s)
            AND (oi.valid_until IS NULL OR oi.valid_until > %s)
            AND (
                LOWER(oi.object_name) LIKE %s
                OR LOWER(COALESCE(oi.canonical_full_name, '')) LIKE %s
            )
        ORDER BY name_rank, sim_score DESC, oi.object_name
    LIMIT %s
    """

    with connection.cursor() as cursor:
        effective_date = as_of_date or date.today()
        cursor.execute(
            sql,
            (
                name_query,         # for similarity()
                name_lower,         # CASE exact match
                name_lower,         # CASE exact match canonical
                like_prefix_lower,  # CASE prefix match
                like_prefix_lower,  # CASE prefix match canonical
                like_contains_lower, # CASE contains match
                like_contains_lower, # CASE contains match canonical
                class_name,         # WHERE class_name check 1
                class_name,         # WHERE class_name check 2
                include_inactive,   # WHERE status filter
                effective_date,     # WHERE valid_from
                effective_date,     # WHERE valid_until
                like_contains_lower, # WHERE contains check (object_name)
                like_contains_lower, # WHERE contains check (canonical_full_name)
                limit,              # LIMIT
            ),
        )
        rows = cursor.fetchall()

    candidates = [
        {
            "object_id": row[0],
            "object_name": row[1],
            "canonical_full_name": row[2],
            "class_name": row[3],
            "metadata": row[4] or {},
            "status": row[5],
            "similarity": float(row[6]),
            "name_rank": row[7],
            "attribute_matches": 0,
            "score": 0,
        }
        for row in rows
    ]

    # Note: do NOT early-return here if candidates is empty.
    # include_merged_variants may still find and add candidates below.

    expected_attributes = attributes or {}
    if expected_attributes and len(candidates) > 1:
        object_ids = [int(candidate["object_id"]) for candidate in candidates]
        attribute_map = _load_object_attributes(connection, object_ids)
        total_expected = len([key for key in expected_attributes if str(key).strip()])

        for candidate in candidates:
            object_id = int(candidate["object_id"])
            object_attrs = attribute_map.get(object_id, [])
            match_count = _attribute_match_count(object_attrs, expected_attributes)
            candidate["attribute_matches"] = match_count
            candidate["attributes_checked"] = total_expected
            # Prioritize attribute evidence first, then name quality.
            candidate["score"] = (match_count * 100) - int(candidate["name_rank"])

        candidates.sort(
            key=lambda item: (
                -int(item["attribute_matches"]),
                int(item["name_rank"]),
                str(item["object_name"]).lower(),
            )
        )
    else:
        for candidate in candidates:
            candidate["score"] = (candidate["similarity"] * 100.0) - int(candidate["name_rank"])

        candidates.sort(
            key=lambda item: (
                -float(item["similarity"]),
                int(item["name_rank"]),
                str(item["object_name"]).lower(),
            )
        )

    if solf_matcher is not None:
        filtered_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                if solf_matcher(candidate):
                    filtered_candidates.append(candidate)
            except Exception:
                logging.exception("SOLF matcher failed for candidate %s", candidate.get("object_name"))
        candidates = filtered_candidates

    # Search for merged object variants that match the name and add them to candidates
    if include_merged_variants:
        merged_variant_sql = """
        SELECT
            oi.object_id,
            oi.object_name,
            oi.canonical_full_name,
            oi.class_name,
            oi.metadata,
            oi.status,
            similarity(LOWER(COALESCE(oi.canonical_full_name, oi.object_name)), LOWER(%s)) AS sim_score
        FROM object_instance oi
        WHERE (%s IS NULL OR oi.class_name = %s)
            AND COALESCE(oi.status, 'active') = 'merged'
        ORDER BY sim_score DESC, oi.object_name
        LIMIT %s
        """
        
        with connection.cursor() as cursor:
            cursor.execute(
                merged_variant_sql,
                (
                    name_query,  # for similarity()
                    class_name,  # WHERE class_name check 1
                    class_name,  # WHERE class_name check 2
                    limit,  # LIMIT
                ),
            )
            merged_rows = cursor.fetchall()
        
        # Filter by similarity threshold (same as in active objects search)
        for row in merged_rows:
            similarity = float(row[6])
            if similarity < 0.12:  # min_similarity threshold
                continue
            merged_variant = {
                "object_id": row[0],
                "object_name": row[1],
                "canonical_full_name": row[2],
                "class_name": row[3],
                "metadata": row[4] or {},
                "status": row[5],
                "similarity": similarity,
                "name_rank": 4,  # merged variants get lower rank
                "attribute_matches": 0,
                "score": 0,
            }
            # Mark that this will be resolved
            merged_variant["_is_merged_variant"] = True
            candidates.append(merged_variant)

    # Resolve merged objects to their canonical forms for transparency
    resolved_candidates: dict[int, dict[str, Any]] = {}
    for candidate in candidates:
        original_object_id = int(candidate["object_id"])
        canonical_object_id = resolve_merged_object_to_canonical(connection, original_object_id)
        
        if canonical_object_id != original_object_id:
            # Object was merged, fetch the canonical object details
            canonical_row = get_object_instance_by_id(
                connection,
                canonical_object_id,
                as_of_date=as_of_date,
                include_inactive=include_inactive,
            )
            if canonical_row:
                # Replace with canonical object, but preserve original similarity score
                resolved_candidate = {
                    "object_id": canonical_row["object_id"],
                    "object_name": canonical_row["object_name"],
                    "canonical_full_name": canonical_row["canonical_full_name"],
                    "class_name": canonical_row["class_name"],
                    "metadata": canonical_row["metadata"],
                    "status": canonical_row["status"],
                    "similarity": candidate.get("similarity", 0.0),
                    "name_rank": candidate.get("name_rank", 4),
                    "attribute_matches": candidate.get("attribute_matches", 0),
                    "score": candidate.get("score", 0),
                    "resolved_from_merged": original_object_id,  # Track what we resolved from
                }
                resolved_candidates[canonical_object_id] = resolved_candidate
            else:
                # Canonical object not found, keep original
                resolved_candidates[original_object_id] = candidate
        else:
            # Not merged, keep as-is
            resolved_candidates[original_object_id] = candidate
    
    # Return deduplicated list (in case multiple merged variants resolved to same canonical)
    return list(resolved_candidates.values())


def search_documents_by_key(
    connection: psycopg2.extensions.connection,
    key_query: str,
    doc_cat: str | None = None,
    doc_type: str | None = None,
    limit: int = 20,
    as_of_date: date | None = None,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    """Search documents by key and user metadata with weighted lexical ranking.

    Ranking preference: key exact/prefix > uploader description/tags > keyword text > trigram similarity.
    """
    if not key_query.strip():
        return []

    sql = """
    SELECT
        d.doc_id,
        d.doc_key,
        d.doc_path,
        d.doc_cat,
        d.doc_type,
        d.doc_date,
        d.identifiers_kv,
        d.metadata,
        similarity(LOWER(COALESCE(d.doc_key, '')), LOWER(%s)) AS sim_score,
        similarity(LOWER(COALESCE(d.keyword_text, '')), LOWER(%s)) AS keyword_sim_score,
        CASE
            WHEN LOWER(COALESCE(d.doc_key, '')) = LOWER(%s) THEN 1
            WHEN LOWER(COALESCE(d.doc_key, '')) LIKE LOWER(%s) || '%%' THEN 2
            WHEN LOWER(COALESCE(d.doc_key, '')) LIKE '%%' || LOWER(%s) || '%%' THEN 3
            ELSE 4
        END AS key_rank,
        (
            CASE
                WHEN LOWER(COALESCE(d.doc_key, '')) = LOWER(%s) THEN 35
                WHEN LOWER(COALESCE(d.doc_key, '')) LIKE LOWER(%s) || '%%' THEN 24
                WHEN LOWER(COALESCE(d.doc_key, '')) LIKE '%%' || LOWER(%s) || '%%' THEN 14
                ELSE 0
            END
            + CASE WHEN LOWER(COALESCE(d.metadata->>'user_description', '')) LIKE '%%' || LOWER(%s) || '%%' THEN 12 ELSE 0 END
            + CASE WHEN LOWER(COALESCE(d.metadata->>'user_tags', '')) LIKE '%%' || LOWER(%s) || '%%' THEN 10 ELSE 0 END
            + CASE WHEN LOWER(COALESCE(d.keyword_text, '')) LIKE '%%' || LOWER(%s) || '%%' THEN 8 ELSE 0 END
        ) AS lexical_boost
    FROM document d
    WHERE (%s IS NULL OR d.doc_cat = %s)
      AND (%s IS NULL OR d.doc_type = %s)
            AND (%s OR COALESCE(d.status, 'active') = 'active')
            AND (d.valid_from <= %s)
            AND (d.valid_until IS NULL OR d.valid_until > %s)
      AND (
            LOWER(COALESCE(d.doc_key, '')) LIKE '%%' || LOWER(%s) || '%%'
            OR LOWER(COALESCE(d.doc_key, '')) %% LOWER(%s)
            OR LOWER(COALESCE(d.metadata->>'user_description', '')) LIKE '%%' || LOWER(%s) || '%%'
            OR LOWER(COALESCE(d.metadata->>'user_tags', '')) LIKE '%%' || LOWER(%s) || '%%'
            OR LOWER(COALESCE(d.keyword_text, '')) LIKE '%%' || LOWER(%s) || '%%'
            OR LOWER(COALESCE(d.keyword_text, '')) %% LOWER(%s)
          )
    ORDER BY lexical_boost DESC, key_rank, sim_score DESC, keyword_sim_score DESC, d.doc_key
    LIMIT %s
    """

    with connection.cursor() as cursor:
        effective_date = as_of_date or date.today()
        cursor.execute(
            sql,
            (
                key_query,
                key_query,
                key_query,
                key_query,
                key_query,
                key_query,
                key_query,
                key_query,
                key_query,
                key_query,
                doc_cat,
                doc_cat,
                doc_type,
                doc_type,
                include_inactive,
                effective_date,
                effective_date,
                key_query,
                key_query,
                key_query,
                key_query,
                key_query,
                key_query,
                limit,
            ),
        )
        rows = cursor.fetchall()

    return [
        {
            "doc_id": row[0],
            "doc_key": row[1],
            "doc_path": row[2],
            "doc_cat": row[3],
            "doc_type": row[4],
            "doc_date": row[5],
            "identifiers_kv": row[6] or {},
            "metadata": row[7] or {},
            "similarity": float(row[8]),
            "keyword_similarity": float(row[9]),
            "key_rank": int(row[10]),
            "lexical_boost": int(row[11]),
            "score": int(row[11]) + (float(row[8]) * 100.0) + (float(row[9]) * 25.0) - int(row[10]),
        }
        for row in rows
    ]


def get_document_brief_by_id(
    connection: psycopg2.extensions.connection,
    doc_id: int,
) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT doc_id, doc_key, doc_cat, doc_type, doc_date, doc_desc, doc_theme,
                   keyword_text, metadata, status, valid_from, valid_until, entry_date
            FROM document
            WHERE doc_id = %s
            """,
            (int(doc_id),),
        )
        row = cursor.fetchone()

    if row is None:
        return None

    return {
        "doc_id": int(row[0]),
        "doc_key": row[1],
        "doc_cat": row[2],
        "doc_type": row[3],
        "doc_date": row[4],
        "doc_desc": row[5],
        "doc_theme": row[6],
        "keyword_text": row[7],
        "metadata": row[8] or {},
        "status": row[9],
        "valid_from": row[10],
        "valid_until": row[11],
        "entry_date": row[12],
    }


def upsert_document_table_cells(
    connection: psycopg2.extensions.connection,
    doc_id: int,
    tables: list[dict[str, Any]] | None,
    source_kind: str = "document",
    replace_existing: bool = True,
) -> int:
    with connection.cursor() as cursor:
        cursor.execute(create_document_table_cell_table)
    normalized_tables = [table for table in (tables or []) if isinstance(table, dict)]
    if replace_existing:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM document_table_cell WHERE doc_id = %s", (int(doc_id),))

    inserted = 0
    with connection.cursor() as cursor:
        for table_idx, table in enumerate(normalized_tables):
            headers = [str(value or "").strip() for value in list(table.get("headers") or [])]
            rows = table.get("rows") if isinstance(table.get("rows"), list) else []
            records = table.get("records") if isinstance(table.get("records"), list) else []

            if not rows and headers and records:
                rebuilt_rows: list[list[str]] = []
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    rebuilt_rows.append([str(record.get(header) or "").strip() for header in headers])
                rows = rebuilt_rows

            if not rows:
                continue

            for row_idx, row in enumerate(rows):
                if not isinstance(row, list):
                    continue
                row_label = str(row[0] or "").strip() if row else ""
                for col_idx, value in enumerate(row):
                    cell_value = str(value or "").strip()
                    if not cell_value:
                        continue
                    column_label = str(headers[col_idx] if col_idx < len(headers) else f"col_{col_idx + 1}").strip()
                    metadata = {
                        "table_index": int(table_idx),
                        "row_index": int(row_idx),
                        "column_index": int(col_idx),
                        "source_kind": str(source_kind or "document").strip().lower() or "document",
                    }
                    cursor.execute(
                        """
                        INSERT INTO document_table_cell (
                            doc_id, table_index, row_index, row_label,
                            column_index, column_label, cell_value, source_kind, metadata
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (doc_id, table_index, row_index, column_index)
                        DO UPDATE SET
                            row_label = EXCLUDED.row_label,
                            column_label = EXCLUDED.column_label,
                            cell_value = EXCLUDED.cell_value,
                            source_kind = EXCLUDED.source_kind,
                            metadata = EXCLUDED.metadata
                        """,
                        (
                            int(doc_id),
                            int(table_idx),
                            int(row_idx),
                            row_label or None,
                            int(col_idx),
                            column_label or None,
                            cell_value,
                            metadata["source_kind"],
                            json.dumps(metadata),
                        ),
                    )
                    inserted += 1

    connection.commit()
    return inserted


def list_document_table_cells(
    connection: psycopg2.extensions.connection,
    doc_id: int,
    limit: int = 200,
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(create_document_table_cell_table)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT cell_id, doc_id, table_index, row_index, row_label,
                   column_index, column_label, cell_value, source_kind, metadata, created_at
            FROM document_table_cell
            WHERE doc_id = %s
            ORDER BY table_index, row_index, column_index
            LIMIT %s
            """,
            (int(doc_id), int(limit)),
        )
        rows = cursor.fetchall() or []

    return [
        {
            "cell_id": int(row[0]),
            "doc_id": int(row[1]),
            "table_index": int(row[2]),
            "row_index": int(row[3]),
            "row_label": row[4],
            "column_index": int(row[5]),
            "column_label": row[6],
            "cell_value": row[7],
            "source_kind": row[8],
            "metadata": row[9] or {},
            "created_at": row[10],
        }
        for row in rows
    ]


def backfill_document_table_cells_from_document_metadata(
    connection: psycopg2.extensions.connection,
    doc_id: int,
) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT metadata FROM document WHERE doc_id = %s",
            (int(doc_id),),
        )
        row = cursor.fetchone()

    if not row:
        return 0
    metadata = row[0] if isinstance(row[0], dict) else {}
    structured_tables = metadata.get("structured_tables") if isinstance(metadata.get("structured_tables"), dict) else {}
    tables = structured_tables.get("tables") if isinstance(structured_tables.get("tables"), list) else []
    source_kind = str(structured_tables.get("source_kind") or "document").strip().lower() or "document"
    if not tables:
        return upsert_document_table_cells(connection, int(doc_id), [], source_kind=source_kind, replace_existing=True)
    return upsert_document_table_cells(connection, int(doc_id), tables, source_kind=source_kind, replace_existing=True)


def select_document_ids_for_table_provenance_backfill(
    connection: psycopg2.extensions.connection,
    doc_ids: list[int] | None = None,
    only_missing: bool = True,
    limit: int = 100,
) -> list[int]:
    with connection.cursor() as cursor:
        cursor.execute(create_document_table_cell_table)

    explicit_ids = [int(item) for item in (doc_ids or []) if int(item) > 0]
    if explicit_ids:
        return sorted(set(explicit_ids))

    with connection.cursor() as cursor:
        if only_missing:
            cursor.execute(
                """
                SELECT d.doc_id
                FROM document d
                WHERE CASE
                        WHEN jsonb_typeof(d.metadata->'structured_tables'->'tables') = 'array'
                        THEN jsonb_array_length(d.metadata->'structured_tables'->'tables')
                        ELSE 0
                      END > 0
                  AND NOT EXISTS (
                    SELECT 1 FROM document_table_cell c WHERE c.doc_id = d.doc_id
                  )
                ORDER BY d.doc_id DESC
                LIMIT %s
                """,
                (int(limit),),
            )
        else:
            cursor.execute(
                """
                SELECT d.doc_id
                FROM document d
                WHERE CASE
                        WHEN jsonb_typeof(d.metadata->'structured_tables'->'tables') = 'array'
                        THEN jsonb_array_length(d.metadata->'structured_tables'->'tables')
                        ELSE 0
                      END > 0
                ORDER BY d.doc_id DESC
                LIMIT %s
                """,
                (int(limit),),
            )
        rows = cursor.fetchall() or []
    return [int(row[0]) for row in rows if row and int(row[0]) > 0]


def _normalize_date_key(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return None
    if isinstance(value, (date, datetime)):
        return value.year * 10000 + value.month * 100 + value.day
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            return int(stripped)
        for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                parsed = datetime.strptime(stripped, pattern)
            except ValueError:
                continue
            return parsed.year * 10000 + parsed.month * 100 + parsed.day
    return None


def _extract_date_key(metadata: dict[str, Any] | None, candidate_fields: tuple[str, ...]) -> int | None:
    if not metadata:
        return None

    for field_name in candidate_fields:
        if field_name not in metadata:
            continue
        normalized = _normalize_date_key(metadata.get(field_name))
        if normalized is not None:
            return normalized
    return None


def _coerce_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                return datetime.strptime(text, pattern).date()
            except ValueError:
                continue
    return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _normalize_attr_type_name(attr_type: str) -> str:
    """Normalize attribute keys so semantically identical fields share one attr_type."""
    normalized = str(attr_type or "").strip().lower()
    aliases = {
        "zweck": "purpose",
        "gesellschaftszweck": "purpose",
        "unternehmenszweck": "purpose",
        "business_purpose": "purpose",
        "company_purpose": "purpose",
    }
    return aliases.get(normalized, normalized)


def upsert_temporal_attribute(
    connection: psycopg2.extensions.connection,
    src_id: int,
    src_type: str,
    attr_type: str,
    attr_json: Any,
    valid_from: date | str | None = None,
    valid_until: date | str | None = None,
    close_previous: bool = True,
) -> dict[str, Any] | None:
    normalized_src_type = str(src_type).strip().lower()
    normalized_attr_type = _normalize_attr_type_name(attr_type)
    if not normalized_src_type or not normalized_attr_type:
        return None

    if isinstance(attr_json, dict):
        payload = attr_json
    else:
        payload = {"value": attr_json}

    start_date = _coerce_date(valid_from) or date.today()
    end_date = _coerce_date(valid_until)

    select_sql = """
    SELECT attr_id, attr_json, valid_from, valid_until
    FROM attribute
    WHERE src_id = %s
      AND src_type = %s
      AND attr_type = %s
      AND (valid_until IS NULL OR valid_until > %s)
    ORDER BY valid_from DESC, attr_id DESC
    """

    update_sql = """
    UPDATE attribute
    SET valid_until = %s
    WHERE attr_id = %s
    """

    insert_sql = """
    INSERT INTO attribute (src_id, src_type, attr_type, valid_from, valid_until, attr_json, search_txt)
    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
    RETURNING attr_id, src_id, src_type, attr_type, valid_from, valid_until, attr_json, entry_date
    """

    with connection.cursor() as cursor:
        cursor.execute(select_sql, (src_id, normalized_src_type, normalized_attr_type, start_date))
        current_rows = cursor.fetchall() or []

        unchanged_row: dict[str, Any] | None = None
        for current in current_rows:
            current_json = current[1]
            current_start = current[2]
            current_end = current[3]
            if (
                _canonical_json(current_json) == _canonical_json(payload)
                and current_start == start_date
                and current_end == end_date
            ):
                unchanged_row = {
                    "attr_id": int(current[0]),
                    "src_id": int(src_id),
                    "src_type": normalized_src_type,
                    "attr_type": normalized_attr_type,
                    "valid_from": current_start,
                    "valid_until": current_end,
                    "attr_json": current_json,
                    "unchanged": True,
                }
                break

        if close_previous:
            for current in current_rows:
                if unchanged_row is not None and int(current[0]) == int(unchanged_row["attr_id"]):
                    continue
                current_start = current[2]
                current_end = current[3]
                if not (current_end is None or current_end > start_date):
                    continue

                # Prevent invalid ranges when backfilling older dates.
                # The closed upper bound must be >= the existing lower bound.
                close_at = start_date
                if current_start and close_at < current_start:
                    close_at = current_start
                if current_end is None or close_at < current_end:
                    cursor.execute(update_sql, (close_at, int(current[0])))

        if unchanged_row is not None:
            return unchanged_row

        search_txt = _canonical_json(payload)
        cursor.execute(
            insert_sql,
            (
                int(src_id),
                normalized_src_type,
                normalized_attr_type,
                start_date,
                end_date,
                _canonical_json(payload),
                search_txt,
            ),
        )
        row = cursor.fetchone()

    return {
        "attr_id": int(row[0]),
        "src_id": int(row[1]),
        "src_type": row[2],
        "attr_type": row[3],
        "valid_from": row[4],
        "valid_until": row[5],
        "attr_json": row[6] or {},
        "entry_date": row[7],
    }


def sync_object_temporal_attributes(
    connection: psycopg2.extensions.connection,
    object_id: int,
    attributes: dict[str, Any],
    valid_from: date | str | None = None,
    valid_until: date | str | None = None,
    close_previous: bool = True,
    commit: bool = True,
) -> list[dict[str, Any]]:
    return sync_temporal_attributes(
        connection=connection,
        src_id=int(object_id),
        src_type="object",
        attributes=attributes,
        valid_from=valid_from,
        valid_until=valid_until,
        close_previous=close_previous,
        commit=commit,
    )


def sync_temporal_attributes(
    connection: psycopg2.extensions.connection,
    src_id: int,
    src_type: str,
    attributes: dict[str, Any],
    valid_from: date | str | None = None,
    valid_until: date | str | None = None,
    close_previous: bool = True,
    commit: bool = True,
) -> list[dict[str, Any]]:
    if not attributes:
        return []

    rows: list[dict[str, Any]] = []
    for attr_name, attr_value in attributes.items():
        if not str(attr_name).strip():
            continue
        row = upsert_temporal_attribute(
            connection=connection,
            src_id=int(src_id),
            src_type=src_type,
            attr_type=str(attr_name),
            attr_json=attr_value,
            valid_from=valid_from,
            valid_until=valid_until,
            close_previous=close_previous,
        )
        if row is not None:
            rows.append(row)

    if commit and rows:
        connection.commit()

    return rows


def get_object_relationship(
    connection: psycopg2.extensions.connection,
    relationship_name: str,
    relationship_cat: str,
    src_object_id: int,
    tar_object_id: int,
) -> dict[str, Any] | None:
    sql = """
    SELECT relationship_id, relationship_name, relationship_cat, src_object_id, tar_object_id, confidence, metadata, entry_date
    FROM object_relationship
    WHERE relationship_name = %s
      AND relationship_cat = %s
      AND src_object_id = %s
      AND tar_object_id = %s
    LIMIT 1
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, (relationship_name, relationship_cat, src_object_id, tar_object_id))
        row = cursor.fetchone()

    if not row:
        return None

    return {
        "relationship_id": int(row[0]),
        "relationship_name": row[1],
        "relationship_cat": row[2],
        "src_object_id": int(row[3]),
        "tar_object_id": int(row[4]),
        "confidence": row[5],
        "metadata": row[6] or {},
        "entry_date": row[7],
    }


def _find_conflicting_relationship_ids(
    connection: psycopg2.extensions.connection,
    relationship_name: str,
    relationship_cat: str,
    src_object_id: int,
    tar_object_id: int,
    exclusivity_scope: str,
    exclude_relationship_id: int,
) -> list[int]:
    scope = str(exclusivity_scope).strip().lower()
    if scope not in {"by_target", "by_source"}:
        return []

    filters = [
        "relationship_name = %s",
        "relationship_cat = %s",
        "relationship_id <> %s",
    ]
    params: list[Any] = [relationship_name, relationship_cat, exclude_relationship_id]

    if scope == "by_target":
        filters.append("tar_object_id = %s")
        params.append(tar_object_id)
    else:
        filters.append("src_object_id = %s")
        params.append(src_object_id)

    sql = f"""
    SELECT relationship_id
    FROM object_relationship
    WHERE {' AND '.join(filters)}
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
    return [int(row[0]) for row in rows]


def deactivate_object_relationship(
    connection: psycopg2.extensions.connection,
    relationship_name: str,
    relationship_cat: str,
    src_object_id: int,
    tar_object_id: int,
    effective_from: date | str | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    existing = get_object_relationship(
        connection=connection,
        relationship_name=relationship_name,
        relationship_cat=relationship_cat,
        src_object_id=src_object_id,
        tar_object_id=tar_object_id,
    )
    if not existing:
        return {
            "deleted": False,
            "relationship_name": relationship_name,
            "relationship_cat": relationship_cat,
            "src_object_id": int(src_object_id),
            "tar_object_id": int(tar_object_id),
        }

    attrs = {"active": False}
    if metadata:
        attrs.update(metadata)

    sync_temporal_attributes(
        connection=connection,
        src_id=int(existing["relationship_id"]),
        src_type="relationship",
        attributes=attrs,
        valid_from=effective_from,
        valid_until=None,
        close_previous=True,
        commit=False,
    )

    if commit:
        connection.commit()

    return {
        "deleted": True,
        "relationship_id": int(existing["relationship_id"]),
        "relationship_name": relationship_name,
        "relationship_cat": relationship_cat,
        "src_object_id": int(src_object_id),
        "tar_object_id": int(tar_object_id),
    }


def upsert_temporal_relationship(
    connection: psycopg2.extensions.connection,
    relationship_name: str,
    relationship_cat: str,
    src_object_id: int,
    tar_object_id: int,
    confidence: float | None = None,
    metadata: dict[str, Any] | None = None,
    temporal_attributes: dict[str, Any] | None = None,
    effective_from: date | str | None = None,
    effective_until: date | str | None = None,
    exclusivity_scope: str | None = None,
) -> dict[str, Any]:
    relationship = upsert_object_relationship(
        connection=connection,
        relationship_name=relationship_name,
        relationship_cat=relationship_cat,
        src_object_id=src_object_id,
        tar_object_id=tar_object_id,
        confidence=confidence,
        metadata=metadata,
    )

    rel_attrs = dict(temporal_attributes or {})
    rel_attrs["active"] = True

    temporal_rows = sync_temporal_attributes(
        connection=connection,
        src_id=int(relationship["relationship_id"]),
        src_type="relationship",
        attributes=rel_attrs,
        valid_from=effective_from,
        valid_until=effective_until,
        close_previous=True,
        commit=False,
    )

    deactivated = 0
    if exclusivity_scope in {"by_target", "by_source"}:
        conflicts = _find_conflicting_relationship_ids(
            connection=connection,
            relationship_name=relationship_name,
            relationship_cat=relationship_cat,
            src_object_id=src_object_id,
            tar_object_id=tar_object_id,
            exclusivity_scope=exclusivity_scope,
            exclude_relationship_id=int(relationship["relationship_id"]),
        )
        for conflict_id in conflicts:
            sync_temporal_attributes(
                connection=connection,
                src_id=conflict_id,
                src_type="relationship",
                attributes={"active": False, "deactivation_reason": f"exclusivity:{exclusivity_scope}"},
                valid_from=effective_from,
                valid_until=None,
                close_previous=True,
                commit=False,
            )
            deactivated += 1

    connection.commit()

    relationship["temporal_attributes_written"] = len(temporal_rows)
    relationship["deactivated_conflicts"] = deactivated
    return relationship


def get_relationships(
    connection: psycopg2.extensions.connection,
    relationship_name: str | None = None,
    relationship_cat: str | None = None,
    src_object_name: str | None = None,
    src_class_name: str | None = None,
    tar_object_name: str | None = None,
    tar_class_name: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sql = """
    SELECT
        rel.relationship_id,
        rel.relationship_name,
        rel.relationship_cat,
        rel.confidence,
        rel.metadata,
        rel.entry_date,
        src.object_id,
        src.object_name,
        src.class_name,
        src.metadata,
        tar.object_id,
        tar.object_name,
        tar.class_name,
        tar.metadata
    FROM object_relationship rel
    JOIN object_instance src ON src.object_id = rel.src_object_id
    JOIN object_instance tar ON tar.object_id = rel.tar_object_id
    WHERE (%s IS NULL OR rel.relationship_name = %s)
      AND (%s IS NULL OR rel.relationship_cat = %s)
      AND (%s IS NULL OR LOWER(src.object_name) = LOWER(%s))
      AND (%s IS NULL OR src.class_name = %s)
      AND (%s IS NULL OR LOWER(tar.object_name) = LOWER(%s))
      AND (%s IS NULL OR tar.class_name = %s)
    ORDER BY rel.entry_date DESC, rel.relationship_id DESC
    LIMIT %s
    """

    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                relationship_name,
                relationship_name,
                relationship_cat,
                relationship_cat,
                src_object_name,
                src_object_name,
                src_class_name,
                src_class_name,
                tar_object_name,
                tar_object_name,
                tar_class_name,
                tar_class_name,
                limit,
            ),
        )
        rows = cursor.fetchall()

    return [
        {
            "relationship_id": row[0],
            "relationship_name": row[1],
            "relationship_cat": row[2],
            "confidence": row[3],
            "relationship_metadata": row[4] or {},
            "entry_date": row[5],
            "src_object_id": row[6],
            "src_object_name": row[7],
            "src_class_name": row[8],
            "src_metadata": row[9] or {},
            "tar_object_id": row[10],
            "tar_object_name": row[11],
            "tar_class_name": row[12],
            "tar_metadata": row[13] or {},
        }
        for row in rows
    ]


def upsert_semantic_terms(
    connection: psycopg2.extensions.connection,
    terms: list[dict[str, Any]],
) -> int:
    """Upsert semantic terms that map user wording to canonical schema names."""
    if not terms:
        return 0

    rows: list[tuple[str, str, str, str, str, Any]] = []
    allowed_source_types = {"seeded", "ingest", "solf_clause", "manual", "llm_generated"}
    source_type_aliases = {
        "ingest_llm_alias": "llm_generated",
    }
    for item in terms:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        canonical_name = str(item.get("canonical_name") or "").strip().lower()
        term_text = str(item.get("term_text") or "").strip()
        language = str(item.get("language") or "und").strip().lower() or "und"
        raw_source_type = str(item.get("source_type") or "ingest").strip().lower() or "ingest"
        source_type = source_type_aliases.get(raw_source_type, raw_source_type)
        if source_type not in allowed_source_types:
            source_type = "ingest"
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}

        if kind not in {"attribute", "relationship"}:
            continue
        if not canonical_name or not term_text:
            continue

        rows.append((kind, canonical_name, term_text, language, source_type, Json(metadata)))

    if not rows:
        return 0

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO semantic_terms (kind, canonical_name, term_text, language, source_type, metadata)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (kind, canonical_name, term_text, language)
            DO UPDATE SET
                source_type = EXCLUDED.source_type,
                metadata = COALESCE(semantic_terms.metadata, '{}'::jsonb) || COALESCE(EXCLUDED.metadata, '{}'::jsonb),
                modified_at = NOW()
            """,
            rows,
        )
    connection.commit()
    return len(rows)


def get_semantic_terms(
    connection: psycopg2.extensions.connection,
    kind: str | None = None,
    canonical_name: str | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Fetch semantic term mappings from DB."""
    kind_norm = str(kind).strip().lower() if kind is not None else None
    canonical_norm = str(canonical_name).strip().lower() if canonical_name is not None else None

    sql = """
    SELECT kind, canonical_name, term_text, language, source_type, metadata
    FROM semantic_terms
    WHERE (%s IS NULL OR kind = %s)
      AND (%s IS NULL OR canonical_name = %s)
    ORDER BY canonical_name, term_text
    LIMIT %s
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, (kind_norm, kind_norm, canonical_norm, canonical_norm, int(limit)))
        rows = cursor.fetchall()

    return [
        {
            "kind": row[0],
            "canonical_name": row[1],
            "term_text": row[2],
            "language": row[3],
            "source_type": row[4],
            "metadata": row[5] or {},
        }
        for row in rows
    ]


def upsert_business_rule(
    connection: psycopg2.extensions.connection,
    rule_name: str,
    rule_text: str,
    structured_rule: dict[str, Any] | None = None,
    solf_script: str | None = None,
    scope: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    is_active: bool = True,
    created_by: str | None = None,
) -> int:
    """Insert a new business rule row."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO business_rules (
                rule_name, rule_text, structured_rule, solf_script, scope, metadata,
                is_active, created_by, created_at, modified_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            RETURNING rule_id
            """,
            (
                str(rule_name or "").strip() or "business_rule",
                str(rule_text or "").strip(),
                Json(structured_rule or {}),
                str(solf_script or ""),
                Json(scope or {}),
                Json(metadata or {}),
                bool(is_active),
                str(created_by or "").strip() or None,
            ),
        )
        row = cursor.fetchone()
    connection.commit()
    return int(row[0])


def set_business_rule_active(
    connection: psycopg2.extensions.connection,
    rule_id: int,
    is_active: bool,
) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE business_rules
            SET is_active = %s,
                modified_at = NOW()
            WHERE rule_id = %s
            """,
            (bool(is_active), int(rule_id)),
        )
        updated = int(cursor.rowcount or 0)
    connection.commit()
    return updated > 0


def get_business_rules(
    connection: psycopg2.extensions.connection,
    is_active: bool | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                rule_id,
                rule_name,
                rule_text,
                structured_rule,
                solf_script,
                scope,
                metadata,
                is_active,
                created_by,
                created_at,
                modified_at
            FROM business_rules
            WHERE (%s IS NULL OR is_active = %s)
            ORDER BY modified_at DESC, rule_id DESC
            LIMIT %s
            """,
            (is_active, is_active, int(limit)),
        )
        rows = cursor.fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "rule_id": row[0],
                "rule_name": row[1],
                "rule_text": row[2],
                "structured_rule": row[3] or {},
                "solf_script": row[4] or "",
                "scope": row[5] or {},
                "metadata": row[6] or {},
                "is_active": bool(row[7]),
                "created_by": row[8],
                "created_at": row[9],
                "modified_at": row[10],
            }
        )
    return out


def get_active_business_rules_by_names(
    connection: psycopg2.extensions.connection,
    rule_names: list[str],
    limit: int = 100,
) -> list[dict[str, Any]]:
    normalized_names = []
    for name in rule_names or []:
        token = str(name or "").strip().lower()
        if token and token not in normalized_names:
            normalized_names.append(token)

    if not normalized_names:
        return []

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                rule_id,
                rule_name,
                rule_text,
                structured_rule,
                solf_script,
                scope,
                metadata,
                is_active,
                created_by,
                created_at,
                modified_at
            FROM business_rules
            WHERE is_active = TRUE
              AND LOWER(TRIM(rule_name)) = ANY(%s)
            ORDER BY modified_at DESC, rule_id DESC
            LIMIT %s
            """,
            (normalized_names, int(limit)),
        )
        rows = cursor.fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "rule_id": row[0],
                "rule_name": row[1],
                "rule_text": row[2],
                "structured_rule": row[3] or {},
                "solf_script": row[4] or "",
                "scope": row[5] or {},
                "metadata": row[6] or {},
                "is_active": bool(row[7]),
                "created_by": row[8],
                "created_at": row[9],
                "modified_at": row[10],
            }
        )
    return out


def get_business_rule_by_id(
    connection: psycopg2.extensions.connection,
    rule_id: int,
) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                rule_id,
                rule_name,
                rule_text,
                structured_rule,
                solf_script,
                scope,
                metadata,
                is_active,
                created_by,
                created_at,
                modified_at
            FROM business_rules
            WHERE rule_id = %s
            LIMIT 1
            """,
            (int(rule_id),),
        )
        row = cursor.fetchone()

    if row is None:
        return None

    return {
        "rule_id": row[0],
        "rule_name": row[1],
        "rule_text": row[2],
        "structured_rule": row[3] or {},
        "solf_script": row[4] or "",
        "scope": row[5] or {},
        "metadata": row[6] or {},
        "is_active": bool(row[7]),
        "created_by": row[8],
        "created_at": row[9],
        "modified_at": row[10],
    }


def update_business_rule(
    connection: psycopg2.extensions.connection,
    rule_id: int,
    rule_name: str,
    rule_text: str,
    structured_rule: dict[str, Any] | None = None,
    solf_script: str | None = None,
    scope: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    is_active: bool = True,
) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE business_rules
            SET rule_name = %s,
                rule_text = %s,
                structured_rule = %s,
                solf_script = %s,
                scope = %s,
                metadata = %s,
                is_active = %s,
                modified_at = NOW()
            WHERE rule_id = %s
            """,
            (
                str(rule_name or "").strip() or "business_rule",
                str(rule_text or "").strip(),
                Json(structured_rule or {}),
                str(solf_script or ""),
                Json(scope or {}),
                Json(metadata or {}),
                bool(is_active),
                int(rule_id),
            ),
        )
        updated = int(cursor.rowcount or 0)
    connection.commit()
    return updated > 0


def upsert_solf_workflow_registry(
    connection: psycopg2.extensions.connection,
    workflow_key: str,
    workflow_name: str,
    description: str | None = None,
    domain: str | None = None,
    status: str = "draft",
    metadata: dict[str, Any] | None = None,
    is_active: bool = True,
    created_by: str | None = None,
) -> int:
    key = str(workflow_key or "").strip().lower()
    if not key:
        raise ValueError("workflow_key is required")

    valid_statuses = {"draft", "review", "published", "deprecated", "archived"}
    normalized_status = str(status or "draft").strip().lower()
    if normalized_status not in valid_statuses:
        normalized_status = "draft"

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO solf_workflow_registry (
                workflow_key, workflow_name, description, domain, status, metadata,
                is_active, created_by, created_at, modified_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (workflow_key)
            DO UPDATE SET
                workflow_name = EXCLUDED.workflow_name,
                description = EXCLUDED.description,
                domain = EXCLUDED.domain,
                status = EXCLUDED.status,
                metadata = COALESCE(solf_workflow_registry.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                is_active = EXCLUDED.is_active,
                modified_at = NOW()
            RETURNING workflow_id
            """,
            (
                key,
                str(workflow_name or key).strip(),
                str(description or "").strip(),
                str(domain or "").strip() or None,
                normalized_status,
                Json(metadata or {}),
                bool(is_active),
                str(created_by or "").strip() or None,
            ),
        )
        row = cursor.fetchone()
    connection.commit()
    return int(row[0])


def list_solf_workflow_registry(
    connection: psycopg2.extensions.connection,
    is_active: bool | None = None,
    domain: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT workflow_id, workflow_key, workflow_name, description, domain, status,
                   metadata, is_active, created_by, created_at, modified_at
            FROM solf_workflow_registry
            WHERE (%s IS NULL OR is_active = %s)
              AND (%s IS NULL OR domain = %s)
              AND (%s IS NULL OR status = %s)
            ORDER BY modified_at DESC, workflow_id DESC
            LIMIT %s
            """,
            (
                is_active,
                is_active,
                str(domain or "").strip() or None,
                str(domain or "").strip() or None,
                str(status or "").strip().lower() or None,
                str(status or "").strip().lower() or None,
                int(limit),
            ),
        )
        rows = cursor.fetchall()

    return [
        {
            "workflow_id": int(row[0]),
            "workflow_key": row[1],
            "workflow_name": row[2],
            "description": row[3] or "",
            "domain": row[4],
            "status": row[5],
            "metadata": row[6] or {},
            "is_active": bool(row[7]),
            "created_by": row[8],
            "created_at": row[9],
            "modified_at": row[10],
        }
        for row in rows
    ]


def get_solf_workflow_registry_by_id(
    connection: psycopg2.extensions.connection,
    workflow_id: int,
) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT workflow_id, workflow_key, workflow_name, description, domain, status,
                   metadata, is_active, created_by, created_at, modified_at
            FROM solf_workflow_registry
            WHERE workflow_id = %s
            LIMIT 1
            """,
            (int(workflow_id),),
        )
        row = cursor.fetchone()

    if row is None:
        return None

    return {
        "workflow_id": int(row[0]),
        "workflow_key": row[1],
        "workflow_name": row[2],
        "description": row[3] or "",
        "domain": row[4],
        "status": row[5],
        "metadata": row[6] or {},
        "is_active": bool(row[7]),
        "created_by": row[8],
        "created_at": row[9],
        "modified_at": row[10],
    }


def get_solf_workflow_registry_by_key(
    connection: psycopg2.extensions.connection,
    workflow_key: str,
) -> dict[str, Any] | None:
    normalized_key = str(workflow_key or "").strip()
    if not normalized_key:
        return None

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT workflow_id, workflow_key, workflow_name, description, domain, status,
                   metadata, is_active, created_by, created_at, modified_at
            FROM solf_workflow_registry
            WHERE workflow_key = %s
            LIMIT 1
            """,
            (normalized_key,),
        )
        row = cursor.fetchone()

    if row is None:
        return None

    return {
        "workflow_id": int(row[0]),
        "workflow_key": row[1],
        "workflow_name": row[2],
        "description": row[3] or "",
        "domain": row[4],
        "status": row[5],
        "metadata": row[6] or {},
        "is_active": bool(row[7]),
        "created_by": row[8],
        "created_at": row[9],
        "modified_at": row[10],
    }


def set_solf_workflow_registry_active(
    connection: psycopg2.extensions.connection,
    workflow_id: int,
    is_active: bool,
) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE solf_workflow_registry
            SET is_active = %s,
                modified_at = NOW()
            WHERE workflow_id = %s
            """,
            (bool(is_active), int(workflow_id)),
        )
        updated = int(cursor.rowcount or 0)
    connection.commit()
    return updated > 0


def create_solf_workflow_version(
    connection: psycopg2.extensions.connection,
    workflow_id: int,
    rule_id: int | None = None,
    graph_spec: dict[str, Any] | None = None,
    input_contract: dict[str, Any] | None = None,
    output_contract: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    is_active: bool = True,
    created_by: str | None = None,
) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM solf_workflow_versions WHERE workflow_id = %s",
            (int(workflow_id),),
        )
        next_version_no = int((cursor.fetchone() or [1])[0] or 1)

        cursor.execute(
            """
            INSERT INTO solf_workflow_versions (
                workflow_id, version_no, rule_id, graph_spec, input_contract,
                output_contract, metadata, is_active, created_by, created_at, modified_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            RETURNING workflow_version_id
            """,
            (
                int(workflow_id),
                next_version_no,
                int(rule_id) if rule_id is not None else None,
                Json(graph_spec or {}),
                Json(input_contract or {}),
                Json(output_contract or {}),
                Json(metadata or {}),
                bool(is_active),
                str(created_by or "").strip() or None,
            ),
        )
        row = cursor.fetchone()
    connection.commit()
    return int(row[0])


def list_solf_workflow_versions(
    connection: psycopg2.extensions.connection,
    workflow_id: int,
    is_active: bool | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT workflow_version_id, workflow_id, version_no, rule_id, graph_spec,
                   input_contract, output_contract, metadata, is_active, created_by,
                   created_at, modified_at
            FROM solf_workflow_versions
            WHERE workflow_id = %s
              AND (%s IS NULL OR is_active = %s)
            ORDER BY version_no DESC
            LIMIT %s
            """,
            (int(workflow_id), is_active, is_active, int(limit)),
        )
        rows = cursor.fetchall()

    return [
        {
            "workflow_version_id": int(row[0]),
            "workflow_id": int(row[1]),
            "version_no": int(row[2]),
            "rule_id": row[3],
            "graph_spec": row[4] or {},
            "input_contract": row[5] or {},
            "output_contract": row[6] or {},
            "metadata": row[7] or {},
            "is_active": bool(row[8]),
            "created_by": row[9],
            "created_at": row[10],
            "modified_at": row[11],
        }
        for row in rows
    ]


def replace_solf_workflow_steps(
    connection: psycopg2.extensions.connection,
    workflow_version_id: int,
    steps: list[dict[str, Any]],
) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM solf_workflow_steps WHERE workflow_version_id = %s",
            (int(workflow_version_id),),
        )

        inserted = 0
        for index, step in enumerate(steps or [], start=1):
            step_key = str(step.get("step_key") or f"step_{index}").strip().lower()
            if not step_key:
                step_key = f"step_{index}"
            step_kind = str(step.get("step_kind") or "clause").strip().lower()
            if step_kind not in {"clause", "class_transform", "class_generate", "class_iterate", "python_binding"}:
                step_kind = "clause"

            cursor.execute(
                """
                INSERT INTO solf_workflow_steps (
                    workflow_version_id, step_order, step_key, step_kind, clause_id,
                    clause_name, input_class, output_class, operation, python_module,
                    python_function, config, created_at, modified_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                """,
                (
                    int(workflow_version_id),
                    int(step.get("step_order") or index),
                    step_key,
                    step_kind,
                    int(step.get("clause_id")) if step.get("clause_id") is not None else None,
                    str(step.get("clause_name") or "").strip() or None,
                    str(step.get("input_class") or "").strip() or None,
                    str(step.get("output_class") or "").strip() or None,
                    str(step.get("operation") or "").strip() or None,
                    str(step.get("python_module") or "").strip() or None,
                    str(step.get("python_function") or "").strip() or None,
                    Json(step.get("config") if isinstance(step.get("config"), dict) else {}),
                ),
            )
            inserted += 1

    connection.commit()
    return inserted


def list_solf_workflow_steps(
    connection: psycopg2.extensions.connection,
    workflow_version_id: int,
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT step_id, workflow_version_id, step_order, step_key, step_kind,
                   clause_id, clause_name, input_class, output_class, operation,
                   python_module, python_function, config, created_at, modified_at
            FROM solf_workflow_steps
            WHERE workflow_version_id = %s
            ORDER BY step_order ASC, step_id ASC
            """,
            (int(workflow_version_id),),
        )
        rows = cursor.fetchall()

    return [
        {
            "step_id": int(row[0]),
            "workflow_version_id": int(row[1]),
            "step_order": int(row[2]),
            "step_key": row[3],
            "step_kind": row[4],
            "clause_id": row[5],
            "clause_name": row[6],
            "input_class": row[7],
            "output_class": row[8],
            "operation": row[9],
            "python_module": row[10],
            "python_function": row[11],
            "config": row[12] or {},
            "created_at": row[13],
            "modified_at": row[14],
        }
        for row in rows
    ]


def upsert_workflow_extension_pack(
    connection: psycopg2.extensions.connection,
    extension_key: str,
    extension_name: str,
    description: str | None = None,
    scope_json: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    is_active: bool = True,
    created_by: str | None = None,
) -> int:
    key = str(extension_key or "").strip().lower()
    if not key:
        raise ValueError("extension_key is required")

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workflow_extension_pack (
                extension_key, extension_name, description, scope_json, metadata,
                is_active, created_by, created_at, modified_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (extension_key)
            DO UPDATE SET
                extension_name = EXCLUDED.extension_name,
                description = EXCLUDED.description,
                scope_json = COALESCE(workflow_extension_pack.scope_json, '{}'::jsonb) || EXCLUDED.scope_json,
                metadata = COALESCE(workflow_extension_pack.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                is_active = EXCLUDED.is_active,
                modified_at = NOW()
            RETURNING extension_pack_id
            """,
            (
                key,
                str(extension_name or key).strip(),
                str(description or "").strip(),
                Json(scope_json or {}),
                Json(metadata or {}),
                bool(is_active),
                str(created_by or "").strip() or None,
            ),
        )
        row = cursor.fetchone()
    connection.commit()
    return int(row[0])


def get_workflow_extension_pack_by_key(
    connection: psycopg2.extensions.connection,
    extension_key: str,
) -> dict[str, Any] | None:
    key = str(extension_key or "").strip().lower()
    if not key:
        return None

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT extension_pack_id, extension_key, extension_name, description,
                   scope_json, metadata, is_active, created_by, created_at, modified_at
            FROM workflow_extension_pack
            WHERE extension_key = %s
            LIMIT 1
            """,
            (key,),
        )
        row = cursor.fetchone()

    if row is None:
        return None

    return {
        "extension_pack_id": int(row[0]),
        "extension_key": row[1],
        "extension_name": row[2],
        "description": row[3] or "",
        "scope_json": row[4] or {},
        "metadata": row[5] or {},
        "is_active": bool(row[6]),
        "created_by": row[7],
        "created_at": row[8],
        "modified_at": row[9],
    }


def list_workflow_extension_packs(
    connection: psycopg2.extensions.connection,
    is_active: bool | None = None,
    extension_key: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    normalized_key = str(extension_key or "").strip().lower() or None
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT extension_pack_id, extension_key, extension_name, description,
                   scope_json, metadata, is_active, created_by, created_at, modified_at
            FROM workflow_extension_pack
            WHERE (%s IS NULL OR is_active = %s)
              AND (%s IS NULL OR extension_key = %s)
            ORDER BY modified_at DESC, extension_pack_id DESC
            LIMIT %s
            """,
            (is_active, is_active, normalized_key, normalized_key, int(limit)),
        )
        rows = cursor.fetchall()

    return [
        {
            "extension_pack_id": int(row[0]),
            "extension_key": row[1],
            "extension_name": row[2],
            "description": row[3] or "",
            "scope_json": row[4] or {},
            "metadata": row[5] or {},
            "is_active": bool(row[6]),
            "created_by": row[7],
            "created_at": row[8],
            "modified_at": row[9],
        }
        for row in rows
    ]


def create_workflow_extension_version(
    connection: psycopg2.extensions.connection,
    extension_pack_id: int,
    status: str = "draft",
    conditions_schema_version: str = "v1",
    metadata: dict[str, Any] | None = None,
    is_active: bool = True,
    created_by: str | None = None,
) -> int:
    valid_statuses = {"draft", "review", "published", "deprecated", "archived"}
    normalized_status = str(status or "draft").strip().lower()
    if normalized_status not in valid_statuses:
        normalized_status = "draft"

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM workflow_extension_version WHERE extension_pack_id = %s",
            (int(extension_pack_id),),
        )
        next_version_no = int((cursor.fetchone() or [1])[0] or 1)

        cursor.execute(
            """
            INSERT INTO workflow_extension_version (
                extension_pack_id, version_no, status, conditions_schema_version,
                metadata, is_active, created_by, created_at, modified_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            RETURNING extension_version_id
            """,
            (
                int(extension_pack_id),
                int(next_version_no),
                normalized_status,
                str(conditions_schema_version or "v1").strip() or "v1",
                Json(metadata or {}),
                bool(is_active),
                str(created_by or "").strip() or None,
            ),
        )
        row = cursor.fetchone()
    connection.commit()
    return int(row[0])


def list_workflow_extension_versions(
    connection: psycopg2.extensions.connection,
    extension_pack_id: int,
    is_active: bool | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT extension_version_id, extension_pack_id, version_no, status,
                   conditions_schema_version, metadata, is_active, created_by,
                   created_at, modified_at
            FROM workflow_extension_version
            WHERE extension_pack_id = %s
              AND (%s IS NULL OR is_active = %s)
            ORDER BY version_no DESC, extension_version_id DESC
            LIMIT %s
            """,
            (int(extension_pack_id), is_active, is_active, int(limit)),
        )
        rows = cursor.fetchall()

    return [
        {
            "extension_version_id": int(row[0]),
            "extension_pack_id": int(row[1]),
            "version_no": int(row[2]),
            "status": row[3],
            "conditions_schema_version": row[4],
            "metadata": row[5] or {},
            "is_active": bool(row[6]),
            "created_by": row[7],
            "created_at": row[8],
            "modified_at": row[9],
        }
        for row in rows
    ]


def replace_workflow_extension_rules(
    connection: psycopg2.extensions.connection,
    extension_version_id: int,
    rules: list[dict[str, Any]],
) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM workflow_extension_rule WHERE extension_version_id = %s",
            (int(extension_version_id),),
        )

        inserted = 0
        for index, rule in enumerate(rules or [], start=1):
            rule_key = str(rule.get("rule_key") or f"rule_{index}").strip().lower()
            if not rule_key:
                rule_key = f"rule_{index}"

            hook_point = str(rule.get("hook_point") or "after_step").strip().lower()
            if hook_point not in {"on_run_start", "before_step", "after_step", "on_pause", "on_failure", "on_complete"}:
                hook_point = "after_step"

            conflict_policy = str(rule.get("conflict_policy") or "skip").strip().lower()
            if conflict_policy not in {"skip", "replace", "merge", "fail"}:
                conflict_policy = "skip"

            cursor.execute(
                """
                INSERT INTO workflow_extension_rule (
                    extension_version_id, rule_key, hook_point, target_step_key,
                    condition_json, precedence, conflict_policy, inserted_steps_json,
                    metadata, is_active, created_at, modified_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                """,
                (
                    int(extension_version_id),
                    rule_key,
                    hook_point,
                    str(rule.get("target_step_key") or "").strip() or None,
                    Json(rule.get("condition_json") if isinstance(rule.get("condition_json"), dict) else {}),
                    int(rule.get("precedence") or 100),
                    conflict_policy,
                    Json(rule.get("inserted_steps_json") if isinstance(rule.get("inserted_steps_json"), list) else []),
                    Json(rule.get("metadata") if isinstance(rule.get("metadata"), dict) else {}),
                    bool(rule.get("is_active", True)),
                ),
            )
            inserted += 1

    connection.commit()
    return inserted


def list_workflow_extension_rules(
    connection: psycopg2.extensions.connection,
    extension_version_id: int,
    is_active: bool | None = None,
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT extension_rule_id, extension_version_id, rule_key, hook_point,
                   target_step_key, condition_json, precedence, conflict_policy,
                   inserted_steps_json, metadata, is_active, created_at, modified_at
            FROM workflow_extension_rule
            WHERE extension_version_id = %s
              AND (%s IS NULL OR is_active = %s)
            ORDER BY precedence ASC, extension_rule_id ASC
            """,
            (int(extension_version_id), is_active, is_active),
        )
        rows = cursor.fetchall()

    return [
        {
            "extension_rule_id": int(row[0]),
            "extension_version_id": int(row[1]),
            "rule_key": row[2],
            "hook_point": row[3],
            "target_step_key": row[4],
            "condition_json": row[5] or {},
            "precedence": int(row[6]),
            "conflict_policy": row[7],
            "inserted_steps_json": row[8] or [],
            "metadata": row[9] or {},
            "is_active": bool(row[10]),
            "created_at": row[11],
            "modified_at": row[12],
        }
        for row in rows
    ]


def create_workflow_extension_attachment(
    connection: psycopg2.extensions.connection,
    workflow_key: str,
    workflow_version_id: int | None,
    extension_pack_id: int,
    extension_version_id: int | None,
    effective_from: str | None = None,
    effective_until: str | None = None,
    priority: int = 100,
    tenant_scope: str | None = None,
    metadata: dict[str, Any] | None = None,
    is_active: bool = True,
    created_by: str | None = None,
) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workflow_extension_attachment (
                workflow_key, workflow_version_id, extension_pack_id, extension_version_id,
                effective_from, effective_until, priority, tenant_scope, metadata,
                is_active, created_by, created_at, modified_at
            )
            VALUES (
                %s, %s, %s, %s,
                COALESCE(%s::date, CURRENT_DATE), %s::date, %s, %s, %s,
                %s, %s, NOW(), NOW()
            )
            RETURNING attachment_id
            """,
            (
                str(workflow_key or "").strip().lower(),
                int(workflow_version_id) if workflow_version_id is not None else None,
                int(extension_pack_id),
                int(extension_version_id) if extension_version_id is not None else None,
                str(effective_from or "").strip() or None,
                str(effective_until or "").strip() or None,
                int(priority),
                str(tenant_scope or "").strip() or None,
                Json(metadata or {}),
                bool(is_active),
                str(created_by or "").strip() or None,
            ),
        )
        row = cursor.fetchone()
    connection.commit()
    return int(row[0])


def list_workflow_extension_attachments(
    connection: psycopg2.extensions.connection,
    workflow_key: str | None = None,
    workflow_version_id: int | None = None,
    extension_pack_id: int | None = None,
    tenant_scope: str | None = None,
    is_active: bool | None = None,
    only_effective_now: bool = False,
    limit: int = 500,
) -> list[dict[str, Any]]:
    normalized_workflow_key = str(workflow_key or "").strip().lower() or None
    normalized_tenant_scope = str(tenant_scope or "").strip() or None

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT attachment_id, workflow_key, workflow_version_id, extension_pack_id,
                   extension_version_id, effective_from, effective_until, priority,
                   tenant_scope, metadata, is_active, created_by, created_at, modified_at
            FROM workflow_extension_attachment
            WHERE (%s IS NULL OR workflow_key = %s)
              AND (%s IS NULL OR workflow_version_id = %s)
              AND (%s IS NULL OR extension_pack_id = %s)
              AND (%s IS NULL OR tenant_scope = %s)
              AND (%s IS NULL OR is_active = %s)
              AND (
                    %s = FALSE OR (
                        effective_from <= CURRENT_DATE
                        AND (effective_until IS NULL OR effective_until >= CURRENT_DATE)
                    )
                  )
            ORDER BY priority ASC, attachment_id ASC
            LIMIT %s
            """,
            (
                normalized_workflow_key,
                normalized_workflow_key,
                int(workflow_version_id) if workflow_version_id is not None else None,
                int(workflow_version_id) if workflow_version_id is not None else None,
                int(extension_pack_id) if extension_pack_id is not None else None,
                int(extension_pack_id) if extension_pack_id is not None else None,
                normalized_tenant_scope,
                normalized_tenant_scope,
                is_active,
                is_active,
                bool(only_effective_now),
                int(limit),
            ),
        )
        rows = cursor.fetchall()

    return [
        {
            "attachment_id": int(row[0]),
            "workflow_key": row[1],
            "workflow_version_id": row[2],
            "extension_pack_id": int(row[3]),
            "extension_version_id": row[4],
            "effective_from": row[5],
            "effective_until": row[6],
            "priority": int(row[7]),
            "tenant_scope": row[8],
            "metadata": row[9] or {},
            "is_active": bool(row[10]),
            "created_by": row[11],
            "created_at": row[12],
            "modified_at": row[13],
        }
        for row in rows
    ]


def set_workflow_extension_attachment_active(
    connection: psycopg2.extensions.connection,
    attachment_id: int,
    is_active: bool,
) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE workflow_extension_attachment
            SET is_active = %s,
                modified_at = NOW()
            WHERE attachment_id = %s
            """,
            (bool(is_active), int(attachment_id)),
        )
        updated = int(cursor.rowcount or 0)
    connection.commit()
    return updated > 0


def replace_workflow_resource_aliases(
    connection: psycopg2.extensions.connection,
    workflow_id: int,
    workflow_version_id: int,
    aliases: list[dict[str, Any]],
    created_by: str | None = None,
) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM workflow_resource_alias WHERE workflow_version_id = %s",
            (int(workflow_version_id),),
        )

        inserted = 0
        for alias in aliases or []:
            resource_type = str(alias.get("resource_type") or "").strip().lower()
            if resource_type not in {"endpoint", "queue", "collection", "purpose", "scope", "policy"}:
                continue
            alias_name = str(alias.get("alias_name") or "").strip()
            canonical_name = str(alias.get("canonical_name") or "").strip()
            if not alias_name or not canonical_name:
                continue

            cursor.execute(
                """
                INSERT INTO workflow_resource_alias (
                    workflow_id, workflow_version_id, resource_type, alias_name,
                    canonical_name, metadata, created_by, created_at, modified_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                """,
                (
                    int(workflow_id),
                    int(workflow_version_id),
                    resource_type,
                    alias_name,
                    canonical_name,
                    Json(alias.get("metadata") if isinstance(alias.get("metadata"), dict) else {}),
                    str(created_by or "").strip() or None,
                ),
            )
            inserted += 1

    connection.commit()
    return inserted


def list_workflow_resource_aliases(
    connection: psycopg2.extensions.connection,
    workflow_id: int | None = None,
    workflow_version_id: int | None = None,
    resource_type: str | None = None,
) -> list[dict[str, Any]]:
    filters: list[str] = []
    params: list[Any] = []

    if workflow_id is not None:
        filters.append("workflow_id = %s")
        params.append(int(workflow_id))
    if workflow_version_id is not None:
        filters.append("workflow_version_id = %s")
        params.append(int(workflow_version_id))
    if resource_type:
        filters.append("resource_type = %s")
        params.append(str(resource_type).strip().lower())

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT alias_id, workflow_id, workflow_version_id, resource_type,
                   alias_name, canonical_name, metadata, created_by,
                   created_at, modified_at
            FROM workflow_resource_alias
            {where_sql}
            ORDER BY resource_type ASC, alias_name ASC, alias_id ASC
            """,
            tuple(params),
        )
        rows = cursor.fetchall()

    return [
        {
            "alias_id": int(row[0]),
            "workflow_id": int(row[1]),
            "workflow_version_id": int(row[2]),
            "resource_type": row[3],
            "alias_name": row[4],
            "canonical_name": row[5],
            "metadata": row[6] or {},
            "created_by": row[7],
            "created_at": row[8],
            "modified_at": row[9],
        }
        for row in rows
    ]


def upsert_business_rule_workflow_link(
    connection: psycopg2.extensions.connection,
    rule_id: int,
    workflow_id: int,
    workflow_version_id: int | None = None,
    link_type: str = "uses",
    metadata: dict[str, Any] | None = None,
    created_by: str | None = None,
) -> int:
    normalized_link_type = str(link_type or "uses").strip().lower()
    if normalized_link_type not in {"uses", "creates", "extends", "overrides"}:
        normalized_link_type = "uses"

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO business_rule_workflow_links (
                rule_id, workflow_id, workflow_version_id, link_type, metadata,
                created_by, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (rule_id, workflow_id, workflow_version_id, link_type)
            DO UPDATE SET
                metadata = COALESCE(business_rule_workflow_links.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                created_by = COALESCE(EXCLUDED.created_by, business_rule_workflow_links.created_by)
            RETURNING link_id
            """,
            (
                int(rule_id),
                int(workflow_id),
                int(workflow_version_id) if workflow_version_id is not None else None,
                normalized_link_type,
                Json(metadata or {}),
                str(created_by or "").strip() or None,
            ),
        )
        row = cursor.fetchone()
    connection.commit()
    return int(row[0])


def list_business_rule_workflow_links(
    connection: psycopg2.extensions.connection,
    rule_id: int,
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT l.link_id, l.rule_id, l.workflow_id, l.workflow_version_id, l.link_type,
                   l.metadata, l.created_by, l.created_at,
                   w.workflow_key, w.workflow_name,
                   v.version_no
            FROM business_rule_workflow_links l
            JOIN solf_workflow_registry w ON w.workflow_id = l.workflow_id
            LEFT JOIN solf_workflow_versions v ON v.workflow_version_id = l.workflow_version_id
            WHERE l.rule_id = %s
            ORDER BY l.created_at DESC, l.link_id DESC
            """,
            (int(rule_id),),
        )
        rows = cursor.fetchall()

    return [
        {
            "link_id": int(row[0]),
            "rule_id": int(row[1]),
            "workflow_id": int(row[2]),
            "workflow_version_id": row[3],
            "link_type": row[4],
            "metadata": row[5] or {},
            "created_by": row[6],
            "created_at": row[7],
            "workflow_key": row[8],
            "workflow_name": row[9],
            "version_no": row[10],
        }
        for row in rows
    ]


def get_solf_clauses_for_business_rule(
    connection: psycopg2.extensions.connection,
    rule_id: int,
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT clause_id, clause_name, clause_type, entity_class, clause_body, metadata
            FROM solf_clauses
            WHERE COALESCE(metadata->>'source', '') = 'business_rule_runtime'
              AND COALESCE(metadata->>'rule_id', '') = %s
            ORDER BY clause_name ASC
            """,
            (str(int(rule_id)),),
        )
        rows = cursor.fetchall()

    return [
        {
            "clause_id": int(row[0]),
            "clause_name": row[1],
            "clause_type": row[2],
            "entity_class": row[3],
            "clause_body": row[4],
            "metadata": row[5] or {},
        }
        for row in rows
    ]


def set_solf_workflow_version_status(
    connection: psycopg2.extensions.connection,
    workflow_version_id: int,
    status: str,
) -> bool:
    valid_statuses = {"draft", "review", "published", "deprecated", "archived"}
    normalized_status = str(status or "draft").strip().lower()
    if normalized_status not in valid_statuses:
        normalized_status = "draft"

    with connection.cursor() as cursor:
        is_active = normalized_status == "published"
        cursor.execute(
            """
            UPDATE solf_workflow_versions
            SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb,
                is_active = %s,
                modified_at = NOW()
            WHERE workflow_version_id = %s
            """,
            (
                Json({"status": normalized_status, "status_updated_at": datetime.utcnow().isoformat()}),
                is_active,
                int(workflow_version_id),
            ),
        )
        updated = int(cursor.rowcount or 0)
    connection.commit()
    return updated > 0


def get_workflow_active_version(
    connection: psycopg2.extensions.connection,
    workflow_id: int,
) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT workflow_version_id, workflow_id, version_no, rule_id, graph_spec,
                   input_contract, output_contract, metadata, is_active, created_by,
                   created_at, modified_at
            FROM solf_workflow_versions
            WHERE workflow_id = %s
              AND is_active = TRUE
            ORDER BY version_no DESC
            LIMIT 1
            """,
            (int(workflow_id),),
        )
        row = cursor.fetchone()

    if row is None:
        return None

    return {
        "workflow_version_id": int(row[0]),
        "workflow_id": int(row[1]),
        "version_no": int(row[2]),
        "rule_id": row[3],
        "graph_spec": row[4] or {},
        "input_contract": row[5] or {},
        "output_contract": row[6] or {},
        "metadata": row[7] or {},
        "is_active": bool(row[8]),
        "created_by": row[9],
        "created_at": row[10],
        "modified_at": row[11],
    }


def deactivate_workflow_versions_except(
    connection: psycopg2.extensions.connection,
    workflow_id: int,
    except_version_id: int | None = None,
) -> int:
    with connection.cursor() as cursor:
        if except_version_id is not None:
            cursor.execute(
                """
                UPDATE solf_workflow_versions
                SET is_active = FALSE,
                    modified_at = NOW()
                WHERE workflow_id = %s
                  AND workflow_version_id <> %s
                  AND is_active = TRUE
                """,
                (int(workflow_id), int(except_version_id)),
            )
        else:
            cursor.execute(
                """
                UPDATE solf_workflow_versions
                SET is_active = FALSE,
                    modified_at = NOW()
                WHERE workflow_id = %s
                  AND is_active = TRUE
                """,
                (int(workflow_id),),
            )
        deactivated = int(cursor.rowcount or 0)
    connection.commit()
    return deactivated


def get_temporal_attributes_as_of(
    connection: psycopg2.extensions.connection,
    src_id: int,
    src_type: str,
    as_of: date | str | None = None,
) -> dict[str, Any]:
    as_of_date = _coerce_date(as_of) or date.today()
    normalized_src_type = str(src_type).strip().lower()

    sql = """
    SELECT attr_type, attr_json, valid_from, valid_until
    FROM attribute
    WHERE src_id = %s
      AND src_type = %s
      AND valid_from <= %s
      AND (valid_until IS NULL OR valid_until > %s)
    ORDER BY attr_type, valid_from DESC, attr_id DESC
    """

    values: dict[str, Any] = {}
    with connection.cursor() as cursor:
        cursor.execute(sql, (int(src_id), normalized_src_type, as_of_date, as_of_date))
        rows = cursor.fetchall()

    for attr_type, attr_json, _valid_from, _valid_until in rows:
        attr_key = str(attr_type).strip().lower()
        if attr_key in values:
            continue
        payload = attr_json if isinstance(attr_json, dict) else {"value": attr_json}
        if isinstance(payload, dict) and "value" in payload and len(payload) == 1:
            values[attr_key] = payload.get("value")
        else:
            values[attr_key] = payload
    return values


def get_current_temporal_attributes(
    connection: psycopg2.extensions.connection,
    src_id: int,
    src_type: str,
) -> dict[str, Any]:
    return get_temporal_attributes_as_of(
        connection=connection,
        src_id=src_id,
        src_type=src_type,
        as_of=date.today(),
    )


def get_attribute_history(
    connection: psycopg2.extensions.connection,
    src_id: int,
    src_type: str,
    attr_type: str | None = None,
) -> list[dict[str, Any]]:
    normalized_src_type = str(src_type).strip().lower()
    normalized_attr_type = str(attr_type).strip().lower() if attr_type is not None else None

    sql = """
    SELECT attr_id, src_id, src_type, attr_type, valid_from, valid_until, attr_json, entry_date
    FROM attribute
    WHERE src_id = %s
      AND src_type = %s
      AND (%s IS NULL OR attr_type = %s)
    ORDER BY attr_type, valid_from DESC, attr_id DESC
    """

    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                int(src_id),
                normalized_src_type,
                normalized_attr_type,
                normalized_attr_type,
            ),
        )
        rows = cursor.fetchall()

    return [
        {
            "attr_id": int(row[0]),
            "src_id": int(row[1]),
            "src_type": row[2],
            "attr_type": row[3],
            "valid_from": row[4],
            "valid_until": row[5],
            "attr_json": row[6] or {},
            "entry_date": row[7],
        }
        for row in rows
    ]


def get_active_relationships_as_of(
    connection: psycopg2.extensions.connection,
    as_of: date | str | None = None,
    relationship_name: str | None = None,
    relationship_cat: str | None = None,
    src_object_id: int | None = None,
    tar_object_id: int | None = None,
) -> list[dict[str, Any]]:
    as_of_date = _coerce_date(as_of) or date.today()

    sql = """
    SELECT
        rel.relationship_id,
        rel.relationship_name,
        rel.relationship_cat,
        rel.src_object_id,
        rel.tar_object_id,
        rel.confidence,
        rel.metadata,
        rel.entry_date,
        src.object_name,
        src.class_name,
        tar.object_name,
        tar.class_name
    FROM object_relationship rel
    JOIN object_instance src ON src.object_id = rel.src_object_id
    JOIN object_instance tar ON tar.object_id = rel.tar_object_id
    WHERE (%s IS NULL OR rel.relationship_name = %s)
      AND (%s IS NULL OR rel.relationship_cat = %s)
      AND (%s IS NULL OR rel.src_object_id = %s)
      AND (%s IS NULL OR rel.tar_object_id = %s)
    ORDER BY rel.relationship_id
    """

    results: list[dict[str, Any]] = []
    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                relationship_name,
                relationship_name,
                relationship_cat,
                relationship_cat,
                src_object_id,
                src_object_id,
                tar_object_id,
                tar_object_id,
            ),
        )
        rows = cursor.fetchall()

    for row in rows:
        rel_id = int(row[0])
        rel_attrs = get_temporal_attributes_as_of(
            connection=connection,
            src_id=rel_id,
            src_type="relationship",
            as_of=as_of_date,
        )
        is_active = bool(rel_attrs.get("active", False))
        if not is_active:
            continue

        results.append(
            {
                "relationship_id": rel_id,
                "relationship_name": row[1],
                "relationship_cat": row[2],
                "src_object_id": int(row[3]),
                "tar_object_id": int(row[4]),
                "confidence": row[5],
                "metadata": row[6] or {},
                "entry_date": row[7],
                "src_object_name": row[8],
                "src_class_name": row[9],
                "tar_object_name": row[10],
                "tar_class_name": row[11],
                "temporal_attributes": rel_attrs,
            }
        )

    return results


def get_relationship_timeline(
    connection: psycopg2.extensions.connection,
    relationship_name: str | None = None,
    relationship_cat: str | None = None,
    src_object_id: int | None = None,
    tar_object_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = """
    SELECT
        rel.relationship_id,
        rel.relationship_name,
        rel.relationship_cat,
        rel.src_object_id,
        rel.tar_object_id,
        rel.confidence,
        rel.metadata,
        rel.entry_date,
        src.object_name,
        src.class_name,
        tar.object_name,
        tar.class_name
    FROM object_relationship rel
    JOIN object_instance src ON src.object_id = rel.src_object_id
    JOIN object_instance tar ON tar.object_id = rel.tar_object_id
    WHERE (%s IS NULL OR rel.relationship_name = %s)
      AND (%s IS NULL OR rel.relationship_cat = %s)
      AND (%s IS NULL OR rel.src_object_id = %s)
      AND (%s IS NULL OR rel.tar_object_id = %s)
    ORDER BY rel.relationship_id DESC
    LIMIT %s
    """

    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                relationship_name,
                relationship_name,
                relationship_cat,
                relationship_cat,
                src_object_id,
                src_object_id,
                tar_object_id,
                tar_object_id,
                limit,
            ),
        )
        rows = cursor.fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        relationship_id = int(row[0])
        history = get_attribute_history(
            connection=connection,
            src_id=relationship_id,
            src_type="relationship",
        )
        active_periods = [
            {
                "valid_from": item.get("valid_from"),
                "valid_until": item.get("valid_until"),
                "active": bool((item.get("attr_json") or {}).get("value") if isinstance(item.get("attr_json"), dict) else item.get("attr_json")),
            }
            for item in history
            if str(item.get("attr_type") or "").lower() == "active"
        ]

        results.append(
            {
                "relationship_id": relationship_id,
                "relationship_name": row[1],
                "relationship_cat": row[2],
                "src_object_id": int(row[3]),
                "tar_object_id": int(row[4]),
                "confidence": row[5],
                "metadata": row[6] or {},
                "entry_date": row[7],
                "src_object_name": row[8],
                "src_class_name": row[9],
                "tar_object_name": row[10],
                "tar_class_name": row[11],
                "active_periods": active_periods,
                "attribute_history": history,
            }
        )

    return results


def build_children_snapshot(
    children_rows: list[dict[str, Any]],
    date_key_fields: tuple[str, ...] = ("birth_date_key", "birth_date", "dob", "date_of_birth", "birthdate"),
) -> list[tuple[str, int | None]]:
    snapshot: list[tuple[str, int | None]] = []
    for child_row in children_rows:
        child_name = str(child_row.get("src_object_name") or child_row.get("object_name") or "").strip()
        if not child_name:
            continue

        child_metadata = child_row.get("src_metadata") or child_row.get("metadata") or {}
        relationship_metadata = child_row.get("relationship_metadata") or {}
        date_key = _extract_date_key(child_metadata, date_key_fields)
        if date_key is None:
            date_key = _extract_date_key(relationship_metadata, date_key_fields)
        snapshot.append((child_name, date_key))
    return snapshot


def seed_children_snapshot_policy(
    interpreter: Any,
    parent_ref: Any,
    children_rows: list[dict[str, Any]],
    date_key_fields: tuple[str, ...] = ("birth_date_key", "birth_date", "dob", "date_of_birth", "birthdate"),
    evaluate_clause: bool = True,
) -> dict[str, Any]:
    if not hasattr(interpreter, "variables"):
        raise TypeError("seed_children_snapshot_policy() requires an interpreter with variables")

    snapshot = build_children_snapshot(children_rows, date_key_fields=date_key_fields)
    parent_key = parent_ref.strip() if isinstance(parent_ref, str) else parent_ref

    variables = interpreter.variables
    children_snapshot_map = variables.setdefault("children_snapshot_by_parent_ref", {})
    children_count_map = variables.setdefault("children_count_by_parent_ref", {})
    children_snapshot_map[parent_key] = snapshot
    children_count_map[parent_key] = len(snapshot)

    result: dict[str, Any] = {
        "parent_ref": parent_key,
        "children_snapshot": snapshot,
        "children_count": len(snapshot),
    }

    if evaluate_clause and hasattr(interpreter, "parser") and hasattr(interpreter, "execute_predicate"):
        clause_expression = f"child_allowance_input_complete({parent_key})"
        ast = interpreter.parser.parse(clause_expression)
        result["input_complete"] = bool(interpreter.execute_predicate(ast))

    return result


def load_children_snapshot_policy(
    connection: psycopg2.extensions.connection,
    interpreter: Any,
    parent_ref: Any,
    parent_object_name: str,
    parent_class_name: str | None = None,
    relationship_name: str = "child_of",
    relationship_cat: str | None = None,
    date_key_fields: tuple[str, ...] = ("birth_date_key", "birth_date", "dob", "date_of_birth", "birthdate"),
    evaluate_clause: bool = True,
) -> dict[str, Any]:
    children_rows = get_relationships(
        connection=connection,
        relationship_name=relationship_name,
        relationship_cat=relationship_cat,
        tar_object_name=parent_object_name,
        tar_class_name=parent_class_name,
    )

    result = seed_children_snapshot_policy(
        interpreter=interpreter,
        parent_ref=parent_ref,
        children_rows=children_rows,
        date_key_fields=date_key_fields,
        evaluate_clause=evaluate_clause,
    )
    result["children_rows"] = children_rows
    return result


def get_object_instance(
    connection: psycopg2.extensions.connection,
    object_name: str,
    class_name: str | None = None,
        as_of_date: date | None = None,
        include_inactive: bool = False,
) -> dict[str, Any] | None:
    sql = """
    SELECT object_id, object_name, canonical_full_name, class_name, metadata, status, valid_from, valid_until, entry_date
    FROM object_instance
    WHERE (LOWER(object_name) = LOWER(%s) OR LOWER(COALESCE(canonical_full_name, '')) = LOWER(%s))
      AND (%s IS NULL OR class_name = %s)
      AND (%s OR COALESCE(status, 'active') = 'active')
      AND (valid_from <= %s)
      AND (valid_until IS NULL OR valid_until > %s)
    ORDER BY entry_date DESC
    LIMIT 1
    """

    with connection.cursor() as cursor:
        effective_date = as_of_date or date.today()
        cursor.execute(
            sql,
            (object_name, object_name, class_name, class_name, include_inactive, effective_date, effective_date),
        )
        row = cursor.fetchone()

    if not row:
        return None

    return {
        "object_id": row[0],
        "object_name": row[1],
        "canonical_full_name": row[2],
        "class_name": row[3],
        "metadata": row[4] or {},
        "status": row[5],
        "valid_from": row[6],
        "valid_until": row[7],
        "entry_date": row[8],
    }


def get_object_instance_by_id(
    connection: psycopg2.extensions.connection,
    object_id: int,
    as_of_date: date | None = None,
    include_inactive: bool = False,
) -> dict[str, Any] | None:
    object_id = int(object_id)
    
    # Resolve merged objects to canonical form
    canonical_id = resolve_merged_object_to_canonical(connection, object_id)
    if canonical_id != object_id:
        # Object was merged, use canonical ID instead
        object_id = canonical_id
    
    sql = """
    SELECT object_id, object_name, canonical_full_name, class_name, metadata, status, valid_from, valid_until, entry_date
    FROM object_instance
    WHERE object_id = %s
      AND (%s OR COALESCE(status, 'active') = 'active')
      AND (valid_from <= %s)
      AND (valid_until IS NULL OR valid_until > %s)
    LIMIT 1
    """

    with connection.cursor() as cursor:
        effective_date = as_of_date or date.today()
        cursor.execute(sql, (int(object_id), include_inactive, effective_date, effective_date))
        row = cursor.fetchone()

    if not row:
        return None

    return {
        "object_id": row[0],
        "object_name": row[1],
        "canonical_full_name": row[2],
        "class_name": row[3],
        "metadata": row[4] or {},
        "status": row[5],
        "valid_from": row[6],
        "valid_until": row[7],
        "entry_date": row[8],
    }


def get_alias_group_object_ids(
    connection: psycopg2.extensions.connection,
    object_id: int,
    include_self: bool = True,
) -> list[int]:
    _ensure_object_alias_table(connection)

    sql = """
    WITH RECURSIVE alias_graph AS (
        SELECT %s::BIGINT AS object_id
        UNION
        SELECT CASE
            WHEN oa.primary_object_id = g.object_id THEN oa.alias_object_id
            ELSE oa.primary_object_id
        END AS object_id
        FROM object_alias oa
        JOIN alias_graph g
          ON oa.primary_object_id = g.object_id
          OR oa.alias_object_id = g.object_id
        WHERE COALESCE(oa.status, 'active') = 'active'
    )
    SELECT DISTINCT object_id
    FROM alias_graph
    ORDER BY object_id
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, (int(object_id),))
        rows = [int(row[0]) for row in cursor.fetchall() or []]

    if include_self:
        return rows if rows else [int(object_id)]
    return [item for item in rows if int(item) != int(object_id)]


def get_alias_group_for_name(
    connection: psycopg2.extensions.connection,
    object_name: str,
    class_name: str | None = None,
) -> dict[str, Any] | None:
    seed = get_object_instance(connection, object_name=object_name, class_name=class_name)
    if not seed:
        return None

    object_ids = get_alias_group_object_ids(connection, int(seed["object_id"]), include_self=True)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT object_id, object_name, canonical_full_name, class_name, metadata
            FROM object_instance
            WHERE object_id = ANY(%s)
            ORDER BY object_name, object_id
            """,
            (object_ids,),
        )
        rows = cursor.fetchall() or []

    members = [
        {
            "object_id": int(row[0]),
            "object_name": row[1],
            "canonical_full_name": row[2],
            "class_name": row[3],
            "metadata": row[4] if isinstance(row[4], dict) else {},
        }
        for row in rows
    ]
    return {
        "seed_object_id": int(seed["object_id"]),
        "seed_object_name": seed["object_name"],
        "class_name": seed["class_name"],
        "object_ids": object_ids,
        "members": members,
    }


def resolve_merged_object_to_canonical(
    connection: psycopg2.extensions.connection,
    object_id: int,
) -> int:
    """Resolve a merged object to its canonical object via merged_record alias link.
    
    If the object is merged, follows the merged_record alias to find the canonical.
    If not merged or no alias found, returns the original object_id.
    
    Args:
        connection: Database connection
        object_id: Object ID to resolve
        
    Returns:
        The canonical object_id (same as input if not merged, or the primary_object_id of the merged_record alias)
    """
    object_id = int(object_id)
    
    # Check if object is merged
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT status
            FROM object_instance
            WHERE object_id = %s
            LIMIT 1
            """,
            (object_id,)
        )
        row = cursor.fetchone()
    
    if not row or row[0] != 'merged':
        return object_id
    
    # Object is merged, look for merged_record alias link
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT primary_object_id
            FROM object_alias
            WHERE alias_object_id = %s
              AND alias_type = 'merged_record'
              AND COALESCE(status, 'active') = 'active'
            LIMIT 1
            """,
            (object_id,)
        )
        row = cursor.fetchone()
    
    if row:
        canonical_id = int(row[0])
        return canonical_id
    
    # No merged_record alias found, return original
    return object_id


def suggest_similar_objects(
    connection: psycopg2.extensions.connection,
    object_name: str,
    class_name: str = "person",
    limit: int = 8,
    min_similarity: float = 0.12,
) -> list[dict[str, Any]]:
    candidates = search_objects(
        connection=connection,
        name_query=object_name,
        class_name=class_name,
        limit=max(3, int(limit)),
    )
    out: list[dict[str, Any]] = []
    normalized_hint = _canonical_person_full_name(object_name) if str(class_name).strip().lower() == "person" else str(object_name or "").strip()
    hint_tokens = [tok for tok in re.findall(r"[A-Za-z0-9ÄÖÜäöüß]+", normalized_hint.lower()) if tok]

    for candidate in candidates:
        similarity = float(candidate.get("similarity") or 0.0)
        if similarity < float(min_similarity):
            continue
        candidate_name = str(candidate.get("object_name") or "").strip()
        candidate_tokens = [tok for tok in re.findall(r"[A-Za-z0-9ÄÖÜäöüß]+", candidate_name.lower()) if tok]
        overlap = sorted(set(hint_tokens).intersection(candidate_tokens))
        out.append(
            {
                "object_id": int(candidate.get("object_id") or 0),
                "object_name": candidate_name,
                "class_name": candidate.get("class_name"),
                "similarity": similarity,
                "token_overlap": overlap,
                "overlap_count": len(overlap),
                "metadata": candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {},
            }
        )

    out.sort(key=lambda item: (-float(item.get("similarity") or 0.0), -int(item.get("overlap_count") or 0), item.get("object_name") or ""))
    return out[: max(1, int(limit))]


def link_object_alias(
    connection: psycopg2.extensions.connection,
    primary_object_id: int,
    alias_object_id: int,
    alias_type: str = "same_entity",
    confidence: float | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    _ensure_object_alias_table(connection)

    primary = int(primary_object_id)
    alias = int(alias_object_id)
    if primary == alias:
        raise ValueError("primary_object_id and alias_object_id must be different")

    left = min(primary, alias)
    right = max(primary, alias)
    payload = metadata or {}

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO object_alias (
                primary_object_id,
                alias_object_id,
                alias_type,
                confidence,
                status,
                metadata
            )
            VALUES (%s, %s, %s, %s, 'active', %s::jsonb)
            ON CONFLICT (primary_object_id, alias_object_id)
            DO UPDATE
            SET alias_type = EXCLUDED.alias_type,
                confidence = COALESCE(EXCLUDED.confidence, object_alias.confidence),
                status = 'active',
                metadata = COALESCE(object_alias.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                updated_at = NOW()
            RETURNING alias_id, primary_object_id, alias_object_id, alias_type, confidence, status, metadata, created_at, updated_at
            """,
            (left, right, str(alias_type or "same_entity").strip().lower(), confidence, json.dumps(payload)),
        )
        row = cursor.fetchone()

    if commit:
        connection.commit()

    return {
        "alias_id": int(row[0]),
        "primary_object_id": int(row[1]),
        "alias_object_id": int(row[2]),
        "alias_type": row[3],
        "confidence": row[4],
        "status": row[5],
        "metadata": row[6] if isinstance(row[6], dict) else {},
        "created_at": row[7],
        "updated_at": row[8],
    }


def merge_object_instances(
    connection: psycopg2.extensions.connection,
    canonical_object_id: int,
    duplicate_object_id: int,
    merge_note: str = "",
    keep_alias_link: bool = True,
) -> dict[str, Any]:
    canonical = int(canonical_object_id)
    duplicate = int(duplicate_object_id)
    if canonical == duplicate:
        raise ValueError("canonical_object_id and duplicate_object_id must be different")

    canonical_row = get_object_instance_by_id(connection, canonical, include_inactive=True)
    duplicate_row = get_object_instance_by_id(connection, duplicate, include_inactive=True)
    if not canonical_row or not duplicate_row:
        raise ValueError("Both canonical and duplicate object ids must exist")
    if str(canonical_row.get("class_name") or "") != str(duplicate_row.get("class_name") or ""):
        raise ValueError("Cannot merge objects with different class_name values")

    rels_retargeted = 0
    rels_deleted_as_duplicate = 0

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT relationship_id, relationship_name, src_object_id, tar_object_id, metadata, confidence
            FROM object_relationship
            WHERE src_object_id = %s OR tar_object_id = %s
            ORDER BY relationship_id
            """,
            (duplicate, duplicate),
        )
        rel_rows = cursor.fetchall() or []

        for rel_id, rel_name, src_id, tar_id, rel_meta, rel_conf in rel_rows:
            new_src = canonical if int(src_id) == duplicate else int(src_id)
            new_tar = canonical if int(tar_id) == duplicate else int(tar_id)

            if new_src == new_tar:
                cursor.execute("DELETE FROM object_relationship WHERE relationship_id = %s", (int(rel_id),))
                rels_deleted_as_duplicate += 1
                continue

            cursor.execute(
                """
                SELECT relationship_id
                FROM object_relationship
                WHERE relationship_name = %s
                  AND src_object_id = %s
                  AND tar_object_id = %s
                  AND relationship_id <> %s
                LIMIT 1
                """,
                (rel_name, new_src, new_tar, int(rel_id)),
            )
            existing = cursor.fetchone()

            if existing:
                keeper_rel_id = int(existing[0])
                cursor.execute(
                    """
                    UPDATE object_relationship
                    SET metadata = COALESCE(object_relationship.metadata, '{}'::jsonb) || %s::jsonb,
                        confidence = GREATEST(COALESCE(object_relationship.confidence, 0), COALESCE(%s, 0))
                    WHERE relationship_id = %s
                    """,
                    (json.dumps(rel_meta if isinstance(rel_meta, dict) else {}), rel_conf, keeper_rel_id),
                )
                cursor.execute(
                    """
                    UPDATE attribute
                    SET src_id = %s
                    WHERE src_type = 'relationship'
                      AND src_id = %s
                    """,
                    (keeper_rel_id, int(rel_id)),
                )
                cursor.execute("DELETE FROM object_relationship WHERE relationship_id = %s", (int(rel_id),))
                rels_deleted_as_duplicate += 1
                continue

            cursor.execute(
                "UPDATE object_relationship SET src_object_id = %s, tar_object_id = %s WHERE relationship_id = %s",
                (new_src, new_tar, int(rel_id)),
            )
            rels_retargeted += 1

        cursor.execute(
            "UPDATE attribute SET src_id = %s WHERE src_type = 'object' AND src_id = %s",
            (canonical, duplicate),
        )
        attrs_retargeted = int(cursor.rowcount or 0)

        cursor.execute(
            "UPDATE part SET object_id = %s WHERE object_id = %s",
            (canonical, duplicate),
        )
        parts_retargeted = int(cursor.rowcount or 0)

        merged_meta = {
            "merged_into": canonical,
            "merge_note": str(merge_note or "").strip(),
            "merged_at": datetime.utcnow().isoformat() + "Z",
        }
        cursor.execute(
            """
            UPDATE object_instance
            SET status = 'merged',
                valid_until = COALESCE(valid_until, CURRENT_DATE),
                metadata = COALESCE(object_instance.metadata, '{}'::jsonb) || %s::jsonb
            WHERE object_id = %s
            """,
            (json.dumps(merged_meta), duplicate),
        )

    alias_row = None
    if keep_alias_link:
        alias_row = link_object_alias(
            connection=connection,
            primary_object_id=canonical,
            alias_object_id=duplicate,
            alias_type="merged_record",
            confidence=1.0,
            metadata={"merge_note": str(merge_note or "").strip()},
            commit=False,
        )

    connection.commit()
    return {
        "canonical_object_id": canonical,
        "duplicate_object_id": duplicate,
        "canonical_object_name": canonical_row.get("object_name"),
        "duplicate_object_name": duplicate_row.get("object_name"),
        "class_name": canonical_row.get("class_name"),
        "relationships_retargeted": rels_retargeted,
        "relationships_deleted_as_duplicate": rels_deleted_as_duplicate,
        "attributes_retargeted": attrs_retargeted,
        "parts_retargeted": parts_retargeted,
        "alias_link": alias_row,
    }


def update_object_instance_by_id(
    connection: psycopg2.extensions.connection,
    object_id: int,
    metadata: dict[str, Any] | None = None,
    object_name: str | None = None,
) -> dict[str, Any] | None:
    payload = metadata or {}

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT class_name, object_name FROM object_instance WHERE object_id = %s",
            (int(object_id),),
        )
        existing_row = cursor.fetchone()

    if not existing_row:
        return None

    existing_class_name = str(existing_row[0] or "").strip().lower()
    effective_name = str(object_name or existing_row[1] or "").strip()
    canonical_full_name = _canonical_person_full_name(effective_name) if existing_class_name == "person" else None

    if object_name and object_name.strip():
        sql = """
        UPDATE object_instance
        SET object_name = %s,
            canonical_full_name = %s,
            metadata = COALESCE(object_instance.metadata, '{}'::jsonb) || %s::jsonb
        WHERE object_id = %s
        RETURNING object_id, object_name, canonical_full_name, class_name, metadata, entry_date
        """
        params = (object_name.strip(), canonical_full_name, json.dumps(payload), int(object_id))
    else:
        sql = """
        UPDATE object_instance
        SET canonical_full_name = %s,
            metadata = COALESCE(object_instance.metadata, '{}'::jsonb) || %s::jsonb
        WHERE object_id = %s
        RETURNING object_id, object_name, canonical_full_name, class_name, metadata, entry_date
        """
        params = (canonical_full_name, json.dumps(payload), int(object_id))

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()

    if not row:
        connection.rollback()
        return None

    connection.commit()
    return {
        "object_id": row[0],
        "object_name": row[1],
        "canonical_full_name": row[2],
        "class_name": row[3],
        "metadata": row[4] or {},
        "entry_date": row[5],
    }


def upsert_object_instance(
    connection: psycopg2.extensions.connection,
    object_name: str,
    class_name: str,
    metadata: dict[str, Any] | None = None,
    status: str = "active",
    valid_from: date | str | None = None,
    valid_until: date | str | None = None,
) -> dict[str, Any]:
    canonical_full_name = _canonical_person_full_name(object_name) if str(class_name or "").strip().lower() == "person" else None
    sql = """
    INSERT INTO object_instance (object_name, canonical_full_name, class_name, metadata, status, valid_from, valid_until)
    VALUES (%s, %s, %s, %s::jsonb, %s, COALESCE(%s, CURRENT_DATE), %s)
    ON CONFLICT (object_name, class_name)
    DO UPDATE SET
        canonical_full_name = COALESCE(object_instance.canonical_full_name, EXCLUDED.canonical_full_name),
        metadata = COALESCE(object_instance.metadata, '{}'::jsonb) || EXCLUDED.metadata,
        status = EXCLUDED.status,
        valid_from = COALESCE(object_instance.valid_from, EXCLUDED.valid_from),
        valid_until = EXCLUDED.valid_until
    RETURNING object_id, object_name, canonical_full_name, class_name, metadata, status, valid_from, valid_until, entry_date
    """

    payload = metadata or {}
    with connection.cursor() as cursor:
        cursor.execute(sql, (object_name, canonical_full_name, class_name, json.dumps(payload), status, valid_from, valid_until))
        row = cursor.fetchone()
    connection.commit()

    return {
        "object_id": row[0],
        "object_name": row[1],
        "canonical_full_name": row[2],
        "class_name": row[3],
        "metadata": row[4] or {},
        "status": row[5],
        "valid_from": row[6],
        "valid_until": row[7],
        "entry_date": row[8],
    }


def enqueue_resolution_ambiguity(
    connection: psycopg2.extensions.connection,
    entity_payload: dict[str, Any],
    resolution_result: dict[str, Any],
    status: str = "pending",
) -> dict[str, Any]:
    sql = """
    INSERT INTO resolution_ambiguity_queue (
        entity_id,
        object_name,
        class_name,
        doc_id,
        payload,
        resolution_result,
        status
    )
    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
    RETURNING queue_id, entity_id, object_name, class_name, doc_id, payload, resolution_result, status, created_at, updated_at
    """

    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                entity_payload.get("entity_id"),
                str(entity_payload.get("object_name") or entity_payload.get("name") or "").strip(),
                str(entity_payload.get("class_name") or "entity").strip().lower(),
                entity_payload.get("doc_id"),
                json.dumps(entity_payload or {}),
                json.dumps(resolution_result or {}),
                status,
            ),
        )
        row = cursor.fetchone()

    connection.commit()
    return {
        "queue_id": row[0],
        "entity_id": row[1],
        "object_name": row[2],
        "class_name": row[3],
        "doc_id": row[4],
        "payload": row[5] or {},
        "resolution_result": row[6] or {},
        "status": row[7],
        "created_at": row[8],
        "updated_at": row[9],
    }


def enqueue_tx_match_index_pending(
    connection: psycopg2.extensions.connection,
    payload: dict[str, Any],
    doc_id: int | None = None,
    error_message: str | None = None,
    source: str = "ingestion",
    status: str = "pending",
) -> dict[str, Any]:
    sql = """
    INSERT INTO tx_match_index_pending (
        doc_id,
        source,
        status,
        payload,
        error_message
    )
    VALUES (%s, %s, %s, %s::jsonb, %s)
    RETURNING pending_id, doc_id, source, status, payload, error_message, retry_count, created_at, updated_at
    """

    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                int(doc_id) if doc_id else None,
                str(source or "ingestion").strip() or "ingestion",
                str(status or "pending").strip() or "pending",
                json.dumps(payload or {}),
                str(error_message or "").strip() or None,
            ),
        )
        row = cursor.fetchone()

    connection.commit()
    return {
        "pending_id": row[0],
        "doc_id": row[1],
        "source": row[2],
        "status": row[3],
        "payload": row[4] or {},
        "error_message": row[5],
        "retry_count": int(row[6] or 0),
        "created_at": row[7],
        "updated_at": row[8],
    }


def list_tx_match_index_pending(
    connection: psycopg2.extensions.connection,
    statuses: list[str] | None = None,
    limit: int = 100,
    doc_id: int | None = None,
) -> list[dict[str, Any]]:
    normalized_statuses = [str(item).strip().lower() for item in (statuses or []) if str(item).strip()]
    params: list[Any] = []

    sql = """
    SELECT pending_id, doc_id, source, status, payload, error_message, retry_count, created_at, updated_at
    FROM tx_match_index_pending
    WHERE 1=1
    """

    if normalized_statuses:
        sql += " AND status = ANY(%s)"
        params.append(normalized_statuses)

    if doc_id is not None:
        sql += " AND doc_id = %s"
        params.append(int(doc_id))

    sql += " ORDER BY created_at ASC LIMIT %s"
    params.append(max(1, int(limit)))

    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()

    return [
        {
            "pending_id": int(row[0]),
            "doc_id": row[1],
            "source": row[2],
            "status": row[3],
            "payload": row[4] or {},
            "error_message": row[5],
            "retry_count": int(row[6] or 0),
            "created_at": row[7],
            "updated_at": row[8],
        }
        for row in rows
    ]


def get_tx_match_index_pending_by_id(
    connection: psycopg2.extensions.connection,
    pending_id: int,
) -> dict[str, Any] | None:
    sql = """
    SELECT pending_id, doc_id, source, status, payload, error_message, retry_count, created_at, updated_at
    FROM tx_match_index_pending
    WHERE pending_id = %s
    LIMIT 1
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, (int(pending_id),))
        row = cursor.fetchone()

    if not row:
        return None

    return {
        "pending_id": int(row[0]),
        "doc_id": row[1],
        "source": row[2],
        "status": row[3],
        "payload": row[4] or {},
        "error_message": row[5],
        "retry_count": int(row[6] or 0),
        "created_at": row[7],
        "updated_at": row[8],
    }


def update_tx_match_index_pending_status(
    connection: psycopg2.extensions.connection,
    pending_id: int,
    status: str,
    error_message: str | None = None,
    retry_increment: bool = False,
    last_result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    current = get_tx_match_index_pending_by_id(connection, pending_id)
    if current is None:
        return None

    payload = dict(current.get("payload") or {})
    if isinstance(last_result, dict):
        payload["last_retry_result"] = last_result

    sql = """
    UPDATE tx_match_index_pending
    SET status = %s,
        payload = %s::jsonb,
        error_message = %s,
        retry_count = retry_count + %s,
        updated_at = NOW()
    WHERE pending_id = %s
    RETURNING pending_id, doc_id, source, status, payload, error_message, retry_count, created_at, updated_at
    """

    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                str(status or "pending").strip().lower() or "pending",
                json.dumps(payload),
                str(error_message or "").strip() or None,
                1 if retry_increment else 0,
                int(pending_id),
            ),
        )
        row = cursor.fetchone()

    connection.commit()
    if not row:
        return None

    return {
        "pending_id": int(row[0]),
        "doc_id": row[1],
        "source": row[2],
        "status": row[3],
        "payload": row[4] or {},
        "error_message": row[5],
        "retry_count": int(row[6] or 0),
        "created_at": row[7],
        "updated_at": row[8],
    }


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
    relationship_name = canonicalize_relationship_name(relationship_name)
    payload = metadata or {}

    existing = _find_semantic_equivalent_relationship(
        connection=connection,
        relationship_name=relationship_name,
        relationship_cat=relationship_cat,
        src_object_id=src_object_id,
        tar_object_id=tar_object_id,
    )
    if existing is not None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE object_relationship
                SET relationship_name = %s,
                    relationship_cat = %s,
                    confidence = COALESCE(%s, object_relationship.confidence),
                    metadata = COALESCE(object_relationship.metadata, '{}'::jsonb) || %s::jsonb,
                    valid_from = COALESCE(object_relationship.valid_from, COALESCE(%s, CURRENT_DATE)),
                    valid_until = COALESCE(%s, object_relationship.valid_until)
                WHERE relationship_id = %s
                RETURNING relationship_id, relationship_name, relationship_cat, src_object_id, tar_object_id, confidence, metadata, valid_from, valid_until, entry_date
                """,
                (
                    relationship_name,
                    relationship_cat,
                    confidence,
                    json.dumps(payload),
                    valid_from,
                    valid_until,
                    int(existing["relationship_id"]),
                ),
            )
            row = cursor.fetchone()
        connection.commit()

        return {
            "relationship_id": row[0],
            "relationship_name": row[1],
            "relationship_cat": row[2],
            "src_object_id": row[3],
            "tar_object_id": row[4],
            "confidence": row[5],
            "metadata": row[6] or {},
            "valid_from": row[7],
            "valid_until": row[8],
            "entry_date": row[9],
        }

    sql = """
    INSERT INTO object_relationship (
        relationship_name,
        relationship_cat,
        src_object_id,
        tar_object_id,
        confidence,
        metadata,
        valid_from,
        valid_until
    )
    VALUES (%s, %s, %s, %s, %s, %s::jsonb, COALESCE(%s, CURRENT_DATE), %s)
    ON CONFLICT (relationship_name, src_object_id, tar_object_id)
    DO UPDATE SET
        relationship_cat = EXCLUDED.relationship_cat,
        confidence = EXCLUDED.confidence,
        metadata = COALESCE(object_relationship.metadata, '{}'::jsonb) || EXCLUDED.metadata,
        valid_from = COALESCE(object_relationship.valid_from, EXCLUDED.valid_from),
        valid_until = EXCLUDED.valid_until
    RETURNING relationship_id, relationship_name, relationship_cat, src_object_id, tar_object_id, confidence, metadata, valid_from, valid_until, entry_date
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, (relationship_name, relationship_cat, src_object_id, tar_object_id, confidence, json.dumps(payload), valid_from, valid_until))
        row = cursor.fetchone()
    connection.commit()

    return {
        "relationship_id": row[0],
        "relationship_name": row[1],
        "relationship_cat": row[2],
        "src_object_id": row[3],
        "tar_object_id": row[4],
        "confidence": row[5],
        "metadata": row[6] or {},
        "valid_from": row[7],
        "valid_until": row[8],
        "entry_date": row[9],
    }


def relate_objects_by_name(
    connection: psycopg2.extensions.connection,
    relationship_name: str,
    relationship_cat: str,
    src_object_name: str,
    src_class_name: str | None,
    tar_object_name: str,
    tar_class_name: str | None,
    confidence: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    src = get_object_instance(connection, src_object_name, src_class_name)
    if not src:
        raise LookupError(f"Source object not found: {src_object_name}")

    tar = get_object_instance(connection, tar_object_name, tar_class_name)
    if not tar:
        raise LookupError(f"Target object not found: {tar_object_name}")

    relationship = upsert_object_relationship(
        connection=connection,
        relationship_name=relationship_name,
        relationship_cat=relationship_cat,
        src_object_id=int(src["object_id"]),
        tar_object_id=int(tar["object_id"]),
        confidence=confidence,
        metadata=metadata,
    )

    relationship["src_object_name"] = src["object_name"]
    relationship["tar_object_name"] = tar["object_name"]
    return relationship


def get_children_of_parent(
    connection: psycopg2.extensions.connection,
    parent_object_name: str,
    parent_class_name: str | None = None,
    relationship_name: str = "child_of",
) -> list[dict[str, Any]]:
    relationships = get_relationships(
        connection=connection,
        relationship_name=relationship_name,
        tar_object_name=parent_object_name,
        tar_class_name=parent_class_name,
    )

    return [
        {
            "object_id": row["src_object_id"],
            "object_name": row["src_object_name"],
            "class_name": row["src_class_name"],
            "metadata": row["src_metadata"],
            "relationship_id": row["relationship_id"],
            "relationship_name": row["relationship_name"],
            "relationship_cat": row["relationship_cat"],
            "confidence": row["confidence"],
            "relationship_metadata": row["relationship_metadata"],
            "entry_date": row["entry_date"],
        }
        for row in relationships
    ]


def build_solf_candidate_matcher(
    interpreter: Any,
    predicate_template: str,
    result_var: str = "_ok",
) -> Callable[[dict[str, Any]], bool]:
    """Create a candidate matcher using SOLF clause execution.

    Example predicate_template:
    "({_ok} ? sameCompanyName {object_name} \"ExampleCo\")"
    The template can reference: object_id, object_name, and _ok.
    """

    def _matcher(candidate: dict[str, Any]) -> bool:
        predicate = predicate_template.format(
            object_id=candidate.get("object_id"),
            object_name=candidate.get("object_name"),
            _ok=result_var,
        )
        if not hasattr(interpreter, "execute_predicate") or not hasattr(interpreter, "parser"):
            return False

        ast = interpreter.parser.parse(predicate)
        interpreter.execute_predicate(ast)
        return bool(getattr(interpreter, "variables", {}).get(result_var))

    return _matcher


def _migrate_legacy_object_class_schema(connection: psycopg2.extensions.connection) -> None:
    """Backfill legacy object_class layouts to the current class_id-based schema.

    Older databases may only have class_name without class_id/parent_class_id.
    Dependent tables (for example part.class_id) require object_class.class_id to exist
    and be unique/referencable.
    """

    if not _table_exists(connection, "object_class"):
        return

    sql_statements = [
        "ALTER TABLE IF EXISTS object_class ADD COLUMN IF NOT EXISTS class_id BIGINT GENERATED BY DEFAULT AS IDENTITY;",
        "UPDATE object_class SET class_id = DEFAULT WHERE class_id IS NULL;",
        "ALTER TABLE IF EXISTS object_class ALTER COLUMN class_id SET NOT NULL;",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_object_class_class_id ON object_class(class_id);",
        "ALTER TABLE IF EXISTS object_class ADD COLUMN IF NOT EXISTS parent_class_id BIGINT;",
        "ALTER TABLE IF EXISTS object_class ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;",
        "ALTER TABLE IF EXISTS object_class ADD COLUMN IF NOT EXISTS entry_date TIMESTAMPTZ NOT NULL DEFAULT NOW();",
        "ALTER TABLE IF EXISTS object_class DROP CONSTRAINT IF EXISTS fk_object_class_parent;",
        "ALTER TABLE IF EXISTS object_class ADD CONSTRAINT fk_object_class_parent FOREIGN KEY (parent_class_id) REFERENCES object_class(class_id) ON DELETE SET NULL;",
    ]

    with connection.cursor() as cursor:
        for sql in sql_statements:
            cursor.execute(sql)


def _migrate_legacy_part_schema(connection: psycopg2.extensions.connection) -> None:
    """Backfill legacy part table columns used by current ingestion pipeline."""

    if not _table_exists(connection, "part"):
        return

    sql_statements = [
        "DROP TABLE IF EXISTS edge CASCADE;",
        "DROP TABLE IF EXISTS node CASCADE;",
        "ALTER TABLE IF EXISTS part ADD COLUMN IF NOT EXISTS class_id BIGINT;",
        "ALTER TABLE IF EXISTS part ADD COLUMN IF NOT EXISTS object_id BIGINT;",
        "ALTER TABLE IF EXISTS part ADD COLUMN IF NOT EXISTS relationship_id BIGINT;",
        "ALTER TABLE IF EXISTS part ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;",
        "ALTER TABLE IF EXISTS part DROP COLUMN IF EXISTS node_id CASCADE;",
        "ALTER TABLE IF EXISTS part DROP COLUMN IF EXISTS edge_id CASCADE;",
        "ALTER TABLE IF EXISTS part DROP COLUMN IF EXISTS part_type CASCADE;",
        "ALTER TABLE IF EXISTS part DROP CONSTRAINT IF EXISTS part_doc_id_part_type_part_key_key;",
        "ALTER TABLE IF EXISTS part DROP CONSTRAINT IF EXISTS part_part_type_check;",
        "ALTER TABLE IF EXISTS part DROP CONSTRAINT IF EXISTS ck_part_part_type;",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_part_doc_key ON part(doc_id, part_key);",
        "ALTER TABLE IF EXISTS part DROP CONSTRAINT IF EXISTS part_part_type_check;",
        "ALTER TABLE IF EXISTS part DROP CONSTRAINT IF EXISTS ck_part_part_type;",
        "ALTER TABLE IF EXISTS part DROP CONSTRAINT IF EXISTS fk_part_class;",
        "ALTER TABLE IF EXISTS part ADD CONSTRAINT fk_part_class FOREIGN KEY (class_id) REFERENCES object_class(class_id) ON DELETE CASCADE;",
        "ALTER TABLE IF EXISTS part DROP CONSTRAINT IF EXISTS fk_part_object;",
        "ALTER TABLE IF EXISTS part ADD CONSTRAINT fk_part_object FOREIGN KEY (object_id) REFERENCES object_instance(object_id) ON DELETE CASCADE;",
        "ALTER TABLE IF EXISTS part DROP CONSTRAINT IF EXISTS fk_part_relationship;",
        "ALTER TABLE IF EXISTS part ADD CONSTRAINT fk_part_relationship FOREIGN KEY (relationship_id) REFERENCES object_relationship(relationship_id) ON DELETE CASCADE;",
    ]

    with connection.cursor() as cursor:
        for sql in sql_statements:
            cursor.execute(sql)


def _migrate_legacy_attribute_schema(connection: psycopg2.extensions.connection) -> None:
    """Backfill legacy attribute table columns expected by current DDL/indexes."""

    if not _table_exists(connection, "attribute"):
        return

    sql_statements = [
        "ALTER TABLE IF EXISTS attribute ADD COLUMN IF NOT EXISTS valid_daterange daterange GENERATED ALWAYS AS (daterange(valid_from, valid_until, '[)')) STORED;",
    ]

    with connection.cursor() as cursor:
        for sql in sql_statements:
            cursor.execute(sql)


def _migrate_document_schema(connection: psycopg2.extensions.connection) -> None:
    """Backfill document constraints/indexes expected by current ingestion pipeline."""

    if not _table_exists(connection, "document"):
        return

    sql_statements = [
        "ALTER TABLE IF EXISTS document ADD COLUMN IF NOT EXISTS doc_name VARCHAR(512);",
        "ALTER TABLE IF EXISTS document ADD COLUMN IF NOT EXISTS doc_desc TEXT;",
        "ALTER TABLE IF EXISTS document ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'active';",
        "ALTER TABLE IF EXISTS document ADD COLUMN IF NOT EXISTS valid_from DATE;",
        "ALTER TABLE IF EXISTS document ADD COLUMN IF NOT EXISTS valid_until DATE;",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='document' AND column_name='source_path') AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='document' AND column_name='markdown_path') THEN ALTER TABLE document RENAME COLUMN source_path TO markdown_path; END IF; END $$;",
        "ALTER TABLE IF EXISTS document ADD COLUMN IF NOT EXISTS markdown_path VARCHAR(1024);",
        "UPDATE document SET markdown_path = NULL WHERE markdown_path = doc_path;",
        "UPDATE document SET markdown_path = metadata->>'markdown_path' WHERE markdown_path IS NULL AND metadata->>'markdown_path' IS NOT NULL;",
        "UPDATE document SET valid_from = COALESCE(valid_from, doc_date, entry_date::date, CURRENT_DATE);",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_document_doc_path_nonnull ON document(doc_path) WHERE doc_path IS NOT NULL;",
        "CREATE INDEX IF NOT EXISTS idx_docs_status ON document(status);",
        "DROP INDEX IF EXISTS idx_docs_source_path;",
        "CREATE INDEX IF NOT EXISTS idx_docs_markdown_path ON document(markdown_path);",
    ]

    with connection.cursor() as cursor:
        for sql in sql_statements:
            cursor.execute(sql)


def _migrate_object_instance_schema(connection: psycopg2.extensions.connection) -> None:
    if not _table_exists(connection, "object_instance"):
        return

    sql_statements = [
        "ALTER TABLE IF EXISTS object_instance ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'active';",
        "ALTER TABLE IF EXISTS object_instance ADD COLUMN IF NOT EXISTS valid_from DATE;",
        "ALTER TABLE IF EXISTS object_instance ADD COLUMN IF NOT EXISTS valid_until DATE;",
        "ALTER TABLE IF EXISTS object_instance ADD COLUMN IF NOT EXISTS canonical_full_name VARCHAR(256);",
        "UPDATE object_instance SET valid_from = COALESCE(valid_from, entry_date::date, CURRENT_DATE);",
        "UPDATE object_instance SET canonical_full_name = CASE WHEN LOWER(class_name) = 'person' THEN COALESCE(canonical_full_name, object_name) ELSE canonical_full_name END;",
        "ALTER TABLE IF EXISTS object_instance ALTER COLUMN valid_from SET DEFAULT CURRENT_DATE;",
        "ALTER TABLE IF EXISTS object_instance ALTER COLUMN valid_from SET NOT NULL;",
        "ALTER TABLE IF EXISTS object_instance ADD COLUMN IF NOT EXISTS valid_daterange daterange GENERATED ALWAYS AS (daterange(valid_from, valid_until, '[)')) STORED;",
        "CREATE INDEX IF NOT EXISTS idx_object_instance_status ON object_instance(status);",
        "CREATE INDEX IF NOT EXISTS idx_object_instance_canonical_full_name ON object_instance(canonical_full_name);",
        "CREATE INDEX IF NOT EXISTS idx_object_instance_canonical_full_name_lower ON object_instance (LOWER(canonical_full_name));",
        "CREATE INDEX IF NOT EXISTS idx_object_instance_canonical_full_name_trgm ON object_instance USING GIN (LOWER(canonical_full_name) gin_trgm_ops);",
        "CREATE INDEX IF NOT EXISTS idx_object_instance_valid_daterange ON object_instance USING GIST (valid_daterange);",
    ]

    with connection.cursor() as cursor:
        for sql in sql_statements:
            cursor.execute(sql)


def _migrate_object_relationship_schema(connection: psycopg2.extensions.connection) -> None:
    if not _table_exists(connection, "object_relationship"):
        return

    sql_statements = [
        "ALTER TABLE IF EXISTS object_relationship ADD COLUMN IF NOT EXISTS valid_from DATE;",
        "ALTER TABLE IF EXISTS object_relationship ADD COLUMN IF NOT EXISTS valid_until DATE;",
        "UPDATE object_relationship SET valid_from = COALESCE(valid_from, entry_date::date, CURRENT_DATE);",
        "ALTER TABLE IF EXISTS object_relationship ALTER COLUMN valid_from SET DEFAULT CURRENT_DATE;",
        "ALTER TABLE IF EXISTS object_relationship ALTER COLUMN valid_from SET NOT NULL;",
        "ALTER TABLE IF EXISTS object_relationship ADD COLUMN IF NOT EXISTS valid_daterange daterange GENERATED ALWAYS AS (daterange(valid_from, valid_until, '[)')) STORED;",
        "CREATE INDEX IF NOT EXISTS idx_object_relationship_valid_daterange ON object_relationship USING GIST (valid_daterange);",
    ]

    with connection.cursor() as cursor:
        for sql in sql_statements:
            cursor.execute(sql)


def create_tables(connection: psycopg2.extensions.connection, recreate: bool = False) -> None:
    ddl_statements = [
        create_extensions,
        create_document_table,
        create_document_table_cell_table,
        create_class_table,
        create_object_table,
        create_relationship_table,
        create_part_table,
        create_attribute_table,
        create_object_alias_table,
        create_resolution_ambiguity_queue_table,
        create_tx_match_index_pending_table,
        create_semantic_patterns_table,
        create_pattern_synonyms_table,
        create_semantic_terms_table,
        create_query_term_alias_table,
        create_solf_clauses_table,
        create_business_rules_table,
        create_solf_workflow_registry_table,
        create_solf_workflow_versions_table,
        create_solf_workflow_steps_table,
        create_business_rule_workflow_links_table,
        create_workflow_resource_alias_table,
        create_workflow_extension_tables,
        create_workflow_pipeline_run_tables,
        create_workflow_pipeline_run_document_links_table,
    ]

    drop_statements = [
        "DROP TABLE IF EXISTS business_rule_workflow_links CASCADE;",
        "DROP TABLE IF EXISTS solf_workflow_steps CASCADE;",
        "DROP TABLE IF EXISTS solf_workflow_versions CASCADE;",
        "DROP TABLE IF EXISTS solf_workflow_registry CASCADE;",
        "DROP TABLE IF EXISTS workflow_extension_attachment CASCADE;",
        "DROP TABLE IF EXISTS workflow_extension_rule CASCADE;",
        "DROP TABLE IF EXISTS workflow_extension_version CASCADE;",
        "DROP TABLE IF EXISTS workflow_extension_pack CASCADE;",
        "DROP TABLE IF EXISTS workflow_pipeline_run_document_link CASCADE;",
        "DROP TABLE IF EXISTS workflow_resource_alias CASCADE;",
        "DROP TABLE IF EXISTS pattern_synonyms CASCADE;",
        "DROP TABLE IF EXISTS semantic_patterns CASCADE;",
        "DROP TABLE IF EXISTS semantic_terms CASCADE;",
        "DROP TABLE IF EXISTS query_term_alias CASCADE;",
        "DROP TABLE IF EXISTS solf_clauses CASCADE;",
        "DROP TABLE IF EXISTS business_rules CASCADE;",
        "DROP TABLE IF EXISTS object_alias CASCADE;",
        "DROP TABLE IF EXISTS resolution_ambiguity_queue CASCADE;",
        "DROP TABLE IF EXISTS tx_match_index_pending CASCADE;",
        "DROP TABLE IF EXISTS document_table_cell CASCADE;",
        "DROP TABLE IF EXISTS part CASCADE;",
        "DROP TABLE IF EXISTS object_relationship CASCADE;",
        "DROP TABLE IF EXISTS object_instance CASCADE;",
        "DROP TABLE IF EXISTS object_class CASCADE;",
        "DROP TABLE IF EXISTS document CASCADE;",
    ]

    try:
        with connection.cursor() as cursor:
            if recreate:
                for sql in drop_statements:
                    cursor.execute(sql)

        if not recreate:
            _migrate_legacy_object_class_schema(connection)
            _migrate_legacy_part_schema(connection)
            _migrate_legacy_attribute_schema(connection)
            _migrate_object_instance_schema(connection)
            _migrate_object_relationship_schema(connection)
            _migrate_document_schema(connection)

        with connection.cursor() as cursor:
            for sql in ddl_statements:
                cursor.execute(sql)
            if not recreate:
                cursor.execute("ALTER TABLE IF EXISTS attribute DROP CONSTRAINT IF EXISTS attribute_src_type_check;")
                cursor.execute(
                    """
                    ALTER TABLE IF EXISTS attribute
                    ADD CONSTRAINT attribute_src_type_check
                    CHECK (src_type IN ('part','document','class','object','relationship'));
                    """
                )
        connection.commit()
        logging.info("Object schema tables created successfully")
    except Exception:
        connection.rollback()
        logging.exception("Failed while creating object schema tables")
        raise


# ---------------------------------------------------------------------------
# Pipeline run DB helpers
# ---------------------------------------------------------------------------

def create_pipeline_run(
    connection: psycopg2.extensions.connection,
    workflow_version_id: int | None,
    workflow_key: str | None,
    input_context: dict[str, Any],
    started_by: str | None = None,
) -> int:
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_pipeline_run
                (workflow_version_id, workflow_key, run_status, input_context, current_context,
                 output_context, started_by, started_at, created_at, modified_at)
            VALUES (%s, %s, 'pending', %s, %s, '{}'::jsonb, %s, NOW(), NOW(), NOW())
            RETURNING run_id
            """,
            (
                int(workflow_version_id) if workflow_version_id is not None else None,
                str(workflow_key or "").strip() or None,
                Json(input_context or {}),
                Json(input_context or {}),
                str(started_by or "").strip() or None,
            ),
        )
        run_id = int(cur.fetchone()[0])
    connection.commit()
    return run_id


def get_pipeline_run(
    connection: psycopg2.extensions.connection,
    run_id: int,
) -> dict[str, Any] | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, workflow_version_id, workflow_key, run_status, paused_at_step_key,
                   input_context, current_context, output_context, started_by,
                   started_at, finished_at, created_at, modified_at
            FROM workflow_pipeline_run
            WHERE run_id = %s
            """,
            (int(run_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "run_id": int(row[0]),
        "workflow_version_id": row[1],
        "workflow_key": row[2],
        "run_status": row[3],
        "paused_at_step_key": row[4],
        "input_context": row[5] or {},
        "current_context": row[6] or {},
        "output_context": row[7] or {},
        "started_by": row[8],
        "started_at": row[9],
        "finished_at": row[10],
        "created_at": row[11],
        "modified_at": row[12],
    }


def update_pipeline_run_status(
    connection: psycopg2.extensions.connection,
    run_id: int,
    run_status: str,
    current_context: dict[str, Any] | None = None,
    output_context: dict[str, Any] | None = None,
    paused_at_step_key: str | None = None,
    finished: bool = False,
) -> None:
    fields = ["run_status = %s", "modified_at = NOW()"]
    params: list[Any] = [run_status]

    if current_context is not None:
        fields.append("current_context = %s")
        params.append(Json(current_context))

    if output_context is not None:
        fields.append("output_context = %s")
        params.append(Json(output_context))

    if paused_at_step_key is not None:
        fields.append("paused_at_step_key = %s")
        params.append(str(paused_at_step_key))
    elif run_status in {"completed", "failed", "cancelled"}:
        fields.append("paused_at_step_key = NULL")

    if finished:
        fields.append("finished_at = NOW()")

    params.append(int(run_id))
    with connection.cursor() as cur:
        cur.execute(
            f"UPDATE workflow_pipeline_run SET {', '.join(fields)} WHERE run_id = %s",
            params,
        )
    connection.commit()


def list_pipeline_runs(
    connection: psycopg2.extensions.connection,
    run_status: str | None = None,
    workflow_key: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, workflow_version_id, workflow_key, run_status, paused_at_step_key,
                   input_context, current_context, output_context, started_by,
                   started_at, finished_at, created_at, modified_at
            FROM workflow_pipeline_run
            WHERE (%s IS NULL OR run_status = %s)
              AND (%s IS NULL OR workflow_key = %s)
            ORDER BY created_at DESC, run_id DESC
            LIMIT %s
            """,
            (run_status, run_status, workflow_key, workflow_key, int(limit)),
        )
        rows = cur.fetchall()
    return [
        {
            "run_id": int(r[0]),
            "workflow_version_id": r[1],
            "workflow_key": r[2],
            "run_status": r[3],
            "paused_at_step_key": r[4],
            "input_context": r[5] or {},
            "current_context": r[6] or {},
            "output_context": r[7] or {},
            "started_by": r[8],
            "started_at": r[9],
            "finished_at": r[10],
            "created_at": r[11],
            "modified_at": r[12],
        }
        for r in rows
    ]


def upsert_pipeline_run_step(
    connection: psycopg2.extensions.connection,
    run_id: int,
    step_key: str,
    step_order: int,
    step_status: str,
    input_snapshot: dict[str, Any] | None = None,
    output_snapshot: dict[str, Any] | None = None,
    pause_reason: str | None = None,
    interaction_prompt: str | None = None,
    missing_data_desc: str | None = None,
    required_doc_types: list[str] | None = None,
    user_response: dict[str, Any] | None = None,
    doc_refs: list[Any] | None = None,
    error_message: str | None = None,
    increment_attempt: bool = False,
) -> int:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT step_run_id, attempt_count FROM workflow_pipeline_run_step WHERE run_id=%s AND step_key=%s",
            (int(run_id), str(step_key)),
        )
        existing = cur.fetchone()

        if existing is None:
            cur.execute(
                """
                INSERT INTO workflow_pipeline_run_step
                    (run_id, step_key, step_order, step_status,
                     input_snapshot, output_snapshot, pause_reason,
                     interaction_prompt, missing_data_desc, required_doc_types,
                     user_response, doc_refs, error_message, attempt_count,
                     started_at, completed_at, created_at, modified_at)
                VALUES (%s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        NOW(), NULL, NOW(), NOW())
                RETURNING step_run_id
                """,
                (
                    int(run_id), str(step_key), int(step_order), str(step_status),
                    Json(input_snapshot or {}), Json(output_snapshot or {}), pause_reason,
                    interaction_prompt, missing_data_desc, Json(required_doc_types or []),
                    Json(user_response or {}), Json(doc_refs or []), error_message, 1,
                ),
            )
            step_run_id = int(cur.fetchone()[0])
        else:
            step_run_id = int(existing[0])
            old_attempts = int(existing[1] or 0)
            new_attempts = (old_attempts + 1) if increment_attempt else old_attempts

            completed_clause = "completed_at = NOW()," if step_status in {"completed", "failed", "skipped"} else ""
            cur.execute(
                f"""
                UPDATE workflow_pipeline_run_step
                SET step_status = %s,
                    {completed_clause}
                    output_snapshot = COALESCE(%s, output_snapshot),
                    pause_reason = %s,
                    interaction_prompt = %s,
                    missing_data_desc = %s,
                    required_doc_types = COALESCE(%s, required_doc_types),
                    user_response = COALESCE(%s, user_response),
                    doc_refs = COALESCE(%s, doc_refs),
                    error_message = %s,
                    attempt_count = %s,
                    modified_at = NOW()
                WHERE step_run_id = %s
                """,
                (
                    str(step_status),
                    Json(output_snapshot) if output_snapshot is not None else None,
                    pause_reason,
                    interaction_prompt,
                    missing_data_desc,
                    Json(required_doc_types) if required_doc_types is not None else None,
                    Json(user_response) if user_response is not None else None,
                    Json(doc_refs) if doc_refs is not None else None,
                    error_message,
                    new_attempts,
                    step_run_id,
                ),
            )
    connection.commit()
    return step_run_id


def get_pipeline_run_steps(
    connection: psycopg2.extensions.connection,
    run_id: int,
) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT step_run_id, run_id, step_key, step_order, step_status,
                   pause_reason, interaction_prompt, missing_data_desc,
                   required_doc_types, input_snapshot, output_snapshot,
                   user_response, doc_refs, error_message, attempt_count,
                   started_at, completed_at, created_at, modified_at
            FROM workflow_pipeline_run_step
            WHERE run_id = %s
            ORDER BY step_order, step_run_id
            """,
            (int(run_id),),
        )
        rows = cur.fetchall()
    return [
        {
            "step_run_id": int(r[0]),
            "run_id": int(r[1]),
            "step_key": r[2],
            "step_order": int(r[3]),
            "step_status": r[4],
            "pause_reason": r[5],
            "interaction_prompt": r[6],
            "missing_data_desc": r[7],
            "required_doc_types": r[8] or [],
            "input_snapshot": r[9] or {},
            "output_snapshot": r[10] or {},
            "user_response": r[11] or {},
            "doc_refs": r[12] or [],
            "error_message": r[13],
            "attempt_count": int(r[14] or 0),
            "started_at": r[15],
            "completed_at": r[16],
            "created_at": r[17],
            "modified_at": r[18],
        }
        for r in rows
    ]


def supply_pipeline_step_response(
    connection: psycopg2.extensions.connection,
    run_id: int,
    step_key: str,
    user_response: dict[str, Any] | None = None,
    doc_refs: list[Any] | None = None,
) -> bool:
    """Store user-supplied response or new document refs for a paused step, reset to pending."""
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_pipeline_run_step
            SET step_status = 'pending',
                pause_reason = NULL,
                user_response = COALESCE(%s, user_response),
                doc_refs = COALESCE(%s, doc_refs),
                modified_at = NOW()
            WHERE run_id = %s AND step_key = %s AND step_status = 'paused'
            """,
            (
                Json(user_response) if user_response is not None else None,
                Json(doc_refs) if doc_refs is not None else None,
                int(run_id),
                str(step_key),
            ),
        )
        updated = int(cur.rowcount or 0)
    connection.commit()
    return updated > 0


def _normalize_document_id(doc_ref: Any) -> int | None:
    if isinstance(doc_ref, dict):
        candidate = doc_ref.get("doc_id")
    else:
        candidate = doc_ref

    if candidate is None:
        return None
    try:
        value = int(candidate)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def link_pipeline_run_documents(
    connection: psycopg2.extensions.connection,
    run_id: int,
    doc_refs: list[Any] | None,
    source: str = "system",
    step_key: str | None = None,
    linked_by: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    refs = list(doc_refs or [])
    if not refs:
        return 0

    normalized_source = str(source or "system").strip().lower() or "system"
    if normalized_source not in {"input_context", "resume", "step_response", "system"}:
        normalized_source = "system"

    normalized_step_key = str(step_key or "").strip()

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT workflow_version_id, workflow_key
            FROM workflow_pipeline_run
            WHERE run_id = %s
            """,
            (int(run_id),),
        )
        run_row = cur.fetchone()
        if run_row is None:
            return 0

        workflow_version_id = run_row[0]
        workflow_key = run_row[1]

        inserted = 0
        for ref in refs:
            document_id = _normalize_document_id(ref)
            if document_id is None:
                continue

            cur.execute(
                """
                INSERT INTO workflow_pipeline_run_document_link (
                    run_id, workflow_version_id, workflow_key, document_id,
                    source, step_key, linked_by, metadata, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (run_id, document_id, source, step_key)
                DO NOTHING
                """,
                (
                    int(run_id),
                    int(workflow_version_id) if workflow_version_id is not None else None,
                    workflow_key,
                    int(document_id),
                    normalized_source,
                    normalized_step_key,
                    str(linked_by or "").strip() or None,
                    Json(metadata or {}),
                ),
            )
            inserted += int(cur.rowcount or 0)

    connection.commit()
    return inserted


def list_pipeline_run_document_links(
    connection: psycopg2.extensions.connection,
    run_id: int,
) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT link_id, run_id, workflow_version_id, workflow_key, document_id,
                   source, step_key, linked_by, metadata, created_at
            FROM workflow_pipeline_run_document_link
            WHERE run_id = %s
            ORDER BY created_at, link_id
            """,
            (int(run_id),),
        )
        rows = cur.fetchall()

    return [
        {
            "link_id": int(row[0]),
            "run_id": int(row[1]),
            "workflow_version_id": int(row[2]) if row[2] is not None else None,
            "workflow_key": row[3],
            "document_id": int(row[4]),
            "source": row[5],
            "step_key": row[6],
            "linked_by": row[7],
            "metadata": row[8] or {},
            "created_at": row[9],
        }
        for row in rows
    ]


def list_pipeline_runs_by_document(
    connection: psycopg2.extensions.connection,
    document_id: int,
    run_status: str | None = None,
    workflow_key: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT r.run_id, r.workflow_version_id, r.workflow_key, r.run_status,
                            r.paused_at_step_key, r.input_context, r.current_context,
                            r.output_context, r.started_by, r.started_at, r.finished_at,
                            r.created_at, r.modified_at
            FROM workflow_pipeline_run r
            JOIN workflow_pipeline_run_document_link l ON l.run_id = r.run_id
            WHERE l.document_id = %s
              AND (%s IS NULL OR r.run_status = %s)
              AND (%s IS NULL OR r.workflow_key = %s)
            ORDER BY r.created_at DESC, r.run_id DESC
            LIMIT %s
            """,
            (int(document_id), run_status, run_status, workflow_key, workflow_key, int(limit)),
        )
        rows = cur.fetchall()

    return [
        {
            "run_id": int(r[0]),
            "workflow_version_id": r[1],
            "workflow_key": r[2],
            "run_status": r[3],
            "paused_at_step_key": r[4],
            "input_context": r[5] or {},
            "current_context": r[6] or {},
            "output_context": r[7] or {},
            "started_by": r[8],
            "started_at": r[9],
            "finished_at": r[10],
            "created_at": r[11],
            "modified_at": r[12],
        }
        for r in rows
    ]


def list_latest_pipeline_runs_by_document_workflow(
    connection: psycopg2.extensions.connection,
    document_id: int,
    workflow_key: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return latest run per workflow (or workflow version fallback) for a document."""
    with connection.cursor() as cur:
        cur.execute(
            """
            WITH ranked AS (
                SELECT
                    COALESCE(NULLIF(r.workflow_key, ''), CONCAT('__workflow_version__', COALESCE(r.workflow_version_id::text, 'unknown'))) AS workflow_group_key,
                    r.run_id,
                    r.workflow_version_id,
                    r.workflow_key,
                    r.run_status,
                    r.paused_at_step_key,
                    r.input_context,
                    r.current_context,
                    r.output_context,
                    r.started_by,
                    r.started_at,
                    r.finished_at,
                    r.created_at,
                    r.modified_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(NULLIF(r.workflow_key, ''), CONCAT('__workflow_version__', COALESCE(r.workflow_version_id::text, 'unknown')))
                        ORDER BY r.created_at DESC, r.run_id DESC
                    ) AS rn
                FROM workflow_pipeline_run r
                JOIN workflow_pipeline_run_document_link l ON l.run_id = r.run_id
                WHERE l.document_id = %s
                  AND (%s IS NULL OR r.workflow_key = %s)
            )
            SELECT workflow_group_key, run_id, workflow_version_id, workflow_key, run_status,
                   paused_at_step_key, input_context, current_context, output_context,
                   started_by, started_at, finished_at, created_at, modified_at
            FROM ranked
            WHERE rn = 1
            ORDER BY created_at DESC, run_id DESC
            LIMIT %s
            """,
            (int(document_id), workflow_key, workflow_key, int(limit)),
        )
        rows = cur.fetchall()

    return [
        {
            "workflow_group_key": r[0],
            "run_id": int(r[1]),
            "workflow_version_id": r[2],
            "workflow_key": r[3],
            "run_status": r[4],
            "paused_at_step_key": r[5],
            "input_context": r[6] or {},
            "current_context": r[7] or {},
            "output_context": r[8] or {},
            "started_by": r[9],
            "started_at": r[10],
            "finished_at": r[11],
            "created_at": r[12],
            "modified_at": r[13],
        }
        for r in rows
    ]


_DB_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def list_exportable_tables(
    connection: psycopg2.extensions.connection,
    schema_name: str = "public",
) -> list[str]:
    schema = str(schema_name or "public").strip().lower() or "public"
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname = %s
            ORDER BY tablename
            """,
            (schema,),
        )
        rows = cur.fetchall()
    return [str(row[0]) for row in rows if row and row[0]]


def export_tables_to_csv_bundle(
    connection: psycopg2.extensions.connection,
    *,
    table_names: list[str] | None = None,
    include_all: bool = False,
    schema_name: str = "public",
    output_file_name: str | None = None,
) -> dict[str, Any]:
    """Export one, multiple, or all tables as CSV files.

    - Single table: writes one CSV and returns its path.
    - Multiple/all tables: writes CSVs and a ZIP bundle for migration portability.
    """
    schema = str(schema_name or "public").strip().lower() or "public"
    if not _DB_IDENTIFIER_PATTERN.match(schema):
        raise ValueError(f"Invalid schema_name: {schema}")

    available = list_exportable_tables(connection, schema_name=schema)
    available_set = {item.lower() for item in available}

    requested = [str(item or "").strip().lower() for item in (table_names or []) if str(item or "").strip()]

    if include_all or not requested:
        selected = list(available)
    else:
        selected = []
        for table in requested:
            if not _DB_IDENTIFIER_PATTERN.match(table):
                raise ValueError(f"Invalid table name: {table}")
            if table not in available_set:
                raise ValueError(f"Table not found in schema '{schema}': {table}")
            if table not in selected:
                selected.append(table)

    if not selected:
        raise ValueError(f"No tables available for export in schema '{schema}'")

    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    export_root = Path(__file__).resolve().parent / "generated" / "db_exports"
    run_dir = export_root / f"db_export_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    exported_files: list[dict[str, Any]] = []
    for table in selected:
        csv_path = run_dir / f"{table}.csv"
        table_ref = f'"{schema}"."{table}"'
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            with connection.cursor() as cur:
                cur.copy_expert(
                    f"COPY {table_ref} TO STDOUT WITH (FORMAT CSV, HEADER TRUE)",
                    handle,
                )
        exported_files.append(
            {
                "table": table,
                "schema": schema,
                "file_path": str(csv_path),
                "file_name": csv_path.name,
                "file_size_bytes": csv_path.stat().st_size,
            }
        )

    bundle_path: Path | None = None
    if len(exported_files) > 1:
        bundle_base = str(output_file_name or "").strip()
        if bundle_base:
            bundle_base = re.sub(r"[^A-Za-z0-9._-]+", "_", bundle_base)
            if not bundle_base.lower().endswith(".zip"):
                bundle_base = f"{bundle_base}.zip"
        else:
            bundle_base = f"db_export_{stamp}.zip"
        bundle_path = run_dir / bundle_base
        with zipfile.ZipFile(bundle_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for item in exported_files:
                zf.write(item["file_path"], arcname=item["file_name"])

    return {
        "schema_name": schema,
        "include_all": bool(include_all),
        "selected_tables": selected,
        "table_count": len(selected),
        "export_dir": str(run_dir),
        "files": exported_files,
        "bundle_path": str(bundle_path) if bundle_path is not None else None,
    }


def _load_fk_edges(
    connection: psycopg2.extensions.connection,
    schema_name: str,
) -> list[tuple[str, str]]:
    """Return FK dependency edges as (parent_table, child_table)."""
    sql = """
    SELECT
        p.relname AS parent_table,
        c.relname AS child_table
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace nc ON nc.oid = c.relnamespace
    JOIN pg_class p ON p.oid = con.confrelid
    JOIN pg_namespace np ON np.oid = p.relnamespace
    WHERE con.contype = 'f'
      AND nc.nspname = %s
      AND np.nspname = %s
    """
    with connection.cursor() as cur:
        cur.execute(sql, (schema_name, schema_name))
        rows = cur.fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def _topological_import_order(
    selected_tables: list[str],
    fk_edges: list[tuple[str, str]],
) -> list[str]:
    selected = [str(t) for t in selected_tables]
    selected_set = set(selected)
    indegree: dict[str, int] = {t: 0 for t in selected}
    graph: dict[str, list[str]] = {t: [] for t in selected}

    for parent, child in fk_edges:
        if parent not in selected_set or child not in selected_set:
            continue
        graph[parent].append(child)
        indegree[child] += 1

    queue = sorted([t for t, deg in indegree.items() if deg == 0])
    ordered: list[str] = []
    while queue:
        node = queue.pop(0)
        ordered.append(node)
        for child in sorted(graph.get(node, [])):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if len(ordered) < len(selected):
        remaining = sorted([t for t in selected if t not in ordered])
        ordered.extend(remaining)
    return ordered


def import_tables_from_csv_bundle(
    connection: psycopg2.extensions.connection,
    *,
    source_zip_path: str | None = None,
    source_dir: str | None = None,
    table_names: list[str] | None = None,
    include_all: bool = False,
    schema_name: str = "public",
    truncate_before_import: bool = False,
    continue_on_error: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import CSV data into IDMS tables from a zip bundle or directory.

    One of source_zip_path or source_dir must be provided.
    """
    schema = str(schema_name or "public").strip().lower() or "public"
    if not _DB_IDENTIFIER_PATTERN.match(schema):
        raise ValueError(f"Invalid schema_name: {schema}")

    if not source_zip_path and not source_dir:
        raise ValueError("source_zip_path or source_dir is required")

    db_tables = list_exportable_tables(connection, schema_name=schema)
    db_table_set = {t.lower() for t in db_tables}

    source_csv_map: dict[str, str] = {}
    source_kind = "directory"
    zip_path_obj: Path | None = None
    dir_path_obj: Path | None = None

    if source_zip_path:
        source_kind = "zip"
        zip_path_obj = Path(str(source_zip_path)).resolve()
        if not zip_path_obj.exists() or not zip_path_obj.is_file():
            raise ValueError(f"source_zip_path not found: {zip_path_obj}")
        with zipfile.ZipFile(zip_path_obj, mode="r") as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".csv"):
                    continue
                table = Path(name).name[:-4].strip().lower()
                if table and _DB_IDENTIFIER_PATTERN.match(table):
                    source_csv_map[table] = name
    else:
        dir_path_obj = Path(str(source_dir)).resolve()
        if not dir_path_obj.exists() or not dir_path_obj.is_dir():
            raise ValueError(f"source_dir not found: {dir_path_obj}")
        for item in sorted(dir_path_obj.iterdir()):
            if not item.is_file() or item.suffix.lower() != ".csv":
                continue
            table = item.stem.strip().lower()
            if table and _DB_IDENTIFIER_PATTERN.match(table):
                source_csv_map[table] = str(item)

    if not source_csv_map:
        raise ValueError("No CSV files found in source")

    requested = [str(item or "").strip().lower() for item in (table_names or []) if str(item or "").strip()]
    if include_all or not requested:
        selected = sorted([table for table in source_csv_map.keys() if table in db_table_set])
    else:
        selected = []
        for table in requested:
            if not _DB_IDENTIFIER_PATTERN.match(table):
                raise ValueError(f"Invalid table name: {table}")
            if table not in db_table_set:
                raise ValueError(f"Table not found in DB schema '{schema}': {table}")
            if table not in source_csv_map:
                raise ValueError(f"CSV for table '{table}' not found in source")
            if table not in selected:
                selected.append(table)

    if not selected:
        raise ValueError("No matching DB tables found for import")

    fk_edges = _load_fk_edges(connection, schema_name=schema)
    import_order = _topological_import_order(selected, fk_edges)

    if dry_run:
        return {
            "schema_name": schema,
            "source_kind": source_kind,
            "source_zip_path": str(zip_path_obj) if zip_path_obj else None,
            "source_dir": str(dir_path_obj) if dir_path_obj else None,
            "selected_tables": selected,
            "import_order": import_order,
            "table_count": len(selected),
            "dry_run": True,
        }

    imported: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if truncate_before_import:
        quoted = [f'"{schema}"."{table}"' for table in selected]
        with connection.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {', '.join(quoted)} RESTART IDENTITY CASCADE")
        connection.commit()

    try:
        if source_kind == "zip" and zip_path_obj is not None:
            with zipfile.ZipFile(zip_path_obj, mode="r") as zf:
                for table in import_order:
                    member_name = source_csv_map.get(table)
                    if not member_name:
                        continue
                    try:
                        with zf.open(member_name, mode="r") as raw:
                            with TextIOWrapper(raw, encoding="utf-8", newline="") as text_stream:
                                with connection.cursor() as cur:
                                    cur.copy_expert(
                                        f'COPY "{schema}"."{table}" FROM STDIN WITH (FORMAT CSV, HEADER TRUE)',
                                        text_stream,
                                    )
                        connection.commit()
                        imported.append({"table": table, "status": "imported", "source": member_name})
                    except Exception as exc:
                        connection.rollback()
                        errors.append({"table": table, "error": str(exc)})
                        if not continue_on_error:
                            break
        else:
            for table in import_order:
                path_text = source_csv_map.get(table)
                if not path_text:
                    continue
                csv_path = Path(path_text)
                try:
                    with csv_path.open("r", encoding="utf-8", newline="") as handle:
                        with connection.cursor() as cur:
                            cur.copy_expert(
                                f'COPY "{schema}"."{table}" FROM STDIN WITH (FORMAT CSV, HEADER TRUE)',
                                handle,
                            )
                    connection.commit()
                    imported.append({"table": table, "status": "imported", "source": str(csv_path)})
                except Exception as exc:
                    connection.rollback()
                    errors.append({"table": table, "error": str(exc)})
                    if not continue_on_error:
                        break
    except Exception:
        connection.rollback()
        raise

    success = len(errors) == 0
    return {
        "schema_name": schema,
        "source_kind": source_kind,
        "source_zip_path": str(zip_path_obj) if zip_path_obj else None,
        "source_dir": str(dir_path_obj) if dir_path_obj else None,
        "selected_tables": selected,
        "import_order": import_order,
        "table_count": len(selected),
        "imported_count": len(imported),
        "error_count": len(errors),
        "imported": imported,
        "errors": errors,
        "success": success,
        "dry_run": False,
    }


if __name__ == "__main__":
    connection = get_connection()
    try:
        create_tables(connection, recreate=False)
    finally:
        connection.close()
