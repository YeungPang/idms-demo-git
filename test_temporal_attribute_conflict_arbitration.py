import argparse
import json
from datetime import date
from uuid import uuid4

import object_db
import solf_function


def _extract_value(attr_json):
    if isinstance(attr_json, dict) and "value" in attr_json:
        return attr_json.get("value")
    return attr_json


def _parse_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    return date.fromisoformat(text[:10])


def _fetch_timeline(connection, object_id, attr_type):
    sql = """
    SELECT attr_id, valid_from, valid_until, attr_json
    FROM attribute
    WHERE src_id = %s
      AND src_type = 'object'
      AND attr_type = %s
    ORDER BY valid_from ASC, attr_id ASC
    """
    rows = []
    with connection.cursor() as cursor:
        cursor.execute(sql, (int(object_id), str(attr_type).strip().lower()))
        for row in cursor.fetchall() or []:
            attr_json = row[3] or {}
            rows.append(
                {
                    "attr_id": int(row[0]),
                    "valid_from": row[1],
                    "valid_until": row[2],
                    "attr_json": attr_json,
                    "value": _extract_value(attr_json),
                    "arbitration": attr_json.get("arbitration") if isinstance(attr_json, dict) else {},
                }
            )
    return rows


def _print_timeline(label, rows):
    print(f"{label}: {len(rows)} row(s)")
    for row in rows:
        arbitration = row.get("arbitration") if isinstance(row.get("arbitration"), dict) else {}
        print(
            "  - "
            f"attr_id={row['attr_id']} "
            f"from={row['valid_from']} "
            f"until={row['valid_until']} "
            f"value={row.get('value')} "
            f"winner={arbitration.get('winner_normalized')} "
            f"strategy={arbitration.get('strategy')}"
        )


def _cleanup_object(connection, object_id):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM attribute WHERE src_id = %s AND src_type = 'object'", (int(object_id),))
        cursor.execute(
            "DELETE FROM object_relationship WHERE src_object_id = %s OR tar_object_id = %s",
            (int(object_id), int(object_id)),
        )
        cursor.execute("DELETE FROM object_instance WHERE object_id = %s", (int(object_id),))
    connection.commit()


def run_test(keep_data=False):
    connection = object_db.get_connection()
    object_id = None
    object_name = f"Temporal Arbitration Entity {uuid4().hex[:8]}"

    try:
        object_db.create_tables(connection, recreate=False)
        solf_function.set_connection(connection)

        first_payload = {
            "entity_id": f"entity-{uuid4().hex[:6]}",
            "object_name": object_name,
            "class_name": "company",
            "operation": "ingest",
            "effective_from": "2026-01-10",
            "confidence": 0.7,
            "attributes": {
                "address": {
                    "candidates": [
                        {
                            "value": "Oldstrasse 1, Zurich",
                            "confidence": 0.55,
                            "provenance": {"doc": "doc_low", "page": 1},
                        },
                        {
                            "value": "Neustrasse 9, Zurich",
                            "confidence": 0.91,
                            "provenance": {"doc": "doc_high", "page": 2, "method": "ocr+rule"},
                        },
                    ]
                }
            },
            "doc_id": 900001,
        }

        first_result = solf_function.db_ingest(first_payload)
        assert isinstance(first_result, dict) and first_result.get("object_id"), "first ingest did not return object row"
        object_id = int(first_result["object_id"])

        second_payload = {
            "entity_id": f"entity-{uuid4().hex[:6]}",
            "object_name": object_name,
            "class_name": "company",
            "operation": "update",
            "effective_from": "2026-03-01",
            "confidence": 0.8,
            "attributes": {
                "address": [
                    {
                        "value": "Neustrasse 9, Zurich",
                        "confidence": 0.80,
                    },
                    {
                        "value": "Bahnhofstrasse 1, Zurich",
                        "confidence": 0.78,
                        "effective_from": "2026-03-01",
                        "provenance": {"doc": "move_notice", "page": 1, "section": "registered_change"},
                    },
                ]
            },
            "doc_id": 900002,
        }

        second_result = solf_function.db_update(second_payload)
        assert isinstance(second_result, dict) and int(second_result.get("object_id") or 0) == object_id, "second update did not reuse object"

        third_payload = {
            "entity_id": f"entity-{uuid4().hex[:6]}",
            "object_name": object_name,
            "class_name": "company",
            "operation": "update",
            "effective_from": "2026-04-01",
            "confidence": 0.9,
            "attributes": {
                "address": {
                    "candidates": [
                        {
                            "value": "Bahnhofstrasse 1, Zurich",
                            "operation": "invalidate",
                            "confidence": 0.95,
                            "provenance": {"doc": "invalid_notice", "page": 1},
                        }
                    ]
                }
            },
            "doc_id": 900003,
        }

        third_result = solf_function.db_update(third_payload)
        assert isinstance(third_result, dict) and int(third_result.get("object_id") or 0) == object_id, "third update did not reuse object"

        timeline_rows = _fetch_timeline(connection, object_id=object_id, attr_type="address")
        _print_timeline("address_timeline", timeline_rows)

        assert len(timeline_rows) >= 2, "expected at least two temporal rows for address"

        first_row = timeline_rows[0]
        second_row = timeline_rows[1]

        assert str(first_row.get("value") or "") == "Neustrasse 9, Zurich", "first winning value mismatch"
        assert _parse_date(first_row.get("valid_until")) == date(2026, 3, 1), "first row was not closed at handover date"

        assert str(second_row.get("value") or "") == "Bahnhofstrasse 1, Zurich", "second winning value mismatch"
        assert _parse_date(second_row.get("valid_until")) == date(2026, 4, 1), "second row was not closed by invalidation"

        open_rows = [row for row in timeline_rows if row.get("valid_until") is None]
        assert not open_rows, "invalidation-only update should leave no active row"

        arbitration_rows = [row for row in timeline_rows if isinstance(row.get("arbitration"), dict) and row.get("arbitration")]
        assert arbitration_rows, "expected arbitration metadata on written rows"
        assert all(
            row.get("arbitration", {}).get("strategy") == "generic_temporal_conflict_arbitration"
            for row in arbitration_rows
        ), "unexpected arbitration strategy marker"

        summary = {
            "status": "ok",
            "object_id": object_id,
            "object_name": object_name,
            "timeline_count": len(timeline_rows),
            "open_rows": len(open_rows),
            "timeline": [
                {
                    "attr_id": row["attr_id"],
                    "valid_from": str(row.get("valid_from")),
                    "valid_until": str(row.get("valid_until")),
                    "value": row.get("value"),
                    "arbitration": row.get("arbitration") or {},
                }
                for row in timeline_rows
            ],
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        return summary
    finally:
        solf_function.clear_connection()
        if object_id is not None and not keep_data:
            _cleanup_object(connection, object_id)
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="Run temporal attribute conflict arbitration regression test")
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="Keep inserted test rows instead of cleaning up",
    )
    args = parser.parse_args()

    run_test(keep_data=bool(args.keep_data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
