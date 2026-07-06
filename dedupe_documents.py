from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import object_db


@dataclass
class DocumentRow:
    doc_id: int
    doc_type: str
    doc_key: str
    metadata: dict[str, Any]


def _get_nested(metadata: dict[str, Any], *keys: str) -> Any:
    current: Any = metadata
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _normalized_markdown_hash(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _normalize_text_fingerprint(value: str) -> str:
    text = str(value or "").lower().strip()
    text = " ".join(text.split())
    return text


def _identity_for_doc(row: DocumentRow, repo_root: Path) -> str | None:
    metadata = row.metadata if isinstance(row.metadata, dict) else {}

    stable_identity = str(
        _get_nested(metadata, "user_metadata", "source_reference", "stable_source_identity")
        or _get_nested(metadata, "user_metadata", "stable_source_identity")
        or ""
    ).strip()
    if stable_identity:
        return f"stable:{stable_identity}"

    content_hash = str(
        _get_nested(metadata, "user_metadata", "source_reference", "markdown_content_hash")
        or ""
    ).strip()
    if content_hash:
        doc_type = (row.doc_type or "document").strip().lower() or "document"
        return f"md:{doc_type}:{content_hash}"

    markdown_cache_path = str(
        _get_nested(metadata, "user_metadata", "source_reference", "markdown_cache_path")
        or ""
    ).strip()
    if markdown_cache_path:
        path = Path(markdown_cache_path)
        if not path.is_absolute():
            path = (repo_root / path).resolve()
        if path.exists() and path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
                doc_type = (row.doc_type or "document").strip().lower() or "document"
                return f"md:{doc_type}:{_normalized_markdown_hash(text)}"
            except OSError:
                return None

    # Legacy fallback: many rows created before stable identity was introduced
    # still carry a reliable semantic description in document metadata.
    user_description = str(metadata.get("user_description") or "").strip()
    if user_description:
        doc_type = (row.doc_type or "document").strip().lower() or "document"
        normalized_desc = _normalize_text_fingerprint(user_description)
        if normalized_desc:
            return f"desc:{doc_type}:{_normalized_markdown_hash(normalized_desc)}"

    return None


def _load_documents(conn) -> list[DocumentRow]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT doc_id, COALESCE(doc_type, ''), COALESCE(doc_key, ''), metadata
            FROM document
            ORDER BY doc_id;
            """
        )
        rows = cur.fetchall()
    out: list[DocumentRow] = []
    for row in rows:
        out.append(
            DocumentRow(
                doc_id=int(row[0]),
                doc_type=str(row[1] or ""),
                doc_key=str(row[2] or ""),
                metadata=row[3] if isinstance(row[3], dict) else {},
            )
        )
    return out


def _find_duplicate_groups(rows: list[DocumentRow], repo_root: Path) -> dict[str, list[DocumentRow]]:
    groups: dict[str, list[DocumentRow]] = {}
    for row in rows:
        identity = _identity_for_doc(row, repo_root)
        if not identity:
            continue
        groups.setdefault(identity, []).append(row)
    return {k: sorted(v, key=lambda r: r.doc_id) for k, v in groups.items() if len(v) > 1}


def _list_fk_targets(conn) -> list[tuple[str, str, str]]:
    # Discover single-column FKs referencing document(doc_id), excluding part (handled specially).
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                a.attname AS column_name
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_class refc ON refc.oid = con.confrelid
            JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
            WHERE con.contype = 'f'
              AND refc.relname = 'document'
              AND array_length(con.conkey, 1) = 1
              AND n.nspname = 'public'
              AND c.relname <> 'part'
            ORDER BY n.nspname, c.relname, a.attname;
            """
        )
        rows = cur.fetchall()
    return [(str(s), str(t), str(col)) for s, t, col in rows]


