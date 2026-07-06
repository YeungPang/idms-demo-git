from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

import document_generation
import document_template_pipeline
import object_db
from idms_api_server.deps import parse_json_dict
from idms_api_server.schemas import (
    DocumentExportSaveRequest,
    TemplateReferenceGenerateRequest,
    SalesPipelineGenerateRequest,
    SalesPipelineStageSyncRequest,
    SalesPipelineStageBulkSyncRequest,
    SalesPipelineStageBulkSyncItem,
)


router = APIRouter(prefix="/api/documents", tags=["documents"])
LOGGER = logging.getLogger("idms.api")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_REFERENCE_DIR = (PROJECT_ROOT / "generated" / "template_references").resolve()
TEMPLATE_REFERENCE_REGISTRY = (TEMPLATE_REFERENCE_DIR / "registry.json").resolve()


def _slugify(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip().lower())
    text = text.strip("-._")
    return text or "template-reference"


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_sales_pipeline_tables(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS sales_pipeline_run (
                run_id BIGSERIAL PRIMARY KEY,
                run_key TEXT NOT NULL UNIQUE,
                pipeline_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                stage_count INTEGER NOT NULL DEFAULT 0,
                generated_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                no_booking BOOLEAN NOT NULL DEFAULT TRUE,
                output_dir TEXT,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS sales_pipeline_stage (
                stage_id BIGSERIAL PRIMARY KEY,
                run_id BIGINT NOT NULL REFERENCES sales_pipeline_run(run_id) ON DELETE CASCADE,
                stage_order INTEGER NOT NULL,
                stage_name TEXT NOT NULL,
                reference_name TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT,
                saved_path TEXT,
                filename TEXT,
                media_type TEXT,
                size_bytes BIGINT,
                result JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (run_id, stage_order)
            )
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sales_pipeline_run_created_at ON sales_pipeline_run(created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sales_pipeline_stage_run_id ON sales_pipeline_stage(run_id, stage_order)")


def _create_sales_pipeline_run(
    connection: Any,
    *,
    pipeline_name: str,
    stage_count: int,
    output_dir: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    run_key = f"{_slugify(pipeline_name)}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO sales_pipeline_run (
                run_key, pipeline_name, status, stage_count, output_dir, no_booking, payload
            )
            VALUES (%s, %s, 'running', %s, %s, TRUE, %s::jsonb)
            RETURNING run_id, run_key, created_at
            """,
            (run_key, pipeline_name, int(stage_count), output_dir, json.dumps(payload or {})),
        )
        row = cursor.fetchone()
    return {
        "run_id": int(row[0]),
        "run_key": str(row[1]),
        "created_at": row[2],
    }


def _upsert_sales_pipeline_stage(
    connection: Any,
    *,
    run_id: int,
    stage_order: int,
    stage_name: str,
    reference_name: str,
    status: str,
    error_message: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    payload = result or {}
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO sales_pipeline_stage (
                run_id, stage_order, stage_name, reference_name, status,
                error_message, saved_path, filename, media_type, size_bytes, result, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
            ON CONFLICT (run_id, stage_order)
            DO UPDATE SET
                stage_name = EXCLUDED.stage_name,
                reference_name = EXCLUDED.reference_name,
                status = EXCLUDED.status,
                error_message = EXCLUDED.error_message,
                saved_path = EXCLUDED.saved_path,
                filename = EXCLUDED.filename,
                media_type = EXCLUDED.media_type,
                size_bytes = EXCLUDED.size_bytes,
                result = EXCLUDED.result,
                updated_at = NOW()
            """,
            (
                int(run_id),
                int(stage_order),
                stage_name,
                reference_name or None,
                status,
                error_message,
                payload.get("saved_path"),
                payload.get("filename"),
                payload.get("media_type"),
                int(payload.get("size_bytes") or 0) if payload.get("size_bytes") not in (None, "") else None,
                json.dumps(payload),
            ),
        )


def _finalize_sales_pipeline_run(
    connection: Any,
    *,
    run_id: int,
    generated_count: int,
    failed_count: int,
) -> None:
    status = "completed" if int(failed_count) == 0 else ("partial" if int(generated_count) > 0 else "failed")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sales_pipeline_run
            SET status = %s,
                generated_count = %s,
                failed_count = %s,
                updated_at = NOW()
            WHERE run_id = %s
            """,
            (status, int(generated_count), int(failed_count), int(run_id)),
        )


def _refresh_sales_pipeline_run_aggregate(connection: Any, *, run_id: int) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                COUNT(*) AS stage_count,
                COUNT(*) FILTER (WHERE lower(status) = 'failed') AS failed_count,
                COUNT(*) FILTER (
                    WHERE lower(status) IN ('generated', 'completed', 'done', 'synced', 'booked', 'paid')
                ) AS success_count
            FROM sales_pipeline_stage
            WHERE run_id = %s
            """,
            (int(run_id),),
        )
        row = cursor.fetchone() or (0, 0, 0)

        stage_count = int(row[0] or 0)
        failed_count = int(row[1] or 0)
        generated_count = int(row[2] or 0)

        if stage_count <= 0:
            run_status = "running"
        elif failed_count == 0 and generated_count >= stage_count:
            run_status = "completed"
        elif failed_count > 0 and generated_count == 0:
            run_status = "failed"
        elif failed_count > 0 or generated_count > 0:
            run_status = "partial"
        else:
            run_status = "running"

        cursor.execute(
            """
            UPDATE sales_pipeline_run
            SET status = %s,
                stage_count = %s,
                generated_count = %s,
                failed_count = %s,
                updated_at = NOW()
            WHERE run_id = %s
            RETURNING run_id, run_key, pipeline_name, status, stage_count,
                      generated_count, failed_count, no_booking, output_dir,
                      created_at, updated_at
            """,
            (run_status, stage_count, generated_count, failed_count, int(run_id)),
        )
        run_row = cursor.fetchone()

    return {
        "run_id": int(run_row[0]),
        "run_key": run_row[1],
        "pipeline_name": run_row[2],
        "status": run_row[3],
        "stage_count": int(run_row[4] or 0),
        "generated_count": int(run_row[5] or 0),
        "failed_count": int(run_row[6] or 0),
        "no_booking": bool(run_row[7]),
        "output_dir": run_row[8],
        "created_at": run_row[9],
        "updated_at": run_row[10],
    }


def _sync_sales_pipeline_stage_internal(
    connection: Any,
    *,
    run_key: str,
    stage_name: str,
    payload: SalesPipelineStageSyncRequest | SalesPipelineStageBulkSyncItem,
) -> dict[str, Any]:
    key = str(run_key or "").strip()
    stage = str(stage_name or "").strip().lower()
    if not key:
        raise HTTPException(status_code=400, detail="run_key is required")
    if not stage:
        raise HTTPException(status_code=400, detail="stage_name is required")

    normalized_status = str(payload.status or "").strip().lower()
    if normalized_status and not re.fullmatch(r"[a-z0-9_-]+", normalized_status):
        raise HTTPException(status_code=400, detail="status must contain only letters, numbers, underscore, or hyphen")

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT r.run_id, s.stage_order, s.stage_name, s.reference_name, s.status, s.result
            FROM sales_pipeline_run r
            JOIN sales_pipeline_stage s ON s.run_id = r.run_id
            WHERE r.run_key = %s
              AND lower(s.stage_name) = %s
            ORDER BY s.stage_order ASC
            LIMIT 1
            """,
            (key, stage),
        )
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Stage '{stage}' not found for run '{key}'")

    run_id = int(row[0])
    stage_order = int(row[1])
    persisted_stage_name = str(row[2])
    reference_name = str(row[3] or "")
    current_status = str(row[4] or "pending").strip().lower() or "pending"
    current_result = row[5] if isinstance(row[5], dict) else {}

    idempotency_key = str(getattr(payload, "idempotency_key", "") or "").strip()
    processed_keys_raw = current_result.get("processed_idempotency_keys")
    processed_keys = [str(item).strip() for item in processed_keys_raw] if isinstance(processed_keys_raw, list) else []
    if idempotency_key and idempotency_key in processed_keys:
        return {
            "run_id": run_id,
            "stage_order": stage_order,
            "stage_name": persisted_stage_name,
            "reference_name": reference_name,
            "status": current_status,
            "error_message": None,
            "result": current_result,
            "idempotency_key": idempotency_key,
            "skipped": True,
            "skip_reason": "duplicate_idempotency_key",
        }

    merged_result = dict(current_result)
    if isinstance(payload.result, dict) and payload.result:
        merged_result.update(payload.result)
    if str(payload.external_event or "").strip():
        merged_result["external_event"] = str(payload.external_event).strip()
    if str(payload.external_reference or "").strip():
        merged_result["external_reference"] = str(payload.external_reference).strip()
    if idempotency_key:
        deduped = []
        for item in [*processed_keys, idempotency_key]:
            token = str(item).strip()
            if token and token not in deduped:
                deduped.append(token)
        merged_result["processed_idempotency_keys"] = deduped
    merged_result["last_synced_at"] = _utc_iso_now()

    next_status = normalized_status or current_status
    error_message = str(payload.error_message or "").strip() or None
    if next_status != "failed" and error_message:
        merged_result["sync_note"] = error_message
        error_message = None

    _upsert_sales_pipeline_stage(
        connection,
        run_id=run_id,
        stage_order=stage_order,
        stage_name=persisted_stage_name,
        reference_name=reference_name,
        status=next_status,
        error_message=error_message,
        result=merged_result,
    )

    return {
        "run_id": run_id,
        "stage_order": stage_order,
        "stage_name": persisted_stage_name,
        "reference_name": reference_name,
        "status": next_status,
        "error_message": error_message,
        "result": merged_result,
        "idempotency_key": idempotency_key or None,
        "skipped": False,
    }


def _load_template_reference_registry() -> list[dict[str, Any]]:
    if not TEMPLATE_REFERENCE_REGISTRY.exists():
        return []
    try:
        raw = json.loads(TEMPLATE_REFERENCE_REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _save_template_reference_registry(rows: list[dict[str, Any]]) -> None:
    TEMPLATE_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATE_REFERENCE_REGISTRY.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _find_template_reference(reference_name: str) -> dict[str, Any] | None:
    normalized = str(reference_name or "").strip().lower()
    if not normalized:
        return None
    for item in _load_template_reference_registry():
        if str(item.get("reference_name") or "").strip().lower() == normalized:
            return item
    return None


def _render_csv_from_context(payload: dict[str, Any]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["field", "value"])
    line_items = payload.get("line_items") if isinstance(payload.get("line_items"), list) else []
    for key, value in payload.items():
        if key == "line_items":
            continue
        writer.writerow([str(key), json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value or "")])
    if line_items:
        writer.writerow([])
        writer.writerow(["line_item_index", "description", "quantity", "unit_price", "amount", "vat_rate", "vat_amount"])
        for idx, item in enumerate(line_items, start=1):
            row = item if isinstance(item, dict) else {}
            writer.writerow([
                idx,
                str(row.get("description") or ""),
                str(row.get("quantity") or ""),
                str(row.get("unit_price") or ""),
                str(row.get("amount") or ""),
                str(row.get("vat_rate") or ""),
                str(row.get("vat_amount") or ""),
            ])
    return output.getvalue().encode("utf-8-sig")


def _apply_specification_overrides(payload: dict[str, Any], specification: str) -> dict[str, Any]:
    out = dict(payload)
    spec = str(specification or "").strip().lower()
    if not spec:
        return out
    if "current date" in spec or "invoice date" in spec:
        today = date.today().isoformat()
        out.setdefault("issue_date", today)
        out.setdefault("invoice_date", today)
    return out


def _generate_from_reference_internal(
    *,
    reference_name: str,
    specification: str,
    output_format: str,
    document_kind: str,
    payload: dict[str, Any],
    output_path: str,
    output_dir: str,
) -> dict[str, Any]:
    entry = _find_template_reference(reference_name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Template reference '{reference_name}' not found")

    template_path = Path(str(entry.get("stored_path") or "").strip())
    if not template_path.exists():
        raise HTTPException(status_code=404, detail=f"Template file not found at {template_path}")

    template_format = str(entry.get("template_format") or template_path.suffix.lstrip(".") or "").strip().lower()
    doc_kind = str(document_kind or entry.get("document_kind") or "invoice").strip().lower() or "invoice"
    effective_payload = dict(entry.get("base_payload") if isinstance(entry.get("base_payload"), dict) else {})
    if isinstance(payload, dict):
        effective_payload.update(payload)
    effective_payload = _apply_specification_overrides(effective_payload, specification)

    requested_format = str(output_format or "pdf").strip().lower() or "pdf"

    if template_format in {"xlsx", "xls", "xlsm"}:
        if requested_format in {"excel", "csv", "xlsx"}:
            content = _render_csv_from_context(effective_payload)
            filename = f"{_slugify(reference_name)}-generated.csv"
            media_type = "text/csv; charset=utf-8"
        else:
            raise HTTPException(
                status_code=400,
                detail="Excel references currently support csv/excel output only; format-preserving xlsx rendering is not implemented.",
            )
    else:
        if template_format not in {"pdf", "docx"}:
            raise HTTPException(status_code=400, detail=f"Unsupported template format: {template_format}")
        normalized_output_format = requested_format
        if normalized_output_format == "excel":
            normalized_output_format = "pdf"
        try:
            content, filename, media_type = document_template_pipeline.render_template_document(
                template_bytes=template_path.read_bytes(),
                template_format=template_format,
                output_format=normalized_output_format,
                document_kind=doc_kind,
                payload=effective_payload,
                qr_data=effective_payload.get("qr_data") if isinstance(effective_payload.get("qr_data"), dict) else None,
                layout=effective_payload.get("layout") if isinstance(effective_payload.get("layout"), dict) else None,
                template_name=template_path.name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    target = _resolve_output_file_path(
        filename=filename,
        output_path=output_path,
        output_dir=output_dir,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)

    return {
        "reference_name": reference_name,
        "doc_name": entry.get("doc_name") or reference_name,
        "document_kind": doc_kind,
        "specification": specification,
        "saved_path": str(target),
        "filename": filename,
        "media_type": media_type,
        "size_bytes": len(content),
        "no_booking": True,
        "source_template_path": str(template_path),
    }


def _resolve_output_file_path(
    *,
    filename: str,
    output_path: str = "",
    output_dir: str = "",
) -> Path:
    requested_path = str(output_path or "").strip()
    requested_dir = str(output_dir or "").strip()

    if requested_path:
        candidate = Path(requested_path)
        if not candidate.is_absolute():
            candidate = (PROJECT_ROOT / candidate).resolve()
        return candidate

    if requested_dir:
        base_dir = Path(requested_dir)
        if not base_dir.is_absolute():
            base_dir = (PROJECT_ROOT / base_dir).resolve()
    else:
        base_dir = (PROJECT_ROOT / "generated" / "docs").resolve()

    return base_dir / filename


@router.delete("/{doc_id}")
def delete_document(doc_id: int) -> dict[str, Any]:
    connection = object_db.get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT doc_id, doc_key, doc_name, doc_path, markdown_path FROM document WHERE doc_id = %s", (doc_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"document {doc_id} not found")

            document_row = {
                "doc_id": int(row[0]),
                "doc_key": row[1],
                "doc_name": row[2],
                "doc_path": row[3],
                "markdown_path": row[4],
            }

            markdown_path = str(row[4] or "").strip()
            if markdown_path:
                try:
                    markdown_file = Path(markdown_path)
                    if markdown_file.exists():
                        markdown_file.unlink()
                except Exception:
                    LOGGER.warning("Failed to remove markdown cache file for doc_id=%s path=%s", doc_id, markdown_path)

            cursor.execute("DELETE FROM document WHERE doc_id = %s", (doc_id,))
            deleted = int(cursor.rowcount)
        connection.commit()
        return {
            "success": True,
            "deleted": deleted,
            "document": document_row,
        }
    except HTTPException:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        LOGGER.exception("Failed to delete document %s", doc_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        connection.close()


@router.get("/{doc_id}/export")
def export_document(doc_id: int, format: str = "pdf") -> Response:
    connection = object_db.get_connection()
    try:
        try:
            payload = document_generation.load_document_export_payload(connection, doc_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"document {doc_id} not found")

        try:
            content, filename, media_type = document_generation.render_document_export(
                payload.get("document") or {},
                payload.get("parts") or [],
                format,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        return Response(content=content, media_type=media_type, headers=headers)
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception("Failed to export document %s", doc_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        connection.close()


@router.post("/{doc_id}/export/save")
def export_document_to_path(doc_id: int, payload: DocumentExportSaveRequest) -> dict[str, Any]:
    connection = object_db.get_connection()
    try:
        try:
            document_payload = document_generation.load_document_export_payload(connection, doc_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"document {doc_id} not found")

        try:
            content, filename, media_type = document_generation.render_document_export(
                document_payload.get("document") or {},
                document_payload.get("parts") or [],
                payload.format,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        target = _resolve_output_file_path(
            filename=filename,
            output_path=payload.output_path,
            output_dir=payload.output_dir,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

        return {
            "success": True,
            "doc_id": int(doc_id),
            "format": str(payload.format or "pdf").strip().lower() or "pdf",
            "filename": filename,
            "media_type": media_type,
            "saved_path": str(target),
            "size_bytes": len(content),
            "path_mode": "absolute" if Path(str(payload.output_path or "")).is_absolute() else "project_relative_or_default",
            "project_root": str(PROJECT_ROOT),
        }
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception("Failed to export document %s to path", doc_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        connection.close()


@router.post("/template-references/ingest")
async def ingest_template_reference(
    reference_file: UploadFile = File(...),
    reference_name: str = Form(...),
    doc_name: str = Form(default=""),
    document_kind: str = Form(default="invoice"),
    description: str = Form(default=""),
    payload_json: str = Form(default="{}"),
) -> dict[str, Any]:
    reference_name_clean = str(reference_name or "").strip()
    if not reference_name_clean:
        raise HTTPException(status_code=400, detail="reference_name is required")

    source_name = str(reference_file.filename or "template.bin").strip() or "template.bin"
    ext = Path(source_name).suffix.lower()
    if ext not in {".pdf", ".docx", ".xlsx", ".xls", ".xlsm"}:
        raise HTTPException(status_code=400, detail="reference_file must be pdf, docx, xlsx, xls, or xlsm")

    payload = parse_json_dict(payload_json, "payload_json")
    raw = await reference_file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="reference_file is empty")

    TEMPLATE_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = _slugify(reference_name_clean)
    stored_name = f"{safe_name}{ext}"
    stored_path = (TEMPLATE_REFERENCE_DIR / stored_name).resolve()
    stored_path.write_bytes(raw)

    registry = _load_template_reference_registry()
    entry = {
        "reference_name": reference_name_clean,
        "doc_name": str(doc_name or reference_name_clean).strip() or reference_name_clean,
        "document_kind": str(document_kind or "invoice").strip().lower() or "invoice",
        "description": str(description or "").strip(),
        "source_filename": source_name,
        "stored_path": str(stored_path),
        "template_format": ext.lstrip("."),
        "base_payload": payload,
        "no_booking": True,
    }

    updated = False
    for idx, item in enumerate(registry):
        if str(item.get("reference_name") or "").strip().lower() == reference_name_clean.lower():
            registry[idx] = entry
            updated = True
            break
    if not updated:
        registry.append(entry)
    _save_template_reference_registry(registry)

    return {
        "success": True,
        "message": "Template reference stored for future document generation.",
        "result": entry,
    }


@router.get("/template-references")
def list_template_references() -> dict[str, Any]:
    rows = _load_template_reference_registry()
    return {
        "success": True,
        "count": len(rows),
        "result": rows,
    }


@router.post("/template-references/generate")
def generate_from_template_reference(payload: TemplateReferenceGenerateRequest) -> dict[str, Any]:
    result = _generate_from_reference_internal(
        reference_name=payload.reference_name,
        specification=payload.specification,
        output_format=payload.output_format,
        document_kind=payload.document_kind,
        payload=payload.payload,
        output_path=payload.output_path,
        output_dir=payload.output_dir,
    )

    return {
        "success": True,
        "result": result,
    }


@router.post("/template-references/run-sales-pipeline")
def run_sales_pipeline_from_references(payload: SalesPipelineGenerateRequest) -> dict[str, Any]:
    stage_order = [str(item or "").strip().lower() for item in (payload.stages or []) if str(item or "").strip()]
    if not stage_order:
        stage_order = ["quotation", "order_confirmation", "delivery", "invoice", "payment"]

    pipeline_slug = _slugify(payload.pipeline_name or "sales_pipeline")
    base_output_dir = str(payload.output_dir or "").strip() or f"generated/docs/{pipeline_slug}"

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    connection = object_db.get_connection()
    try:
        _ensure_sales_pipeline_tables(connection)
        run_record = _create_sales_pipeline_run(
            connection,
            pipeline_name=str(payload.pipeline_name or "sales_pipeline").strip() or "sales_pipeline",
            stage_count=len(stage_order),
            output_dir=base_output_dir,
            payload={
                "stages": stage_order,
                "stage_reference_names": payload.stage_reference_names,
                "specification": payload.specification,
                "stage_specifications": payload.stage_specifications,
                "output_format": payload.output_format,
                "common_payload": payload.common_payload,
                "stage_payloads": payload.stage_payloads,
                "continue_on_error": payload.continue_on_error,
            },
        )

        for index, stage in enumerate(stage_order, start=1):
            ref_name = str((payload.stage_reference_names or {}).get(stage) or "").strip()
            if not ref_name:
                error_text = f"No template reference specified for stage '{stage}'."
                failures.append({
                    "stage": stage,
                    "error": error_text,
                })
                _upsert_sales_pipeline_stage(
                    connection,
                    run_id=run_record["run_id"],
                    stage_order=index,
                    stage_name=stage,
                    reference_name=ref_name,
                    status="failed",
                    error_message=error_text,
                    result={"stage": stage, "error": error_text},
                )
                if not payload.continue_on_error:
                    break
                continue

            stage_payload = dict(payload.common_payload or {})
            stage_overlay = (payload.stage_payloads or {}).get(stage)
            if isinstance(stage_overlay, dict):
                stage_payload.update(stage_overlay)
            stage_payload.setdefault("sales_stage", stage)

            stage_spec = str((payload.stage_specifications or {}).get(stage) or payload.specification or "").strip()
            stage_out_dir = f"{base_output_dir}/{index:02d}-{_slugify(stage)}"

            _upsert_sales_pipeline_stage(
                connection,
                run_id=run_record["run_id"],
                stage_order=index,
                stage_name=stage,
                reference_name=ref_name,
                status="running",
                result={"stage": stage, "started_at": _utc_iso_now()},
            )

            try:
                generated = _generate_from_reference_internal(
                    reference_name=ref_name,
                    specification=stage_spec,
                    output_format=payload.output_format,
                    document_kind=stage,
                    payload=stage_payload,
                    output_path="",
                    output_dir=stage_out_dir,
                )
                results.append({"stage": stage, "result": generated})
                _upsert_sales_pipeline_stage(
                    connection,
                    run_id=run_record["run_id"],
                    stage_order=index,
                    stage_name=stage,
                    reference_name=ref_name,
                    status="generated",
                    result=generated,
                )
            except HTTPException as exc:
                error_text = str(exc.detail)
                failures.append({"stage": stage, "error": error_text})
                _upsert_sales_pipeline_stage(
                    connection,
                    run_id=run_record["run_id"],
                    stage_order=index,
                    stage_name=stage,
                    reference_name=ref_name,
                    status="failed",
                    error_message=error_text,
                    result={"stage": stage, "error": error_text},
                )
                if not payload.continue_on_error:
                    break
            except Exception as exc:
                error_text = str(exc)
                failures.append({"stage": stage, "error": error_text})
                _upsert_sales_pipeline_stage(
                    connection,
                    run_id=run_record["run_id"],
                    stage_order=index,
                    stage_name=stage,
                    reference_name=ref_name,
                    status="failed",
                    error_message=error_text,
                    result={"stage": stage, "error": error_text},
                )
                if not payload.continue_on_error:
                    break

        _finalize_sales_pipeline_run(
            connection,
            run_id=run_record["run_id"],
            generated_count=len(results),
            failed_count=len(failures),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "success": len(failures) == 0,
        "run_id": run_record.get("run_id"),
        "run_key": run_record.get("run_key"),
        "pipeline_name": payload.pipeline_name,
        "stages": stage_order,
        "generated_count": len(results),
        "failed_count": len(failures),
        "results": results,
        "failures": failures,
        "no_booking": True,
        "output_dir": base_output_dir,
    }


@router.get("/template-references/sales-pipeline-runs")
def list_sales_pipeline_runs(limit: int = 50) -> dict[str, Any]:
    effective_limit = max(1, min(int(limit or 50), 500))
    connection = object_db.get_connection()
    try:
        _ensure_sales_pipeline_tables(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id, run_key, pipeline_name, status, stage_count,
                       generated_count, failed_count, no_booking, output_dir,
                       created_at, updated_at
                FROM sales_pipeline_run
                ORDER BY run_id DESC
                LIMIT %s
                """,
                (effective_limit,),
            )
            rows = cursor.fetchall() or []

        result = [
            {
                "run_id": int(row[0]),
                "run_key": row[1],
                "pipeline_name": row[2],
                "status": row[3],
                "stage_count": int(row[4] or 0),
                "generated_count": int(row[5] or 0),
                "failed_count": int(row[6] or 0),
                "no_booking": bool(row[7]),
                "output_dir": row[8],
                "created_at": row[9],
                "updated_at": row[10],
            }
            for row in rows
        ]
        return {"success": True, "count": len(result), "result": result}
    except Exception as exc:
        LOGGER.exception("Failed to list sales pipeline runs")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        connection.close()


