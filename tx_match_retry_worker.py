from __future__ import annotations

import argparse
import json
import os
from typing import Any

import object_db
import tx_match_index


def _retry_one(connection: Any, entry: dict[str, Any]) -> dict[str, Any]:
    pending_id = int(entry.get("pending_id") or 0)
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    tx_rows = payload.get("tx_rows") if isinstance(payload.get("tx_rows"), list) else []

    allow_forced_success = str(os.getenv("IDMS_TX_MATCH_ALLOW_TEST_FORCE_SUCCESS", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if allow_forced_success and bool(payload.get("force_index_success")):
        mock_result = {
            "indexed": True,
            "collection": "tx_match_test_mode",
            "row_count": len(tx_rows),
            "doc_id": int(entry.get("doc_id") or 0),
            "test_mode": True,
        }
        updated = object_db.update_tx_match_index_pending_status(
            connection=connection,
            pending_id=pending_id,
            status="indexed",
            error_message=None,
            retry_increment=True,
            last_result=mock_result,
        )
        return {
            "pending_id": pending_id,
            "ok": True,
            "result": mock_result,
            "updated": updated,
        }

    if not tx_rows:
        updated = object_db.update_tx_match_index_pending_status(
            connection=connection,
            pending_id=pending_id,
            status="failed",
            error_message="missing tx_rows in pending payload",
            retry_increment=True,
            last_result={"indexed": False, "reason": "missing_tx_rows"},
        )
        return {
            "pending_id": pending_id,
            "ok": False,
            "reason": "missing_tx_rows",
            "updated": updated,
        }

    result = tx_match_index.index_rows(tx_rows)
    if bool(result.get("indexed")):
        updated = object_db.update_tx_match_index_pending_status(
            connection=connection,
            pending_id=pending_id,
            status="indexed",
            error_message=None,
            retry_increment=True,
            last_result=result,
        )
        return {
            "pending_id": pending_id,
            "ok": True,
            "result": result,
            "updated": updated,
        }

    updated = object_db.update_tx_match_index_pending_status(
        connection=connection,
        pending_id=pending_id,
        status="failed",
        error_message=str(result.get("reason") or result.get("error") or "retry_failed"),
        retry_increment=True,
        last_result=result,
    )
    return {
        "pending_id": pending_id,
        "ok": False,
        "result": result,
        "updated": updated,
    }


def run_worker(statuses: list[str], limit: int) -> dict[str, Any]:
    connection = object_db.get_connection()
    try:
        object_db.create_tables(connection, recreate=False)
        entries = object_db.list_tx_match_index_pending(
            connection=connection,
            statuses=statuses,
            limit=limit,
        )

        retried: list[dict[str, Any]] = []
        for entry in entries:
            retried.append(_retry_one(connection, entry))

        success_count = sum(1 for item in retried if item.get("ok"))
        failed_count = len(retried) - success_count
        return {
            "requested_statuses": statuses,
            "selected": len(entries),
            "retried": len(retried),
            "success_count": success_count,
            "failed_count": failed_count,
            "items": retried,
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry tx_match_index_pending queue entries.")
    parser.add_argument(
        "--status",
        action="append",
        default=["pending", "failed"],
        help="Queue status to include (repeatable). Default: pending, failed",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum entries to retry in one run (default: 50)",
    )
    args = parser.parse_args()

    statuses = [str(item).strip().lower() for item in (args.status or []) if str(item).strip()]
    statuses = statuses or ["pending", "failed"]

    result = run_worker(statuses=statuses, limit=max(1, int(args.limit)))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if int(result.get("failed_count") or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
