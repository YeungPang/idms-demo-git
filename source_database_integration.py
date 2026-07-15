from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable

import psycopg2
from psycopg2.extras import RealDictCursor

import object_db


@dataclass(frozen=True)
class SourceTableInfo:
    schema_name: str
    table_name: str
    object_kind: str
    fingerprint: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class SourceRowMappingResult:
    source_pk_value: str
    action: str
    target_object_id: int | None
    source_row_hash: str
    source_row_fingerprint: str
    source_row_updated_at: str | None


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_identifier(value: Any) -> str:
    return str(value or "").strip().lower()


def _coerce_iso_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt_value = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt_value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value.isoformat()


def discover_source_tables(
    connection: psycopg2.extensions.connection,
    schema_names: Iterable[str] | None = None,
    include_views: bool = False,
) -> list[SourceTableInfo]:
    schema_filter = [str(item).strip() for item in (schema_names or []) if str(item).strip()]
    object_types = ["BASE TABLE"]
    if include_views:
        object_types.append("VIEW")

    sql = """
    SELECT table_schema, table_name, table_type
    FROM information_schema.tables
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
      AND table_type = ANY(%s)
    """
    params: list[Any] = [object_types]
    if schema_filter:
        sql += " AND table_schema = ANY(%s)"
        params.append(schema_filter)
    sql += " ORDER BY table_schema, table_name"

    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(sql, tuple(params))
        tables = cursor.fetchall() or []

    results: list[SourceTableInfo] = []
    for table in tables:
        schema_name = _normalize_identifier(table.get("table_schema"))
        table_name = _normalize_identifier(table.get("table_name"))
        object_kind = _normalize_identifier(table.get("table_type")) or "table"

        column_sql = """
        SELECT column_name, data_type, is_nullable, ordinal_position, column_default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(column_sql, (schema_name, table_name))
            columns = cursor.fetchall() or []

        column_signature = [
            {
                "column_name": _normalize_identifier(column.get("column_name")),
                "data_type": _normalize_identifier(column.get("data_type")),
                "is_nullable": str(column.get("is_nullable") or "").strip().lower(),
                "ordinal_position": int(column.get("ordinal_position") or 0),
                "column_default": str(column.get("column_default") or "").strip(),
            }
            for column in columns
        ]
        fingerprint = _sha256_text(_stable_json({"schema": schema_name, "table": table_name, "kind": object_kind, "columns": column_signature}))
        results.append(
            SourceTableInfo(
                schema_name=schema_name,
                table_name=table_name,
                object_kind=object_kind,
                fingerprint=fingerprint,
                metadata={"columns": column_signature},
            )
        )

    return results


def upsert_source_table_inventory(
    metadata_connection: psycopg2.extensions.connection,
    source_id: int,
    table_info: SourceTableInfo,
) -> None:
    sql = """
    INSERT INTO source_schema_inventory (
        source_id,
        schema_name,
        table_name,
        object_kind,
        fingerprint,
        metadata
    )
    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
    ON CONFLICT (source_id, schema_name, table_name)
    DO UPDATE SET
        object_kind = EXCLUDED.object_kind,
        fingerprint = EXCLUDED.fingerprint,
        metadata = EXCLUDED.metadata,
        discovered_at = NOW()
    """
    with metadata_connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                int(source_id),
                table_info.schema_name,
                table_info.table_name,
                table_info.object_kind,
                table_info.fingerprint,
                _stable_json(table_info.metadata),
            ),
        )
    metadata_connection.commit()


def register_source_database(
    metadata_connection: psycopg2.extensions.connection,
    *,
    source_key: str,
    source_name: str,
    db_name: str,
    db_host: str | None = None,
    db_port: int | None = None,
    db_user: str | None = None,
    db_schema: str | None = None,
    connection_hint: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sql = """
    INSERT INTO source_database_registry (
        source_key,
        source_name,
        db_host,
        db_port,
        db_name,
        db_user,
        db_schema,
        connection_hint,
        metadata,
        updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
    ON CONFLICT (source_key)
    DO UPDATE SET
        source_name = EXCLUDED.source_name,
        db_host = EXCLUDED.db_host,
        db_port = EXCLUDED.db_port,
        db_name = EXCLUDED.db_name,
        db_user = EXCLUDED.db_user,
        db_schema = EXCLUDED.db_schema,
        connection_hint = EXCLUDED.connection_hint,
        metadata = COALESCE(source_database_registry.metadata, '{}'::jsonb) || EXCLUDED.metadata,
        status = 'active',
        updated_at = NOW()
    RETURNING source_id, source_key, source_name, db_host, db_port, db_name, db_schema, connection_hint, status, metadata, created_at, updated_at
    """

    with metadata_connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                str(source_key or "").strip(),
                str(source_name or "").strip(),
                str(db_name or "").strip(),
                db_host,
                db_port,
                db_user,
                db_schema,
                connection_hint,
                _stable_json(metadata or {}),
            ),
        )
        row = cursor.fetchone()
    metadata_connection.commit()

    return {
        "source_id": int(row[0]),
        "source_key": row[1],
        "source_name": row[2],
        "db_host": row[3],
        "db_port": row[4],
        "db_name": row[5],
        "db_user": row[6],
        "db_schema": row[7],
        "connection_hint": row[8],
        "status": row[9],
        "metadata": row[10] if isinstance(row[10], dict) else {},
        "created_at": row[11].isoformat() if row[11] is not None else None,
        "updated_at": row[12].isoformat() if row[12] is not None else None,
    }


def load_source_mapping(
    metadata_connection: psycopg2.extensions.connection,
    source_id: int,
    schema_name: str,
    table_name: str,
    source_pk_value: str,
    target_class_name: str,
) -> dict[str, Any] | None:
    sql = """
    SELECT mapping_id, source_id, schema_name, table_name, source_pk_value, target_class_name,
           target_object_id, target_metadata, source_row_hash, source_row_updated_at, source_row_fingerprint,
           mapped_at, updated_at
    FROM source_entity_mapping
    WHERE source_id = %s
      AND schema_name = %s
      AND table_name = %s
      AND source_pk_value = %s
      AND target_class_name = %s
    LIMIT 1
    """
    with metadata_connection.cursor() as cursor:
        cursor.execute(sql, (int(source_id), schema_name, table_name, str(source_pk_value), target_class_name))
        row = cursor.fetchone()

    if row is None:
        return None

    return {
        "mapping_id": int(row[0]),
        "source_id": int(row[1]),
        "schema_name": row[2],
        "table_name": row[3],
        "source_pk_value": row[4],
        "target_class_name": row[5],
        "target_object_id": row[6],
        "target_metadata": row[7] if isinstance(row[7], dict) else {},
        "source_row_hash": row[8],
        "source_row_updated_at": _coerce_iso_datetime(row[9]),
        "source_row_fingerprint": row[10],
        "mapped_at": row[11].isoformat() if row[11] is not None else None,
        "updated_at": row[12].isoformat() if row[12] is not None else None,
    }


def upsert_source_mapping(
    metadata_connection: psycopg2.extensions.connection,
    *,
    source_id: int,
    schema_name: str,
    table_name: str,
    source_pk_value: str,
    target_class_name: str,
    target_object_id: int | None,
    target_metadata: dict[str, Any],
    source_row_hash: str,
    source_row_updated_at: str | None,
    source_row_fingerprint: str,
) -> dict[str, Any]:
    sql = """
    INSERT INTO source_entity_mapping (
        source_id,
        schema_name,
        table_name,
        source_pk_value,
        target_class_name,
        target_object_id,
        target_metadata,
        source_row_hash,
        source_row_updated_at,
        source_row_fingerprint,
        mapped_at,
        updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, NOW(), NOW())
    ON CONFLICT (source_id, schema_name, table_name, source_pk_value, target_class_name)
    DO UPDATE SET
        target_object_id = EXCLUDED.target_object_id,
        target_metadata = COALESCE(source_entity_mapping.target_metadata, '{}'::jsonb) || EXCLUDED.target_metadata,
        source_row_hash = EXCLUDED.source_row_hash,
        source_row_updated_at = EXCLUDED.source_row_updated_at,
        source_row_fingerprint = EXCLUDED.source_row_fingerprint,
        updated_at = NOW()
    RETURNING mapping_id, source_id, schema_name, table_name, source_pk_value, target_class_name,
              target_object_id, target_metadata, source_row_hash, source_row_updated_at,
              source_row_fingerprint, mapped_at, updated_at
    """
    with metadata_connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                int(source_id),
                schema_name,
                table_name,
                str(source_pk_value),
                target_class_name,
                target_object_id,
                _stable_json(target_metadata or {}),
                source_row_hash,
                source_row_updated_at,
                source_row_fingerprint,
            ),
        )
        row = cursor.fetchone()
    metadata_connection.commit()

    return {
        "mapping_id": int(row[0]),
        "source_id": int(row[1]),
        "schema_name": row[2],
        "table_name": row[3],
        "source_pk_value": row[4],
        "target_class_name": row[5],
        "target_object_id": row[6],
        "target_metadata": row[7] if isinstance(row[7], dict) else {},
        "source_row_hash": row[8],
        "source_row_updated_at": _coerce_iso_datetime(row[9]),
        "source_row_fingerprint": row[10],
        "mapped_at": row[11].isoformat() if row[11] is not None else None,
        "updated_at": row[12].isoformat() if row[12] is not None else None,
    }


def _row_hash(row: dict[str, Any]) -> str:
    normalized = {key: row[key] for key in sorted(row.keys())}
    return _sha256_text(_stable_json(normalized))


def _row_fingerprint(row: dict[str, Any], pk_column: str, updated_at_column: str | None) -> str:
    payload = {
        "pk": str(row.get(pk_column) or ""),
        "updated_at": _coerce_iso_datetime(row.get(updated_at_column)) if updated_at_column else None,
        "hash": _row_hash(row),
    }
    return _sha256_text(_stable_json(payload))


def sync_mapped_rows(
    *,
    source_connection: psycopg2.extensions.connection,
    idms_connection: psycopg2.extensions.connection,
    metadata_connection: psycopg2.extensions.connection,
    source_id: int,
    schema_name: str,
    table_name: str,
    target_class_name: str,
    pk_column: str,
    map_row_to_object: Callable[[dict[str, Any]], dict[str, Any]],
    updated_at_column: str | None = "updated_at",
    metadata_columns: list[str] | None = None,
) -> list[SourceRowMappingResult]:
    column_list = metadata_columns or ["*"]
    selected_columns = ", ".join(column_list)
    sql = f"SELECT {selected_columns} FROM {schema_name}.{table_name}"

    with source_connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall() or []

    results: list[SourceRowMappingResult] = []
    for row in rows:
        normalized_row = dict(row)
        source_pk_value = str(normalized_row.get(pk_column) or "").strip()
        if not source_pk_value:
            continue

        source_row_hash = _row_hash(normalized_row)
        source_row_updated_at = _coerce_iso_datetime(normalized_row.get(updated_at_column)) if updated_at_column else None
        source_row_fingerprint = _row_fingerprint(normalized_row, pk_column=pk_column, updated_at_column=updated_at_column)

        existing = load_source_mapping(
            metadata_connection,
            source_id=source_id,
            schema_name=schema_name,
            table_name=table_name,
            source_pk_value=source_pk_value,
            target_class_name=target_class_name,
        )
        if existing and existing.get("source_row_hash") == source_row_hash:
            results.append(
                SourceRowMappingResult(
                    source_pk_value=source_pk_value,
                    action="skipped_unchanged",
                    target_object_id=int(existing["target_object_id"]) if existing.get("target_object_id") else None,
                    source_row_hash=source_row_hash,
                    source_row_fingerprint=source_row_fingerprint,
                    source_row_updated_at=source_row_updated_at,
                )
            )
            continue

        if existing and source_row_updated_at and existing.get("source_row_updated_at"):
            try:
                current_updated_at = datetime.fromisoformat(source_row_updated_at)
                previous_updated_at = datetime.fromisoformat(str(existing.get("source_row_updated_at") or ""))
                if current_updated_at <= previous_updated_at and existing.get("source_row_hash") == source_row_hash:
                    results.append(
                        SourceRowMappingResult(
                            source_pk_value=source_pk_value,
                            action="skipped_not_newer",
                            target_object_id=int(existing["target_object_id"]) if existing.get("target_object_id") else None,
                            source_row_hash=source_row_hash,
                            source_row_fingerprint=source_row_fingerprint,
                            source_row_updated_at=source_row_updated_at,
                        )
                    )
                    continue
            except Exception:
                pass

        target_payload = map_row_to_object(normalized_row)
        object_name = str(target_payload.get("object_name") or normalized_row.get(pk_column) or "").strip()
        if not object_name:
            continue
        class_name = str(target_payload.get("class_name") or target_class_name or "entity").strip().lower()
        target_metadata = target_payload.get("metadata") if isinstance(target_payload.get("metadata"), dict) else {}
        status = str(target_payload.get("status") or "active").strip().lower() or "active"
        valid_from = target_payload.get("valid_from")
        valid_until = target_payload.get("valid_until")

        upserted = object_db.upsert_object_instance(
            idms_connection,
            object_name=object_name,
            class_name=class_name,
            metadata=target_metadata,
            status=status,
            valid_from=valid_from,
            valid_until=valid_until,
        )
        mapping = upsert_source_mapping(
            metadata_connection,
            source_id=source_id,
            schema_name=schema_name,
            table_name=table_name,
            source_pk_value=source_pk_value,
            target_class_name=class_name,
            target_object_id=int(upserted["object_id"]),
            target_metadata=target_metadata,
            source_row_hash=source_row_hash,
            source_row_updated_at=source_row_updated_at,
            source_row_fingerprint=source_row_fingerprint,
        )
        results.append(
            SourceRowMappingResult(
                source_pk_value=source_pk_value,
                action="upserted",
                target_object_id=int(mapping["target_object_id"]) if mapping.get("target_object_id") else None,
                source_row_hash=source_row_hash,
                source_row_fingerprint=source_row_fingerprint,
                source_row_updated_at=source_row_updated_at,
            )
        )

    return results
