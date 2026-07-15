import argparse
from datetime import date

import object_db


def _print_result(label: str, rows: list[dict]) -> None:
    print(f"{label}: {len(rows)}")
    for row in rows:
        attrs = row.get("temporal_attributes") or {}
        print(
            f"  - rel_id={row['relationship_id']} "
            f"src={row['src_object_name']}({row['src_object_id']}) "
            f"tar={row['tar_object_name']}({row['tar_object_id']}) "
            f"attrs={attrs}"
        )


def run_temporal_handover_test(recreate_schema: bool = False) -> dict:
    conn = object_db.get_connection()
    try:
        object_db.create_tables(conn, recreate=recreate_schema)

        # Seed three people and one company.
        werner = object_db.upsert_object_instance(conn, "Werner Hofer", "person", {"seed": True})
        reto = object_db.upsert_object_instance(conn, "Reto", "person", {"seed": True})
        tom = object_db.upsert_object_instance(conn, "Tom Miller", "person", {"seed": True})
        seveco = object_db.upsert_object_instance(conn, "Seveco Software AG", "company", {"seed": True})

        # Werner is CEO until 2026-09-15.
        object_db.upsert_temporal_relationship(
            connection=conn,
            relationship_name="holds_role",
            relationship_cat="organizational",
            src_object_id=int(werner["object_id"]),
            tar_object_id=int(seveco["object_id"]),
            temporal_attributes={"role": "CEO"},
            effective_from="2026-01-01",
            effective_until=None,
            exclusivity_scope="by_target",
        )

        before_rows = object_db.get_active_relationships_as_of(
            connection=conn,
            as_of="2026-09-10",
            relationship_name="holds_role",
            relationship_cat="organizational",
            tar_object_id=int(seveco["object_id"]),
        )
        _print_result("active_before", before_rows)

        # Reto takes over from 2026-09-15, exclusivity by target means prior active holder is deactivated.
        object_db.upsert_temporal_relationship(
            connection=conn,
            relationship_name="holds_role",
            relationship_cat="organizational",
            src_object_id=int(reto["object_id"]),
            tar_object_id=int(seveco["object_id"]),
            temporal_attributes={"role": "CEO"},
            effective_from="2026-09-15",
            effective_until=None,
            exclusivity_scope="by_target",
        )

        after_rows = object_db.get_active_relationships_as_of(
            connection=conn,
            as_of="2026-09-20",
            relationship_name="holds_role",
            relationship_cat="organizational",
            tar_object_id=int(seveco["object_id"]),
        )
        _print_result("active_after", after_rows)

        # Sanity checks.
        assert any(row["src_object_id"] == int(werner["object_id"]) for row in before_rows), (
            "Werner should be active before handover"
        )
        assert len(after_rows) == 1, "Exactly one active holder is expected after handover"
        assert int(after_rows[0]["src_object_id"]) == int(reto["object_id"]), (
            "Reto should be active after handover"
        )

        # Optional additional ambiguity seed example (same name, different evidence) for resolver tests.
        object_db.sync_object_temporal_attributes(
            connection=conn,
            object_id=int(tom["object_id"]),
            attributes={"email": "tom.miller.a@example.com", "birth_date": "1985-05-01"},
            valid_from=date.today(),
            commit=True,
        )

        return {
            "status": "ok",
            "werner_object_id": int(werner["object_id"]),
            "reto_object_id": int(reto["object_id"]),
            "seveco_object_id": int(seveco["object_id"]),
            "active_before_count": len(before_rows),
            "active_after_count": len(after_rows),
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Temporal relationship handover integration test")
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Drop and recreate object schema tables before running the test",
    )
    args = parser.parse_args()

    result = run_temporal_handover_test(recreate_schema=args.recreate)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_temporal_handover_module_smoke() -> None:
    assert callable(run_temporal_handover_test)
    assert callable(_print_result)
