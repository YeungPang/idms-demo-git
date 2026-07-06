from __future__ import annotations

import csv
import io
import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

import domain_db
import object_db


router = APIRouter(prefix="/api/accounting", tags=["accounting"])
LOGGER = logging.getLogger("idms.api")


def _parse_iso_date(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: {text}") from exc


@router.get("/journal")
def get_journal(
    start_date: str,
    end_date: str,
    legal_entity_ref: str | None = None,
    account_number: int | None = None,
    direction: str | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> dict[str, Any]:
    start = _parse_iso_date(start_date, "start_date")
    end = _parse_iso_date(end_date, "end_date")
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    direction_norm = str(direction or "").strip().lower() or None
    if direction_norm not in {None, "debit", "credit"}:
        raise HTTPException(status_code=400, detail="direction must be debit or credit")

    connection = object_db.get_connection()
    try:
        rows = domain_db.list_journal_lines(
            connection=connection,
            start_date=start,
            end_date=end,
            legal_entity_ref=legal_entity_ref,
            account_number=account_number,
            direction=direction_norm,
            limit=limit,
            offset=offset,
        )

        tx_map: dict[str, dict[str, Any]] = {}
        for row in rows:
            tx_id = str(row.get("transaction_id") or "")
            tx = tx_map.get(tx_id)
            if tx is None:
                tx = {
                    "transaction_id": tx_id,
                    "journal_sequence_number": row.get("journal_sequence_number"),
                    "transaction_date": row.get("transaction_date"),
                    "legal_entity_ref": row.get("legal_entity_ref"),
                    "source_type": row.get("source_type"),
                    "source_document_id": row.get("source_document_id"),
                    "object_id": row.get("object_id"),
                    "description": row.get("description"),
                    "receipt_archive_url": row.get("receipt_archive_url"),
                    "created_at": row.get("created_at"),
                    "lines": [],
                }
                tx_map[tx_id] = tx

            tx["lines"].append(
                {
                    "line_id": row.get("line_id"),
                    "account_number": row.get("account_number"),
                    "account_name": row.get("account_name"),
                    "account_type": row.get("account_type"),
                    "direction": row.get("direction"),
                    "source_currency": row.get("source_currency"),
                    "amount_source_currency": row.get("amount_source_currency"),
                    "exchange_rate_to_chf": row.get("exchange_rate_to_chf"),
                    "amount_chf": row.get("amount_chf"),
                    "swiss_vat_code": row.get("swiss_vat_code"),
                    "swiss_vat_box": row.get("swiss_vat_box"),
                    "metadata": row.get("line_metadata") or {},
                }
            )

        journal = sorted(
            tx_map.values(),
            key=lambda tx: (
                str(tx.get("transaction_date") or ""),
                int(tx.get("journal_sequence_number") or 0),
            ),
        )

        return {
            "success": True,
            "count_lines": len(rows),
            "count_transactions": len(journal),
            "result": journal,
            "query": {
                "start_date": start,
                "end_date": end,
                "legal_entity_ref": legal_entity_ref,
                "account_number": account_number,
                "direction": direction_norm,
                "limit": limit,
                "offset": offset,
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception("Failed to fetch accounting journal")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        connection.close()


@router.get("/vat-return")
def get_vat_return(
    start_date: str,
    end_date: str,
    legal_entity_ref: str | None = None,
) -> dict[str, Any]:
    start = _parse_iso_date(start_date, "start_date")
    end = _parse_iso_date(end_date, "end_date")
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    connection = object_db.get_connection()
    try:
        result = domain_db.summarize_vat_return(
            connection=connection,
            start_date=start,
            end_date=end,
            legal_entity_ref=legal_entity_ref,
        )
        return {"success": True, "result": result}
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception("Failed to summarize VAT return")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        connection.close()


@router.get("/journal/export", response_class=PlainTextResponse)
def export_journal_csv(
    start_date: str,
    end_date: str,
    legal_entity_ref: str | None = None,
    account_number: int | None = None,
    direction: str | None = None,
    limit: int = 10000,
    offset: int = 0,
) -> str:
    start = _parse_iso_date(start_date, "start_date")
    end = _parse_iso_date(end_date, "end_date")
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    direction_norm = str(direction or "").strip().lower() or None
    if direction_norm not in {None, "debit", "credit"}:
        raise HTTPException(status_code=400, detail="direction must be debit or credit")

    connection = object_db.get_connection()
    try:
        rows = domain_db.list_journal_lines(
            connection=connection,
            start_date=start,
            end_date=end,
            legal_entity_ref=legal_entity_ref,
            account_number=account_number,
            direction=direction_norm,
            limit=limit,
            offset=offset,
        )

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "transaction_id",
                "journal_sequence_number",
                "transaction_date",
                "legal_entity_ref",
                "source_type",
                "source_document_id",
                "object_id",
                "description",
                "line_id",
                "account_number",
                "account_name",
                "account_type",
                "direction",
                "source_currency",
                "amount_source_currency",
                "exchange_rate_to_chf",
                "amount_chf",
                "swiss_vat_code",
                "swiss_vat_box",
                "created_at",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.get("transaction_id"),
                    row.get("journal_sequence_number"),
                    row.get("transaction_date"),
                    row.get("legal_entity_ref"),
                    row.get("source_type"),
                    row.get("source_document_id"),
                    row.get("object_id"),
                    row.get("description"),
                    row.get("line_id"),
                    row.get("account_number"),
                    row.get("account_name"),
                    row.get("account_type"),
                    row.get("direction"),
                    row.get("source_currency"),
                    row.get("amount_source_currency"),
                    row.get("exchange_rate_to_chf"),
                    row.get("amount_chf"),
                    row.get("swiss_vat_code"),
                    row.get("swiss_vat_box"),
                    row.get("created_at"),
                ]
            )

        return output.getvalue()
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception("Failed to export accounting journal")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        connection.close()