@router.get("/template-references/sales-pipeline-runs/{run_key}")
def get_sales_pipeline_run(run_key: str) -> dict[str, Any]:
    key = str(run_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="run_key is required")

    connection = object_db.get_connection()
    try:
        _ensure_sales_pipeline_tables(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id, run_key, pipeline_name, status, stage_count,
                       generated_count, failed_count, no_booking, output_dir,
                       payload, created_at, updated_at
                FROM sales_pipeline_run
                WHERE run_key = %s
                LIMIT 1
                """,
                (key,),
            )
            run_row = cursor.fetchone()
            if not run_row:
                raise HTTPException(status_code=404, detail=f"Sales pipeline run '{key}' not found")

            cursor.execute(
                """
                SELECT stage_order, stage_name, reference_name, status, error_message,
                       saved_path, filename, media_type, size_bytes, result,
                       created_at, updated_at
                FROM sales_pipeline_stage
                WHERE run_id = %s
                ORDER BY stage_order ASC
                """,
                (int(run_row[0]),),
            )
            stage_rows = cursor.fetchall() or []

        run_payload = run_row[9] if isinstance(run_row[9], dict) else {}
        stages = [
            {
                "stage_order": int(row[0]),
                "stage_name": row[1],
                "reference_name": row[2],
                "status": row[3],
                "error_message": row[4],
                "saved_path": row[5],
                "filename": row[6],
                "media_type": row[7],
                "size_bytes": int(row[8] or 0) if row[8] is not None else None,
                "result": row[9] if isinstance(row[9], dict) else {},
                "created_at": row[10],
                "updated_at": row[11],
            }
            for row in stage_rows
        ]

        return {
            "success": True,
            "result": {
                "run_id": int(run_row[0]),
                "run_key": run_row[1],
                "pipeline_name": run_row[2],
                "status": run_row[3],
                "stage_count": int(run_row[4] or 0),
                "generated_count": int(run_row[5] or 0),
                "failed_count": int(run_row[6] or 0),
                "no_booking": bool(run_row[7]),
                "output_dir": run_row[8],
                "payload": run_payload,
                "created_at": run_row[10],
                "updated_at": run_row[11],
                "stages": stages,
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception("Failed to load sales pipeline run")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        connection.close()


@router.patch("/template-references/sales-pipeline-runs/{run_key}/stages/{stage_name}")
def sync_sales_pipeline_stage(
    run_key: str,
    stage_name: str,
    payload: SalesPipelineStageSyncRequest,
) -> dict[str, Any]:
    connection = object_db.get_connection()
    try:
        _ensure_sales_pipeline_tables(connection)
        stage_result = _sync_sales_pipeline_stage_internal(
            connection,
            run_key=run_key,
            stage_name=stage_name,
            payload=payload,
        )
        run_summary = _refresh_sales_pipeline_run_aggregate(connection, run_id=stage_result["run_id"])
        connection.commit()

        return {
            "success": True,
            "run": run_summary,
            "stage": {k: v for k, v in stage_result.items() if k != "run_id"},
        }
    except HTTPException:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        LOGGER.exception("Failed to sync sales pipeline stage")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        connection.close()


@router.patch("/template-references/sales-pipeline-runs/{run_key}/stages/sync-bulk")
def sync_sales_pipeline_stages_bulk(run_key: str, payload: SalesPipelineStageBulkSyncRequest) -> dict[str, Any]:
    updates = payload.updates or []
    if not updates:
        raise HTTPException(status_code=400, detail="updates must contain at least one stage update")

    connection = object_db.get_connection()
    try:
        _ensure_sales_pipeline_tables(connection)
        applied: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        skipped_count = 0
        run_id: int | None = None

        for item in updates:
            try:
                stage_result = _sync_sales_pipeline_stage_internal(
                    connection,
                    run_key=run_key,
                    stage_name=item.stage_name,
                    payload=item,
                )
                run_id = stage_result["run_id"]
                applied.append({k: v for k, v in stage_result.items() if k != "run_id"})
                if bool(stage_result.get("skipped")):
                    skipped_count += 1
            except HTTPException as exc:
                failures.append(
                    {
                        "stage_name": str(item.stage_name or "").strip().lower(),
                        "error": str(exc.detail),
                        "status_code": int(exc.status_code),
                    }
                )
                if not payload.continue_on_error:
                    break

        if run_id is None:
            raise HTTPException(status_code=404, detail=f"Sales pipeline run '{run_key}' not found or no valid stage update was applied")

        run_summary = _refresh_sales_pipeline_run_aggregate(connection, run_id=run_id)
        connection.commit()

        return {
            "success": len(failures) == 0,
            "run": run_summary,
            "applied_count": len(applied),
            "skipped_count": skipped_count,
            "failed_count": len(failures),
            "applied": applied,
            "failures": failures,
        }
    except HTTPException:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        LOGGER.exception("Failed to bulk sync sales pipeline stages")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        connection.close()


@router.post("/template-render")
async def render_document_template(
    template_file: UploadFile = File(...),
    template_format: str = Form(default="docx"),
    output_format: str = Form(default="pdf"),
    document_kind: str = Form(default="invoice"),
    payload_json: str = Form(default="{}"),
    qr_json: str = Form(default="{}"),
    layout_json: str = Form(default="{}"),
) -> Response:
    template_bytes = await template_file.read()
    payload = parse_json_dict(payload_json, "payload_json")
    qr_data = parse_json_dict(qr_json, "qr_json")
    layout = parse_json_dict(layout_json, "layout_json")

    try:
        content, filename, media_type = document_template_pipeline.render_template_document(
            template_bytes=template_bytes,
            template_format=template_format,
            output_format=output_format,
            document_kind=document_kind,
            payload=payload,
            qr_data=qr_data if qr_data else None,
            layout=layout if layout else None,
            template_name=template_file.filename or "template",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        LOGGER.exception("Failed to render template document")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    return Response(content=content, media_type=media_type, headers=headers)