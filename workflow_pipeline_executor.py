"""Workflow pipeline executor with pause/resume support.

A pipeline run executes a sequence of steps defined in solf_workflow_steps.
Any step may pause the run when:
  - user interaction is required (e.g. a confirmation or a missing field value)
  - insufficient data exists in the database (e.g. a document not yet ingested)
  - explicit approval is required before proceeding

When paused, the run stores the pause reason, what is needed, and resumes from
the same step once the caller supplies the missing information via resume_pipeline_run().

Step kinds supported
--------------------
clause          – Call a SOLF clause by name. The clause receives the current context dict
                  as its sole argument. Its return value is merged into the context.
python_binding  – Call module.function(context, config). The function may raise
                  PipelineStepPauseRequired to signal a pause.
class_transform – Reserved for future use; treated as a no-op with a log warning.
class_generate  – Reserved for future use; treated as a no-op with a log warning.
class_iterate   – Reserved for future use; treated as a no-op with a log warning.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import hashlib
import logging
import re
import time
from types import MappingProxyType
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import object_db

LOGGER = logging.getLogger("idms.pipeline")

_BUILTIN_STEP_MODULE = "workflow_pipeline_builtin_steps"

_CLARIFICATION_TEXT_KEYS = (
    "clarification_note",
    "note_text",
    "explanation",
    "comment",
    "notes",
)


_GENERATED_SCRIPT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS workflow_generated_python_script (
    script_id BIGSERIAL PRIMARY KEY,
    script_key VARCHAR(256) NOT NULL UNIQUE,
    script_name VARCHAR(256) NOT NULL,
    script_source TEXT NOT NULL,
    entrypoint VARCHAR(128) NOT NULL DEFAULT 'run',
    approval_status VARCHAR(32) NOT NULL DEFAULT 'draft' CHECK (approval_status IN ('draft','approved','deprecated','archived')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wgps_script_key ON workflow_generated_python_script(script_key);
CREATE INDEX IF NOT EXISTS idx_wgps_approval_status ON workflow_generated_python_script(approval_status);
CREATE INDEX IF NOT EXISTS idx_wgps_is_active ON workflow_generated_python_script(is_active);
CREATE INDEX IF NOT EXISTS idx_wgps_metadata_gin ON workflow_generated_python_script USING GIN(metadata);
"""


