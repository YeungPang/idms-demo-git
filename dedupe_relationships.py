from __future__ import annotations

import argparse
import json
from collections import defaultdict
from typing import Any

import object_db


def _fetch_relationship_rows(connection: Any) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT relationship_id, relationship_name, relationship_cat, src_object_id, tar_object_id,
                   confidence, metadata, valid_from, valid_until, entry_date
            FROM object_relationship
            ORDER BY src_object_id, tar_object_id, relationship_cat, relationship_id
            """
        )
        rows = cursor.fetchall()

    return [
        {
            "relationship_id": int(row[0]),
            "relationship_name": str(row[1] or "").strip(),
            "relationship_cat": str(row[2] or "").strip(),
            "src_object_id": int(row[3]),
            "tar_object_id": int(row[4]),
            "confidence": row[5],
            "metadata": row[6] or {},
            "valid_from": row[7],
            "valid_until": row[8],
            "entry_date": row[9],
        }
        for row in rows
    ]


def _merge_metadata(rows: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for row in rows:
        payload = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        merged.update(payload)
    return merged


def _max_confidence(rows: list[dict[str, Any]]) -> Any:
    values = [row.get("confidence") for row in rows if row.get("confidence") is not None]
    if not values:
        return None
    return max(values)


def _copy_parts_for_relationship(connection: Any, source_id: int, target_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT part_id, doc_id, metadata
            FROM part
            WHERE relationship_id = %s
            ORDER BY part_id
            """,
            (int(source_id),),
        )
        rows = cursor.fetchall()

        for part_id, doc_id, metadata in rows:
            cursor.execute(
                """
                SELECT part_id
                FROM part
                WHERE doc_id = %s AND relationship_id = %s
                LIMIT 1
                """,
                (int(doc_id), int(target_id)),
            )
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    """
                    UPDATE part
                    SET metadata = COALESCE(part.metadata, '{}'::jsonb) || %s::jsonb
                    WHERE part_id = %s
                    """,
                    (json.dumps(metadata or {}), int(existing[0])),
                )
                cursor.execute("DELETE FROM part WHERE part_id = %s", (int(part_id),))
                continue

            cursor.execute(
                """
                UPDATE part
                SET relationship_id = %s,
                    part_key = %s
                WHERE part_id = %s
                """,
                (int(target_id), f"relationship:{int(target_id)}", int(part_id)),
            )


def _move_relationship_attributes(connection: Any, source_id: int, target_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE attribute
            SET src_id = %s
            WHERE src_type = 'relationship' AND src_id = %s
            """,
            (int(target_id), int(source_id)),
        )


def _distribute_compound_relationship_attributes(
    connection: Any,
    source_id: int,
    target_by_name: dict[str, int],
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT attr_id, attr_type, attr_json, valid_from, valid_until
            FROM attribute
            WHERE src_type = 'relationship' AND src_id = %s
            ORDER BY attr_id
            """,
            (int(source_id),),
        )
        attr_rows = cursor.fetchall()

    def _pick_target(attr_type: str) -> int | None:
        lowered = str(attr_type or "").strip().lower()
        if lowered in {"share_count", "share_allocation_raw", "share_nominal_value_chf", "share_total_nominal_value_chf", "nominal_value", "holding"}:
            return target_by_name.get("shareholder_of")
        if lowered in {"role", "title", "position", "president_since"}:
            return target_by_name.get("president_of")
        if lowered == "active":
            return None
        return None

    with connection.cursor() as cursor:
        for attr_id, attr_type, attr_json, valid_from, valid_until in attr_rows:
            chosen_target = _pick_target(str(attr_type or ""))

            target_ids: list[int] = []
            if chosen_target is not None:
                target_ids = [int(chosen_target)]
            else:
                target_ids = [int(v) for v in target_by_name.values()]

            for target_id in target_ids:
                cursor.execute(
                    """
                    INSERT INTO attribute (src_id, src_type, attr_type, valid_from, valid_until, attr_json, search_txt)
                    VALUES (%s, 'relationship', %s, COALESCE(%s, CURRENT_DATE), %s, %s::jsonb, NULL)
                    """,
                    (int(target_id), str(attr_type or ""), valid_from, valid_until, json.dumps(attr_json or {})),
                )

            cursor.execute("DELETE FROM attribute WHERE attr_id = %s", (int(attr_id),))