def _merge_part_rows(conn, old_doc_id: int, keep_doc_id: int) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO part (doc_id, part_key, class_id, object_id, relationship_id, metadata)
            SELECT %s, part_key, class_id, object_id, relationship_id, metadata
            FROM part
            WHERE doc_id = %s
            ON CONFLICT (doc_id, part_key) DO UPDATE
            SET
                class_id = COALESCE(part.class_id, EXCLUDED.class_id),
                object_id = COALESCE(part.object_id, EXCLUDED.object_id),
                relationship_id = COALESCE(part.relationship_id, EXCLUDED.relationship_id),
                metadata = COALESCE(part.metadata, '{}'::jsonb) || COALESCE(EXCLUDED.metadata, '{}'::jsonb);
            """,
            (keep_doc_id, old_doc_id),
        )
        merged = cur.rowcount

        cur.execute("DELETE FROM part WHERE doc_id = %s", (old_doc_id,))
        deleted = cur.rowcount

    return int(merged), int(deleted)


def _retarget_fk_references(conn, old_doc_id: int, keep_doc_id: int) -> int:
    updates = 0
    for schema_name, table_name, column_name in _list_fk_targets(conn):
        with conn.cursor() as cur:
            sql = f'UPDATE "{schema_name}"."{table_name}" SET "{column_name}" = %s WHERE "{column_name}" = %s'
            cur.execute(sql, (keep_doc_id, old_doc_id))
            updates += int(cur.rowcount)
    return updates


def _retarget_metadata_doc_ids(conn, old_doc_id: int, keep_doc_id: int) -> int:
    updated = 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE object_instance
            SET metadata = jsonb_set(metadata, '{doc_id}', to_jsonb(%s::text), true)
            WHERE metadata ? 'doc_id'
              AND (metadata->>'doc_id') ~ '^\\d+$'
              AND (metadata->>'doc_id')::bigint = %s;
            """,
            (str(keep_doc_id), old_doc_id),
        )
        updated += int(cur.rowcount)

        cur.execute(
            """
            UPDATE object_relationship
            SET metadata = jsonb_set(metadata, '{doc_id}', to_jsonb(%s::text), true)
            WHERE metadata ? 'doc_id'
              AND (metadata->>'doc_id') ~ '^\\d+$'
              AND (metadata->>'doc_id')::bigint = %s;
            """,
            (str(keep_doc_id), old_doc_id),
        )
        updated += int(cur.rowcount)

    return updated


def _append_merge_history(conn, keep_doc_id: int, old_doc_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE document
            SET metadata = jsonb_set(
                COALESCE(metadata, '{}'::jsonb),
                '{dedupe,merged_from_doc_ids}',
                COALESCE(
                    (
                        SELECT to_jsonb(ARRAY(
                            SELECT DISTINCT value::bigint
                            FROM jsonb_array_elements_text(
                                COALESCE(metadata#>'{dedupe,merged_from_doc_ids}', '[]'::jsonb)
                            )
                            UNION
                            SELECT %s::bigint
                            ORDER BY 1
                        ))
                    ),
                    to_jsonb(ARRAY[%s::bigint])
                ),
                true
            )
            WHERE doc_id = %s;
            """,
            (old_doc_id, old_doc_id, keep_doc_id),
        )


def _delete_document(conn, doc_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM document WHERE doc_id = %s", (doc_id,))
        return int(cur.rowcount)


def run_dedupe(apply_changes: bool, identity_filter: str | None = None) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parent
    conn = object_db.get_connection()

    summary: dict[str, Any] = {
        "groups": 0,
        "documents_considered": 0,
        "documents_merged": 0,
        "parts_merged": 0,
        "parts_deleted_old": 0,
        "fk_updates": 0,
        "metadata_doc_id_updates": 0,
        "documents_deleted": 0,
        "plans": [],
    }

    try:
        rows = _load_documents(conn)
        groups = _find_duplicate_groups(rows, repo_root)

        if identity_filter:
            groups = {k: v for k, v in groups.items() if identity_filter in k}

        summary["groups"] = len(groups)

        for identity, docs in sorted(groups.items()):
            keep = docs[0]
            losers = docs[1:]
            plan = {
                "identity": identity,
                "keep_doc_id": keep.doc_id,
                "merge_doc_ids": [d.doc_id for d in losers],
                "doc_keys": [d.doc_key for d in docs],
            }
            summary["plans"].append(plan)
            summary["documents_considered"] += len(docs)

            if not apply_changes:
                continue

            for loser in losers:
                merged, deleted = _merge_part_rows(conn, loser.doc_id, keep.doc_id)
                fk_updates = _retarget_fk_references(conn, loser.doc_id, keep.doc_id)
                metadata_updates = _retarget_metadata_doc_ids(conn, loser.doc_id, keep.doc_id)
                _append_merge_history(conn, keep.doc_id, loser.doc_id)
                doc_deleted = _delete_document(conn, loser.doc_id)

                summary["documents_merged"] += 1
                summary["parts_merged"] += merged
                summary["parts_deleted_old"] += deleted
                summary["fk_updates"] += fk_updates
                summary["metadata_doc_id_updates"] += metadata_updates
                summary["documents_deleted"] += doc_deleted

        if apply_changes:
            conn.commit()
        else:
            conn.rollback()

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deduplicate documents by stable identity/content hash while preserving part and relationship links."
    )
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    parser.add_argument("--identity-filter", type=str, default=None, help="Only process groups whose identity contains this text")
    args = parser.parse_args()

    result = run_dedupe(apply_changes=args.apply, identity_filter=args.identity_filter)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