_GENERATED_SCRIPT_AUDIT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS workflow_generated_python_script_audit (
    audit_id BIGSERIAL PRIMARY KEY,
    run_id BIGINT,
    step_key VARCHAR(256),
    script_id BIGINT REFERENCES workflow_generated_python_script(script_id) ON DELETE SET NULL,
    script_key VARCHAR(256),
    entrypoint VARCHAR(128),
    execution_status VARCHAR(32) NOT NULL,
    duration_ms INTEGER,
    input_hash VARCHAR(64),
    output_hash VARCHAR(64),
    input_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key VARCHAR(256),
    executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wgps_audit_run_id ON workflow_generated_python_script_audit(run_id);
CREATE INDEX IF NOT EXISTS idx_wgps_audit_step_key ON workflow_generated_python_script_audit(step_key);
CREATE INDEX IF NOT EXISTS idx_wgps_audit_script_key ON workflow_generated_python_script_audit(script_key);
CREATE INDEX IF NOT EXISTS idx_wgps_audit_status ON workflow_generated_python_script_audit(execution_status);
CREATE INDEX IF NOT EXISTS idx_wgps_audit_executed_at ON workflow_generated_python_script_audit(executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_wgps_audit_metadata_gin ON workflow_generated_python_script_audit USING GIN(metadata);
"""


def _ensure_generated_script_table(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(_GENERATED_SCRIPT_TABLE_DDL)


def _ensure_generated_script_audit_table(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(_GENERATED_SCRIPT_AUDIT_TABLE_DDL)
        cursor.execute(
            "ALTER TABLE IF EXISTS workflow_generated_python_script_audit ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(256)"
        )
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_wgps_audit_idempotency
            ON workflow_generated_python_script_audit(run_id, step_key, script_key, idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )


def _stable_hash_payload(value: Any) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        payload = repr(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_generated_script_execution_audit(
    connection: Any,
    *,
    run_id: int | None,
    step_key: str,
    script_id: int | None,
    script_key: str,
    entrypoint: str,
    execution_status: str,
    duration_ms: int,
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
    error_message: str | None,
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    _ensure_generated_script_audit_table(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workflow_generated_python_script_audit (
                run_id, step_key, script_id, script_key, entrypoint,
                execution_status, duration_ms, input_hash, output_hash,
                input_payload, output_payload, error_message, metadata, idempotency_key, executed_at
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, NOW()
            )
            ON CONFLICT (run_id, step_key, script_key, idempotency_key)
            WHERE idempotency_key IS NOT NULL
            DO NOTHING
            """,
            (
                int(run_id) if run_id is not None else None,
                str(step_key or "").strip() or None,
                int(script_id) if script_id is not None else None,
                str(script_key or "").strip() or None,
                str(entrypoint or "").strip() or None,
                str(execution_status or "").strip().lower() or "unknown",
                int(duration_ms or 0),
                _stable_hash_payload(input_payload),
                _stable_hash_payload(output_payload),
                json.dumps(input_payload or {}),
                json.dumps(output_payload or {}),
                str(error_message or "").strip() or None,
                json.dumps(metadata or {}),
                str(idempotency_key or "").strip() or None,
            ),
        )
        return int(cursor.rowcount or 0) > 0


def _load_generated_script_for_execution(connection: Any, config: dict[str, Any]) -> dict[str, Any] | None:
    script_id_raw = config.get("generated_script_id")
    script_key_raw = config.get("generated_script_key")
    script_id: int | None = None
    try:
        if script_id_raw is not None and str(script_id_raw).strip() != "":
            script_id = int(script_id_raw)
    except (TypeError, ValueError):
        script_id = None

    script_key = str(script_key_raw or "").strip()
    if script_id is None and not script_key:
        return None

    _ensure_generated_script_table(connection)
    with connection.cursor() as cursor:
        if script_id is not None:
            cursor.execute(
                """
                SELECT script_id, script_key, script_name, script_source, entrypoint, approval_status, metadata, is_active
                FROM workflow_generated_python_script
                WHERE script_id = %s
                LIMIT 1
                """,
                (int(script_id),),
            )
        else:
            cursor.execute(
                """
                SELECT script_id, script_key, script_name, script_source, entrypoint, approval_status, metadata, is_active
                FROM workflow_generated_python_script
                WHERE script_key = %s
                LIMIT 1
                """,
                (script_key,),
            )
        row = cursor.fetchone()

    if not row:
        return None

    return {
        "script_id": int(row[0]),
        "script_key": row[1],
        "script_name": row[2],
        "script_source": row[3],
        "entrypoint": row[4],
        "approval_status": row[5],
        "metadata": row[6] or {},
        "is_active": bool(row[7]),
    }


def _extract_amount_candidates(text: str) -> list[str]:
    if not text:
        return []
    pattern = re.compile(r"(?:(?:CHF|EUR|USD)\s*)?[+-]?\d{1,3}(?:[',\s]\d{3})*(?:\.\d{1,2})?|[+-]?\d+(?:\.\d{1,2})")
    values: list[str] = []
    seen: set[str] = set()
    for match in pattern.findall(text):
        value = str(match).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def _build_clarification_context(
    payload: dict[str, Any] | None,
    doc_refs: list[Any] | None,
    source: str,
) -> dict[str, Any] | None:
    data = dict(payload or {})
    if not data and not doc_refs:
        return None

    note_fragments: list[str] = []
    for key in _CLARIFICATION_TEXT_KEYS:
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            note_fragments.append(text)

    joined_note = "\n".join(note_fragments).strip()
    lowered = joined_note.lower()

    issue_flags: list[str] = []
    if any(token in lowered for token in ("allowance", "no receipt", "without receipt", "receipt missing")):
        issue_flags.append("unreceipted_allowance")
    if any(token in lowered for token in ("combined", "multiple receipts", "several receipts", "aggregation")):
        issue_flags.append("multi_receipt_allocation")
    if any(token in lowered for token in ("personal account", "personal transfer", "private account")):
        issue_flags.append("personal_account_transfer")
    if any(token in lowered for token in ("mismatch", "does not match", "unmatched", "difference")):
        issue_flags.append("amount_mismatch")

    clarification = {
        "source": source,
        "note_text": joined_note,
        "doc_refs": list(doc_refs or []),
        "provided_fields": sorted(list(data.keys())),
        "amount_candidates": _extract_amount_candidates(joined_note),
        "issue_flags": issue_flags,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    if not joined_note and not clarification["doc_refs"] and not clarification["provided_fields"]:
        return None
    return clarification


def _build_note_payload_from_doc_refs(db_connection_fn, doc_refs: list[Any] | None) -> dict[str, Any]:
    ids: list[int] = []
    seen: set[int] = set()
    for item in list(doc_refs or []):
        try:
            if isinstance(item, dict):
                raw = item.get("doc_id")
            else:
                raw = item
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        ids.append(value)

    if not ids:
        return {}

    note_fragments: list[str] = []
    tags: list[str] = []
    for doc_id in ids:
        with db_connection_fn() as conn:
            row = object_db.get_document_brief_by_id(conn, doc_id=doc_id)
        if not row:
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}

        for key in ("note_content", "user_description", "doc_desc", "description"):
            value = metadata.get(key)
            if value is None and key in ("doc_desc", "description"):
                value = row.get("doc_desc")
            text = str(value or "").strip()
            if text:
                note_fragments.append(text)
                break

        for key in ("note_tags", "user_tags"):
            value = metadata.get(key)
            if isinstance(value, list):
                for tag in value:
                    text = str(tag or "").strip()
                    if text:
                        tags.append(text)

    if not note_fragments:
        return {}

    joined = "\n\n".join(note_fragments)
    return {
        "clarification_note": joined,
        "notes": joined,
        "clarification_tags": sorted({str(tag).strip() for tag in tags if str(tag).strip()}),
    }

# ---------------------------------------------------------------------------
# Pause signal
# ---------------------------------------------------------------------------


class PipelineStepPauseRequired(Exception):
    """Raised by a python_binding step to signal that the run must be paused.

    Args:
        reason: 'user_interaction' | 'missing_data' | 'approval_required'
        prompt: Human-readable prompt to show the user (for user_interaction)
        missing_data_desc: Description of what data is missing (for missing_data)
        required_doc_types: List of document types that would provide the missing data
    """

    def __init__(
        self,
        reason: str = "user_interaction",
        prompt: str = "",
        missing_data_desc: str = "",
        required_doc_types: list[str] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = str(reason or "user_interaction")
        self.prompt = str(prompt or "")
        self.missing_data_desc = str(missing_data_desc or "")
        self.required_doc_types: list[str] = list(required_doc_types or [])


# ---------------------------------------------------------------------------
# Internal step result
# ---------------------------------------------------------------------------


@dataclass
class _StepOutcome:
    success: bool = False
    output: dict[str, Any] = field(default_factory=dict)
    paused: bool = False
    pause_reason: str | None = None
    interaction_prompt: str | None = None
    missing_data_desc: str | None = None
    required_doc_types: list[str] = field(default_factory=list)
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class WorkflowPipelineExecutor:
    """Execute workflow pipeline runs with pause/resume semantics."""

    def __init__(self, db_connection_fn, solf_interpreter=None):
        """
        Args:
            db_connection_fn: Callable returning a psycopg2 connection.
            solf_interpreter: Optional pre-loaded SOLFInterpreter instance.
                              Required when steps use step_kind='clause'.
        """
        self.db_connection_fn = db_connection_fn
        self.solf_interpreter = solf_interpreter

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_pipeline_run(
        self,
        workflow_version_id: int,
        input_context: dict[str, Any],
        started_by: str | None = None,
        workflow_key: str | None = None,
    ) -> dict[str, Any]:
        """Create and immediately execute a pipeline run.

        Returns the final run state dict (possibly paused after the first blocking step).
        """
        with self.db_connection_fn() as conn:
            steps = object_db.list_solf_workflow_steps(conn, workflow_version_id=workflow_version_id)

        if not steps:
            return {"error": "no_steps", "message": f"No steps found for workflow_version_id={workflow_version_id}"}

        with self.db_connection_fn() as conn:
            run_id = object_db.create_pipeline_run(
                conn,
                workflow_version_id=workflow_version_id,
                workflow_key=workflow_key,
                input_context=input_context,
                started_by=started_by,
            )

            # Persist explicit run-to-document links for auditability and rerun checks.
            object_db.link_pipeline_run_documents(
                conn,
                run_id=run_id,
                doc_refs=input_context.get("doc_refs") if isinstance(input_context, dict) else None,
                source="input_context",
                linked_by=started_by,
                metadata={"event": "run_start"},
            )

        initial_context = dict(input_context)
        initial_clarification = _build_clarification_context(
            payload=input_context if isinstance(input_context, dict) else {},
            doc_refs=(input_context or {}).get("doc_refs") if isinstance(input_context, dict) else None,
            source="run_start",
        )
        if initial_clarification is not None:
            history = list(initial_context.get("clarification_history") or [])
            history.append(initial_clarification)
            initial_context["clarification_history"] = history
            initial_context["clarification_latest"] = initial_clarification

        LOGGER.info("pipeline_run start run_id=%s workflow_version_id=%s", run_id, workflow_version_id)
        return self._execute_run(run_id, steps, initial_context)

    def resume_pipeline_run(
        self,
        run_id: int,
        user_response: dict[str, Any] | None = None,
        doc_refs: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Resume a paused run, optionally supplying user answers or new document refs.

        The paused step's status is reset to 'pending' and execution continues
        from that step with the enriched context.
        """
        with self.db_connection_fn() as conn:
            run = object_db.get_pipeline_run(conn, run_id)

        if run is None:
            return {"error": "not_found", "message": f"Pipeline run {run_id} not found"}

        if run["run_status"] != "paused":
            return {
                "error": "not_paused",
                "message": f"Run {run_id} is in status '{run['run_status']}', not paused",
            }

        paused_step_key = run.get("paused_at_step_key")
        if not paused_step_key:
            return {"error": "no_paused_step", "message": "Run is marked paused but has no paused_at_step_key"}

        # Store user response in the paused step row
        if user_response or doc_refs:
            with self.db_connection_fn() as conn:
                object_db.supply_pipeline_step_response(
                    conn,
                    run_id=run_id,
                    step_key=paused_step_key,
                    user_response=user_response,
                    doc_refs=doc_refs,
                )

                object_db.link_pipeline_run_documents(
                    conn,
                    run_id=run_id,
                    doc_refs=doc_refs,
                    source="resume",
                    step_key=paused_step_key,
                    metadata={"event": "run_resume"},
                )

        # Reload steps from DB (in case workflow version was updated)
        wv_id = run.get("workflow_version_id")
        with self.db_connection_fn() as conn:
            steps = object_db.list_solf_workflow_steps(conn, workflow_version_id=wv_id) if wv_id else []

        if not steps:
            return {"error": "no_steps", "message": "No steps found for this run's workflow version"}

        # Determine which steps remain (from paused_step_key onward)
        step_keys = [s["step_key"] for s in steps]
        try:
            resume_idx = step_keys.index(paused_step_key)
        except ValueError:
            resume_idx = 0

        remaining_steps = steps[resume_idx:]
        context = dict(run.get("current_context") or {})

        # Enrich context with user-supplied response
        if user_response:
            context.update(user_response)
        if doc_refs:
            context.setdefault("_resumed_doc_refs", [])
            context["_resumed_doc_refs"] = list(context["_resumed_doc_refs"]) + list(doc_refs)

        note_payload = _build_note_payload_from_doc_refs(self.db_connection_fn, doc_refs)
        merged_payload = dict(note_payload)
        if user_response:
            merged_payload.update(user_response)

        clarification = _build_clarification_context(
            payload=merged_payload,
            doc_refs=doc_refs,
            source="resume",
        )
        if clarification is not None:
            history = list(context.get("clarification_history") or [])
            history.append(clarification)
            context["clarification_history"] = history
            context["clarification_latest"] = clarification

        LOGGER.info("pipeline_run resume run_id=%s from_step=%s", run_id, paused_step_key)
        return self._execute_run(run_id, remaining_steps, context, resuming=True)

    def cancel_pipeline_run(self, run_id: int) -> bool:
        with self.db_connection_fn() as conn:
            run = object_db.get_pipeline_run(conn, run_id)
            if run is None:
                return False
            if run["run_status"] in {"completed", "failed", "cancelled"}:
                return False
            object_db.update_pipeline_run_status(conn, run_id, "cancelled", finished=True)
        LOGGER.info("pipeline_run cancelled run_id=%s", run_id)
        return True

    def get_run_state(self, run_id: int) -> dict[str, Any] | None:
        with self.db_connection_fn() as conn:
            run = object_db.get_pipeline_run(conn, run_id)
            if run is None:
                return None
            steps = object_db.get_pipeline_run_steps(conn, run_id)
        run["steps"] = steps
        return run

    def list_runs(
        self,
        run_status: str | None = None,
        workflow_key: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.db_connection_fn() as conn:
            return object_db.list_pipeline_runs(conn, run_status=run_status, workflow_key=workflow_key, limit=limit)

    # ------------------------------------------------------------------
    # Internal execution loop
    # ------------------------------------------------------------------

    def _execute_run(
        self,
        run_id: int,
        steps: list[dict[str, Any]],
        context: dict[str, Any],
        resuming: bool = False,
    ) -> dict[str, Any]:
        # Mark run as running
        with self.db_connection_fn() as conn:
            object_db.update_pipeline_run_status(conn, run_id, "running", current_context=context)

        for step in steps:
            step_key = step["step_key"]
            step_order = int(step.get("step_order") or 0)
            step_kind = str(step.get("step_kind") or "clause")

            LOGGER.info("pipeline_run execute run_id=%s step=%s kind=%s", run_id, step_key, step_kind)

            # Load user response / doc_refs stored for this step (resume case)
            prior_user_response: dict[str, Any] = {}
            prior_doc_refs: list[Any] = []
            if resuming:
                with self.db_connection_fn() as conn:
                    existing_steps = object_db.get_pipeline_run_steps(conn, run_id)
                    for s in existing_steps:
                        if s["step_key"] == step_key:
                            prior_user_response = s.get("user_response") or {}
                            prior_doc_refs = s.get("doc_refs") or []
                            break

            step_context = dict(context)
            if prior_user_response:
                step_context.update(prior_user_response)
            if prior_doc_refs:
                step_context.setdefault("_doc_refs", [])
                step_context["_doc_refs"] = list(step_context["_doc_refs"]) + list(prior_doc_refs)

            # Persist step as running
            with self.db_connection_fn() as conn:
                object_db.upsert_pipeline_run_step(
                    conn,
                    run_id=run_id,
                    step_key=step_key,
                    step_order=step_order,
                    step_status="running",
                    input_snapshot=step_context,
                    increment_attempt=True,
                )

            outcome = self._execute_step(step, step_context, run_id=run_id)

            if outcome.paused:
                # Persist paused state
                with self.db_connection_fn() as conn:
                    object_db.upsert_pipeline_run_step(
                        conn,
                        run_id=run_id,
                        step_key=step_key,
                        step_order=step_order,
                        step_status="paused",
                        output_snapshot={},
                        pause_reason=outcome.pause_reason,
                        interaction_prompt=outcome.interaction_prompt,
                        missing_data_desc=outcome.missing_data_desc,
                        required_doc_types=outcome.required_doc_types,
                    )
                    object_db.update_pipeline_run_status(
                        conn,
                        run_id,
                        "paused",
                        current_context=context,
                        paused_at_step_key=step_key,
                    )

                LOGGER.info(
                    "pipeline_run paused run_id=%s step=%s reason=%s",
                    run_id, step_key, outcome.pause_reason,
                )
                with self.db_connection_fn() as conn:
                    run = object_db.get_pipeline_run(conn, run_id)
                    run["steps"] = object_db.get_pipeline_run_steps(conn, run_id)
                return run

            if not outcome.success:
                error_msg = outcome.error_message or "step failed"
                with self.db_connection_fn() as conn:
                    object_db.upsert_pipeline_run_step(
                        conn,
                        run_id=run_id,
                        step_key=step_key,
                        step_order=step_order,
                        step_status="failed",
                        output_snapshot={},
                        error_message=error_msg,
                    )
                    object_db.update_pipeline_run_status(
                        conn, run_id, "failed",
                        current_context=context, finished=True,
                    )

                LOGGER.error("pipeline_run failed run_id=%s step=%s error=%s", run_id, step_key, error_msg)
                with self.db_connection_fn() as conn:
                    run = object_db.get_pipeline_run(conn, run_id)
                    run["steps"] = object_db.get_pipeline_run_steps(conn, run_id)
                return run

            # Step succeeded — merge outputs into running context
            if isinstance(outcome.output, dict):
                context.update(outcome.output)

            with self.db_connection_fn() as conn:
                object_db.upsert_pipeline_run_step(
                    conn,
                    run_id=run_id,
                    step_key=step_key,
                    step_order=step_order,
                    step_status="completed",
                    output_snapshot=outcome.output or {},
                )

        # All steps completed
        with self.db_connection_fn() as conn:
            object_db.update_pipeline_run_status(
                conn, run_id, "completed",
                current_context=context, output_context=context, finished=True,
            )

        LOGGER.info("pipeline_run completed run_id=%s", run_id)
        with self.db_connection_fn() as conn:
            run = object_db.get_pipeline_run(conn, run_id)
            run["steps"] = object_db.get_pipeline_run_steps(conn, run_id)
        return run

    def _execute_step(self, step: dict[str, Any], context: dict[str, Any], run_id: int | None = None) -> _StepOutcome:
        step_kind = str(step.get("step_kind") or "clause")
        config: dict[str, Any] = step.get("config") or {}

        try:
            if step_kind == "clause":
                return self._execute_clause_step(step, context, config)
            elif step_kind == "python_binding":
                return self._execute_python_step(step, context, config, run_id=run_id)
            else:
                LOGGER.warning(
                    "pipeline step_kind=%s not yet implemented; treating as no-op (step_key=%s)",
                    step_kind, step.get("step_key"),
                )
                return _StepOutcome(success=True, output={})
        except PipelineStepPauseRequired as exc:
            return _StepOutcome(
                success=False,
                paused=True,
                pause_reason=exc.reason,
                interaction_prompt=exc.prompt or None,
                missing_data_desc=exc.missing_data_desc or None,
                required_doc_types=exc.required_doc_types,
            )
        except Exception as exc:
            LOGGER.exception("pipeline step error step_key=%s", step.get("step_key"))
            return _StepOutcome(success=False, error_message=str(exc))

    def _execute_clause_step(
        self, step: dict[str, Any], context: dict[str, Any], config: dict[str, Any]
    ) -> _StepOutcome:
        if self.solf_interpreter is None:
            return _StepOutcome(
                success=False,
                error_message="solf_interpreter not provided; cannot execute clause step",
            )

        clause_name = str(step.get("clause_name") or "").strip()
        if not clause_name:
            return _StepOutcome(success=False, error_message="clause step has no clause_name")

        try:
            result = self.solf_interpreter._invoke_clause(clause_name, [context])
        except Exception as exc:
            raise  # re-raised, caught by _execute_step wrapper

        if isinstance(result, dict):
            # A SOLF clause can signal a pause by returning {paused: true, ...}
            if result.get("paused") is True or result.get("paused") == "true":
                raise PipelineStepPauseRequired(
                    reason=str(result.get("reason") or "user_interaction"),
                    prompt=str(result.get("prompt") or result.get("interaction_prompt") or ""),
                    missing_data_desc=str(result.get("missing_data_desc") or ""),
                    required_doc_types=list(result.get("required_doc_types") or []),
                )
            return _StepOutcome(success=True, output=result)

        if result is None or result is False:
            return _StepOutcome(success=False, error_message=f"SOLF clause '{clause_name}' returned falsy result")

        return _StepOutcome(success=True, output={"result": result})

    def _execute_python_step(
        self,
        step: dict[str, Any],
        context: dict[str, Any],
        config: dict[str, Any],
        run_id: int | None = None,
    ) -> _StepOutcome:
        generated_script = None
        with self.db_connection_fn() as conn:
            generated_script = _load_generated_script_for_execution(conn, config)

        if generated_script is not None:
            audit_started = time.perf_counter()
            step_key = str(step.get("step_key") or "").strip()
            entrypoint = str(config.get("entrypoint") or generated_script.get("entrypoint") or "run").strip() or "run"
            audit_input_payload = {
                "context": context,
                "config": config,
            }
            audit_output_payload: dict[str, Any] = {}
            audit_status = "success"
            audit_error: str | None = None
            explicit_audit_key = str(config.get("audit_idempotency_key") or "").strip() or None
            auto_dedupe = bool(config.get("audit_idempotent", True))

            def _finalize_audit() -> None:
                duration = int((time.perf_counter() - audit_started) * 1000)
                try:
                    with self.db_connection_fn() as conn:
                        _record_generated_script_execution_audit(
                            conn,
                            run_id=run_id,
                            step_key=step_key,
                            script_id=int(generated_script.get("script_id")) if generated_script.get("script_id") is not None else None,
                            script_key=str(generated_script.get("script_key") or ""),
                            entrypoint=entrypoint,
                            execution_status=audit_status,
                            duration_ms=duration,
                            input_payload=audit_input_payload,
                            output_payload=audit_output_payload,
                            error_message=audit_error,
                            idempotency_key=(
                                explicit_audit_key
                                or (
                                    f"{int(run_id)}:{step_key}:{str(generated_script.get('script_key') or '')}:{_stable_hash_payload(audit_input_payload)}"
                                    if auto_dedupe and run_id is not None
                                    else None
                                )
                            ),
                            metadata={"source": "workflow_pipeline_executor", "step_kind": "python_binding"},
                        )
                        conn.commit()
                except Exception:
                    LOGGER.exception("Failed to persist generated script execution audit")

            if not bool(generated_script.get("is_active")):
                audit_status = "error"
                audit_error = f"Generated script '{generated_script.get('script_key')}' is inactive"
                _finalize_audit()
                return _StepOutcome(
                    success=False,
                    error_message=audit_error,
                )
            if str(generated_script.get("approval_status") or "").strip().lower() != "approved":
                audit_status = "error"
                audit_error = f"Generated script '{generated_script.get('script_key')}' is not approved"
                _finalize_audit()
                return _StepOutcome(
                    success=False,
                    error_message=audit_error,
                )

            script_source = str(generated_script.get("script_source") or "")
            script_filename = f"<workflow-generated-script:{generated_script.get('script_id')}>"
            namespace: dict[str, Any] = {
                "__name__": f"idms_generated_script_{generated_script.get('script_id')}",
                "__builtins__": __builtins__,
                "PipelineStepPauseRequired": PipelineStepPauseRequired,
                "SCRIPT_METADATA": MappingProxyType(dict(generated_script.get("metadata") or {})),
            }

            try:
                compiled = compile(script_source, script_filename, "exec")
                exec(compiled, namespace, namespace)
            except Exception as exc:
                audit_status = "error"
                audit_error = f"Generated script compile/exec failure: {exc}"
                _finalize_audit()
                return _StepOutcome(
                    success=False,
                    error_message=audit_error,
                )

            fn = namespace.get(entrypoint)
            if fn is None or not callable(fn):
                audit_status = "error"
                audit_error = f"Entrypoint '{entrypoint}' not found in generated script '{generated_script.get('script_key')}'"
                _finalize_audit()
                return _StepOutcome(
                    success=False,
                    error_message=audit_error,
                )
            try:
                result = fn(context, config)
                if isinstance(result, dict):
                    audit_output_payload = dict(result)
                    if result.get("paused") is True:
                        audit_status = "paused"
                        _finalize_audit()
                        raise PipelineStepPauseRequired(
                            reason=str(result.get("reason") or "user_interaction"),
                            prompt=str(result.get("prompt") or ""),
                            missing_data_desc=str(result.get("missing_data_desc") or ""),
                            required_doc_types=list(result.get("required_doc_types") or []),
                        )
                    _finalize_audit()
                    return _StepOutcome(success=True, output=result)

                audit_output_payload = {"result": result}
                _finalize_audit()
                return _StepOutcome(success=True, output={"result": result})
            except PipelineStepPauseRequired:
                raise
            except Exception as exc:
                audit_status = "error"
                audit_error = str(exc)
                _finalize_audit()
                return _StepOutcome(success=False, error_message=audit_error)

        python_module = str(step.get("python_module") or "").strip()
        python_function = str(step.get("python_function") or "").strip()

        # Allow concise builtin usage from workflow configs:
        # - step.python_function = "require_fields"
        # - step.config.builtin = "require_fields"
        if not python_function:
            python_function = str(config.get("builtin") or "").strip()
        if not python_module:
            python_module = str(config.get("python_module") or "").strip()
        if not python_module:
            python_module = _BUILTIN_STEP_MODULE

        if not python_function:
            return _StepOutcome(
                success=False,
                error_message="python_binding step missing python_module or python_function",
            )

        try:
            mod = importlib.import_module(python_module)
        except ImportError as exc:
            return _StepOutcome(success=False, error_message=f"Cannot import module '{python_module}': {exc}")

        fn = getattr(mod, python_function, None)
        if fn is None:
            return _StepOutcome(
                success=False,
                error_message=f"Function '{python_function}' not found in module '{python_module}'",
            )

        # PipelineStepPauseRequired raised here is caught by _execute_step wrapper
        result = fn(context, config)

        if isinstance(result, dict):
            # Python functions can also use the dict-signal convention
            if result.get("paused") is True:
                raise PipelineStepPauseRequired(
                    reason=str(result.get("reason") or "user_interaction"),
                    prompt=str(result.get("prompt") or ""),
                    missing_data_desc=str(result.get("missing_data_desc") or ""),
                    required_doc_types=list(result.get("required_doc_types") or []),
                )
            return _StepOutcome(success=True, output=result)

        return _StepOutcome(success=True, output={"result": result})