def _delete_relationship(connection: Any, relationship_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM object_relationship WHERE relationship_id = %s", (int(relationship_id),))


def _update_relationship_row(connection: Any, relationship_id: int, canonical_name: str, relationship_cat: str, metadata: dict[str, Any], confidence: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE object_relationship
            SET relationship_name = %s,
                relationship_cat = %s,
                metadata = COALESCE(object_relationship.metadata, '{}'::jsonb) || %s::jsonb,
                confidence = COALESCE(%s, object_relationship.confidence)
            WHERE relationship_id = %s
            """,
            (canonical_name, relationship_cat, json.dumps(metadata or {}), confidence, int(relationship_id)),
        )


def build_plan(rows: list[dict[str, Any]]) -> dict[str, Any]:
    simple_groups: dict[tuple[int, int, str, str], list[dict[str, Any]]] = defaultdict(list)
    compound_rows: list[dict[str, Any]] = []

    for row in rows:
        expanded = object_db.expand_relationship_names(row["relationship_name"])
        if len(expanded) > 1:
            compound_rows.append({**row, "expanded": expanded})
            continue
        canonical = expanded[0]
        simple_groups[(row["src_object_id"], row["tar_object_id"], row["relationship_cat"], canonical)].append(row)

    merge_groups: list[dict[str, Any]] = []
    for key, grouped_rows in simple_groups.items():
        if len(grouped_rows) <= 1:
            continue
        canonical = key[3]
        ordered = sorted(
            grouped_rows,
            key=lambda item: (0 if object_db.canonicalize_relationship_name(item["relationship_name"]) == canonical and item["relationship_name"].lower() == canonical else 1, item["relationship_id"]),
        )
        keeper = ordered[0]
        duplicates = ordered[1:]
        merge_groups.append(
            {
                "src_object_id": key[0],
                "tar_object_id": key[1],
                "relationship_cat": key[2],
                "canonical_name": canonical,
                "keeper": keeper,
                "duplicates": duplicates,
            }
        )

    return {
        "merge_groups": merge_groups,
        "compound_rows": compound_rows,
    }


def apply_plan(connection: Any, plan: dict[str, Any]) -> dict[str, Any]:
    merged = 0
    deleted = 0
    created_from_compound = 0

    for group in plan["merge_groups"]:
        keeper = group["keeper"]
        duplicates = group["duplicates"]
        canonical_name = group["canonical_name"]
        all_rows = [keeper, *duplicates]
        _update_relationship_row(
            connection,
            relationship_id=int(keeper["relationship_id"]),
            canonical_name=canonical_name,
            relationship_cat=group["relationship_cat"],
            metadata=_merge_metadata(all_rows),
            confidence=_max_confidence(all_rows),
        )

        for duplicate in duplicates:
            _copy_parts_for_relationship(connection, duplicate["relationship_id"], keeper["relationship_id"])
            _move_relationship_attributes(connection, duplicate["relationship_id"], keeper["relationship_id"])
            _delete_relationship(connection, duplicate["relationship_id"])
            deleted += 1
        merged += 1

    for compound in plan["compound_rows"]:
        created_by_name: dict[str, int] = {}
        for canonical_name in compound["expanded"]:
            row = object_db.upsert_object_relationship(
                connection=connection,
                relationship_name=canonical_name,
                relationship_cat=compound["relationship_cat"],
                src_object_id=compound["src_object_id"],
                tar_object_id=compound["tar_object_id"],
                confidence=compound.get("confidence"),
                metadata=compound.get("metadata") if isinstance(compound.get("metadata"), dict) else {},
                valid_from=compound.get("valid_from"),
                valid_until=compound.get("valid_until"),
            )
            created_by_name[canonical_name] = int(row["relationship_id"])
            created_from_compound += 1

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT doc_id, metadata
                FROM part
                WHERE relationship_id = %s
                """,
                (int(compound["relationship_id"]),),
            )
            part_rows = cursor.fetchall()
            for doc_id, metadata in part_rows:
                for target_id in created_by_name.values():
                    cursor.execute(
                        """
                        INSERT INTO part (doc_id, part_key, relationship_id, metadata)
                        VALUES (%s, %s, %s, %s::jsonb)
                        ON CONFLICT (doc_id, part_key)
                        DO UPDATE SET metadata = COALESCE(part.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                                      relationship_id = EXCLUDED.relationship_id
                        """,
                        (int(doc_id), f"relationship:{int(target_id)}", int(target_id), json.dumps(metadata or {})),
                    )

        _distribute_compound_relationship_attributes(
            connection=connection,
            source_id=int(compound["relationship_id"]),
            target_by_name=created_by_name,
        )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM part
                WHERE relationship_id = %s
                """,
                (int(compound["relationship_id"]),),
            )

        _delete_relationship(connection, compound["relationship_id"])
        deleted += 1

    connection.commit()
    return {
        "merged_groups": merged,
        "deleted_relationship_rows": deleted,
        "created_from_compound": created_from_compound,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deduplicate semantically equivalent object_relationship rows.")
    parser.add_argument("--apply", action="store_true", help="Apply the dedupe plan instead of printing it.")
    args = parser.parse_args()

    connection = object_db.get_connection()
    try:
        rows = _fetch_relationship_rows(connection)
        plan = build_plan(rows)
        print("merge_groups=", len(plan["merge_groups"]))
        print("compound_rows=", len(plan["compound_rows"]))

        for group in plan["merge_groups"][:50]:
            print(
                "MERGE",
                group["src_object_id"],
                "->",
                group["tar_object_id"],
                group["canonical_name"],
                "keeper=",
                group["keeper"]["relationship_id"],
                "drop=",
                [row["relationship_id"] for row in group["duplicates"]],
                "names=",
                [group["keeper"]["relationship_name"], *[row["relationship_name"] for row in group["duplicates"]]],
            )

        for compound in plan["compound_rows"][:50]:
            print(
                "SPLIT",
                compound["relationship_id"],
                compound["relationship_name"],
                compound["src_object_id"],
                "->",
                compound["tar_object_id"],
                "into",
                compound["expanded"],
            )

        if not args.apply:
            connection.rollback()
            return

        summary = apply_plan(connection, plan)
        print(json.dumps(summary, ensure_ascii=False, default=str, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
