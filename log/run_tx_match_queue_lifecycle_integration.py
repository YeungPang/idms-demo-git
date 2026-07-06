from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import object_db
import tx_match_retry_worker


def _cleanup_seeded_row(connection: Any, pending_id: int) -> int:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM tx_match_index_pending WHERE pending_id = %s", (int(pending_id),))
        deleted = int(cursor.rowcount or 0)
    connection.commit()
    return deleted


def run(out_dir: Path, cleanup: bool = False) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    connection = object_db.get_connection()
    try:
        object_db.create_tables(connection, recreate=False)

        payload = {
            "doc_id": 999999,
            "doc_type": "invoice",
            "tx_rows": [
                {
                    "record_id": "test:doc:999999:entity:e1",
                    "doc_id": 999999,
                    "doc_key": "test_doc_key",
                    "doc_type": "invoice",
                    "class_name": "invoice",
                    "source": "integration_test",
                    "amount": 100.0,
                    "currency": "CHF",
                    "date": "2026-06-30",
                    "description": "CHEVRON 4921 ZURICH",
                    "reference": "TEST-REF-001",
                    "party": "CHEVRON",
                    "feature_text": "desc=CHEVRON 4921 ZURICH; party=CHEVRON; ref=TEST-REF-001; amount=100.0; currency=CHF; date=2026-06-30; doc_type=invoice; class_name=invoice",
                }
            ],
            "force_index_success": True,
            "integration_test": True,
        }

        created = object_db.enqueue_tx_match_index_pending(
            connection=connection,
            payload=payload,
            doc_id=None,
            error_message="seeded_by_integration_test",
            source="integration_test",
            status="pending",
        )
        pending_id = int(created.get("pending_id") or 0)

        os.environ["IDMS_TX_MATCH_ALLOW_TEST_FORCE_SUCCESS"] = "true"
        retry_result = tx_match_retry_worker.run_worker(statuses=["pending"], limit=200)

        after = object_db.get_tx_match_index_pending_by_id(connection=connection, pending_id=pending_id)

        result = {
            "created": created,
            "retry_result": retry_result,
            "after": after,
            "assertions": {
                "pending_created": pending_id > 0,
                "final_status_indexed": bool((after or {}).get("status") == "indexed"),
                "retry_count_incremented": int((after or {}).get("retry_count") or 0) >= 1,
            },
        }

        if cleanup:
            deleted = _cleanup_seeded_row(connection=connection, pending_id=pending_id)
            result["cleanup"] = {
                "enabled": True,
                "pending_id": pending_id,
                "deleted_rows": deleted,
            }
        else:
            result["cleanup"] = {"enabled": False, "pending_id": pending_id}

        artifact = out_dir / "tx_match_queue_lifecycle_result.json"
        artifact.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        result["artifact"] = str(artifact)
        return result
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Integration lifecycle test for tx_match_index_pending queue.")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--cleanup", action="store_true", help="Delete seeded pending row after assertions.")
    args = parser.parse_args()

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(__file__).resolve().parent / f"tx_match_queue_lifecycle_{stamp}"

    result = run(out_dir=out_dir, cleanup=bool(args.cleanup))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    assertions = result.get("assertions") if isinstance(result.get("assertions"), dict) else {}
    ok = bool(assertions.get("pending_created")) and bool(assertions.get("final_status_indexed")) and bool(assertions.get("retry_count_incremented"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
