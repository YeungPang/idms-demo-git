import argparse
import ast
import contextlib
import hashlib
import io
import json
import logging
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg2.extras import Json
from action_tool import ActionTool
from audit_compliance_manager import AuditComplianceManager
from dashboard_api import DashboardAPI
from event_bus import EventBus, handle_resource_conflict, handle_sla_breach, handle_task_overdue
from notifier import create_notifier_from_config
from notifier_engine import NotifierEngine
from project_simulator import ProjectSimulator, ScenarioModification
from scheduler import TaskScheduler
from workflow_monitor import WorkflowMonitor
from workflow_reliability_manager import WorkflowReliabilityManager
from workflow_state_machine import WorkflowStateMachine
from workflow_transition_executor import WorkflowTransitionExecutor
from runtime_logging import configure_logging
from llm_fallback import generate_content_with_openrouter_fallback, get_openrouter_client
import business_rules
import object_db

try:
    import spacy
except Exception:  # pragma: no cover - optional dependency
    spacy = None

adk = None


from idms_config import (
    PROJECT_ID, LOCATION, CHAT_MODEL, EXTRACT_MODEL, FLASH_CONFIDENCE_THRESHOLD,
    OPENROUTER_SIMPLE_MODEL, OPENROUTER_COMPLEX_MODEL
)


@dataclass
class InteractionState:
    history: list[dict[str, str]] = field(default_factory=list)


class QueryResultCache:
    """Simple in-memory TTL-based cache for query results (1-hour default)."""
    
    def __init__(self, ttl_seconds: int = 3600):
        self.ttl_seconds = ttl_seconds
        self.cache: dict[str, tuple[dict[str, Any], float]] = {}
    
    def _hash_key(self, question: str) -> str:
        """Normalize question and return cache key."""
        normalized = " ".join(str(question or "").strip().lower().split())
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]
    
    def get(self, question: str) -> dict[str, Any] | None:
        """Retrieve cached result if fresh; return None otherwise."""
        key = self._hash_key(question)
        if key not in self.cache:
            return None
        result, timestamp = self.cache[key]
        if time.time() - timestamp > self.ttl_seconds:
            del self.cache[key]
            return None
        return result
    
    def put(self, question: str, result: dict[str, Any]) -> None:
        """Store query result with timestamp."""
        key = self._hash_key(question)
        self.cache[key] = (result, time.time())
    
    def clear(self) -> None:
        """Clear all cached results."""
        self.cache.clear()


class IDMSInteractionTools:
    """Generic interaction tools that can be used directly or attached to ADK."""

    def __init__(self, enable_query_cache: bool = True) -> None:
        configure_logging()
        self.logger = logging.getLogger("idms.interaction")
        try:
            from query_engine import QueryEngine as _QueryEngine
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("query_engine dependencies are not available") from exc

        try:
            from sql_db import get_connection as _get_connection
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("sql_db dependencies are not available") from exc

        self.client = get_openrouter_client()
        if self.client is None:
            raise ValueError("OPENROUTER_API_KEY is required in environment")
        self.query_engine = _QueryEngine()
        self.get_connection = _get_connection
        self.state = InteractionState()
        self._command_nlp = None
        self._command_nlp_initialized = False
        self.model_usage_stats: dict[str, Any] = {
            "total_calls": 0,
            "flash_calls": 0,
            "chat_calls": 0,
            "escalations": 0,
            "by_call_name": {},
        }
        self.solf_interpreter = self._build_solf_policy_interpreter()
        self.query_cache = QueryResultCache() if enable_query_cache else None
        self.event_bus = EventBus(solf_interpreter=self.solf_interpreter, db_connection_fn=self.get_connection)
        self.notifier = create_notifier_from_config(db_connection_fn=self.get_connection)
        self.action_tool = ActionTool(
            db_connection_fn=self.get_connection,
            solf_interpreter=self.solf_interpreter,
            require_confirmation=False,
        )
        self.scheduler = TaskScheduler(
            autonomous_query_fn=self.autonomous_query,
            db_connection_fn=self.get_connection,
            event_bus=self.event_bus,
        )
        self.state_machine = WorkflowStateMachine(db_connection_fn=self.get_connection)
        self.transition_executor = WorkflowTransitionExecutor(
            state_machine=self.state_machine,
            event_bus=self.event_bus,
            solf_interpreter=self.solf_interpreter,
        )
        self.workflow_monitor = WorkflowMonitor(db_connection_fn=self.get_connection)
        self.workflow_reliability = WorkflowReliabilityManager(
            db_connection_fn=self.get_connection,
            monitor=self.workflow_monitor,
            event_bus=self.event_bus,
        )
        self.notifier_engine = NotifierEngine(db_connection_fn=self.get_connection)
        self.dashboard_api = DashboardAPI(
            db_connection_fn=self.get_connection,
            monitor=self.workflow_monitor,
            reliability_manager=self.workflow_reliability,
        )
        self.audit_compliance = AuditComplianceManager(
            db_connection_fn=self.get_connection,
            reliability_manager=self.workflow_reliability,
        )
        self.project_simulator = ProjectSimulator(db_connection_fn=self.get_connection)
        self._ensure_interaction_runtime_tables()
        self._wire_default_event_handlers()

    def _ensure_interaction_runtime_tables(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS workflow_action_log (
            id BIGSERIAL PRIMARY KEY,
            action_name TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            success BOOLEAN NOT NULL,
            message TEXT,
            result_data JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_action_log_name ON workflow_action_log(action_name);
        CREATE INDEX IF NOT EXISTS idx_workflow_action_log_created ON workflow_action_log(created_at);

        CREATE TABLE IF NOT EXISTS interaction_clarification_thread (
            thread_id BIGSERIAL PRIMARY KEY,
            session_id VARCHAR(128),
            channel VARCHAR(32) NOT NULL DEFAULT 'chat',
            user_ref VARCHAR(128),
            original_query TEXT NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'pending_clarification'
                CHECK (status IN ('pending_clarification','resolved','abandoned')),
            reason_code VARCHAR(64) NOT NULL DEFAULT 'ambiguous_value',
            clarification_question TEXT NOT NULL,
            expected_input_type VARCHAR(64) NOT NULL DEFAULT 'free_text',
            ambiguity_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            candidate_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
            provenance_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_clar_thread_status ON interaction_clarification_thread(status);
        CREATE INDEX IF NOT EXISTS idx_clar_thread_created_at ON interaction_clarification_thread(created_at DESC);

        CREATE TABLE IF NOT EXISTS interaction_clarification_turn (
            turn_id BIGSERIAL PRIMARY KEY,
            thread_id BIGINT NOT NULL REFERENCES interaction_clarification_thread(thread_id) ON DELETE CASCADE,
            turn_index INTEGER NOT NULL,
            role VARCHAR(16) NOT NULL CHECK (role IN ('user','assistant','system')),
            message_text TEXT NOT NULL,
            source_type VARCHAR(64),
            answer_source VARCHAR(64),
            confidence NUMERIC(5,4),
            ambiguity_flag BOOLEAN NOT NULL DEFAULT FALSE,
            ambiguity_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            candidate_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
            provenance_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
            linked_doc_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            linked_object_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (thread_id, turn_index)
        );
        CREATE INDEX IF NOT EXISTS idx_clar_turn_thread ON interaction_clarification_turn(thread_id, turn_index);
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(ddl)
        except Exception:
            pass

    @staticmethod
    def _contains_singular_intent(question: str) -> bool:
        text = str(question or "").strip().lower()
        if not text:
            return False
        singular_markers = (
            " current ", " last ", " latest ", " exact ", " full ",
            " aktuell ", " letzte ", " letzter ", " genau ", " vollstaendig ", " vollständig ",
        )
        wrapped = f" {text} "
        return any(marker in wrapped for marker in singular_markers)

    @staticmethod
    def _contains_plural_intent(question: str) -> bool:
        text = str(question or "").strip().lower()
        if not text:
            return False
        plural_markers = (
            " all ", " each ", " every ", " list ", " enumerate ", " options ", " candidates ",
            " alle ", " jeweils ", " liste ", " auflisten ", " mehrere ",
        )
        wrapped = f" {text} "
        return any(marker in wrapped for marker in plural_markers)

    @staticmethod
    def _attribute_expects_plural(attribute_name: str | None) -> bool:
        normalized = str(attribute_name or "").strip().lower()
        return normalized in {
            "shareholders",
            "documents",
            "document_list",
            "related_entities",
            "parties",
            "tags",
        }

    @staticmethod
    def _normalize_candidate_snapshot(candidates: list[Any], limit: int = 8) -> list[Any]:
        out: list[Any] = []
        seen: set[str] = set()
        for item in list(candidates or []):
            normalized: Any
            if isinstance(item, dict):
                normalized = {
                    "name": item.get("name") or item.get("entity_name") or item.get("value"),
                    "value": item.get("attribute_value") if "attribute_value" in item else item.get("value"),
                    "score": item.get("score"),
                    "doc_id": item.get("doc_id"),
                    "doc_name": item.get("doc_name"),
                    "doc_path": item.get("doc_path"),
                    "relationship_name": item.get("relationship_name"),
                }
                normalized = {k: v for k, v in normalized.items() if v not in (None, "")}
            else:
                text = str(item or "").strip()
                if not text:
                    continue
                normalized = text

            dedupe_key = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            out.append(normalized)
            if len(out) >= max(1, int(limit)):
                break
        return out

    def _extract_provenance_snapshot(self, query_result: dict[str, Any] | None, limit: int = 8) -> list[dict[str, Any]]:
        if not isinstance(query_result, dict):
            return []

        grounding = query_result.get("grounding") if isinstance(query_result.get("grounding"), dict) else {}
        data = query_result.get("data") if isinstance(query_result.get("data"), dict) else {}
        attribute_result = grounding.get("attribute_result") if isinstance(grounding.get("attribute_result"), dict) else {}
        direct = grounding.get("direct_query_result") if isinstance(grounding.get("direct_query_result"), dict) else {}
        direct_data = direct.get("data") if isinstance(direct.get("data"), dict) else {}

        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _append_from(payload: dict[str, Any], source_hint: str) -> None:
            if not isinstance(payload, dict):
                return
            entry = {
                "source": source_hint,
                "doc_id": payload.get("doc_id"),
                "doc_name": payload.get("doc_name"),
                "doc_path": payload.get("doc_path"),
                "doc_date": payload.get("doc_date"),
                "entity_name": payload.get("entity_name") or payload.get("source_entity_name"),
                "related_entity_name": payload.get("related_entity_name"),
                "attribute_name": payload.get("attribute_name"),
                "relationship_name": payload.get("relationship_name"),
            }
            entry = {k: v for k, v in entry.items() if v not in (None, "")}
            if len(entry) <= 1:
                return
            key = json.dumps(entry, sort_keys=True, ensure_ascii=False)
            if key in seen:
                return
            seen.add(key)
            out.append(entry)

        _append_from(attribute_result, "grounding.attribute_result")
        _append_from(direct_data, "grounding.direct_query_result")
        _append_from(data, "query_result.data")

        for item in list(grounding.get("matches") or []):
            if not isinstance(item, dict):
                continue
            _append_from(item, "grounding.matches")
            if len(out) >= max(1, int(limit)):
                break

        return out[: max(1, int(limit))]

    @staticmethod
    def _normalize_entity_token(value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = re.sub(r"\([^\)]*\)", " ", text)
        text = re.sub(r"[^a-z0-9äöüß]+", " ", text)
        return " ".join(text.split())

    def _candidate_labels_from_snapshot(self, candidate_snapshot: list[Any]) -> list[str]:
        labels: list[str] = []
        for item in list(candidate_snapshot or []):
            if isinstance(item, dict):
                label = str(item.get("name") or item.get("value") or item.get("entity_name") or "").strip()
            else:
                label = str(item or "").strip()
            if label:
                labels.append(label)
        return labels

    def _match_user_response_to_candidate(self, candidate_snapshot: list[Any], user_response: str) -> str | None:
        response_raw = str(user_response or "").strip()
        if not response_raw:
            return None

        response_norm = self._normalize_entity_token(response_raw)
        if not response_norm:
            return None

        labels = self._candidate_labels_from_snapshot(candidate_snapshot)
        if not labels:
            return None

        for label in labels:
            if self._normalize_entity_token(label) == response_norm:
                return label

        for label in labels:
            label_norm = self._normalize_entity_token(label)
            if not label_norm:
                continue
            if response_norm in label_norm or label_norm in response_norm:
                return label

        return None

    def _entity_matches_anchor(self, candidate: Any, anchor_entity: str) -> bool:
        anchor_norm = self._normalize_entity_token(anchor_entity)
        if not anchor_norm:
            return True

        candidate_norm = self._normalize_entity_token(candidate)
        if not candidate_norm:
            return True

        if candidate_norm == anchor_norm:
            return True

        anchor_parts = anchor_norm.split()
        candidate_parts = candidate_norm.split()
        if len(anchor_parts) >= 2 and len(candidate_parts) >= 2:
            if candidate_parts[: len(anchor_parts)] == anchor_parts:
                return True
            if candidate_parts[-len(anchor_parts):] == anchor_parts:
                return True
        return False

    def _sanitize_grounding_for_anchor(self, grounding: dict[str, Any], anchor_entity: str) -> dict[str, Any]:
        if not isinstance(grounding, dict) or not anchor_entity:
            return grounding if isinstance(grounding, dict) else {}

        filtered = dict(grounding)

        def _filter_item(item: Any) -> Any:
            if not isinstance(item, dict):
                return item

            entity_fields = [
                item.get("entity_name"),
                item.get("source_entity_name"),
                item.get("related_entity_name"),
                item.get("anchor_name"),
                item.get("company_name"),
                item.get("object_name"),
                item.get("target_name"),
                item.get("source_name"),
            ]
            candidate_entities = [value for value in entity_fields if str(value or "").strip()]
            if candidate_entities and not any(self._entity_matches_anchor(value, anchor_entity) for value in candidate_entities):
                return None

            nested_matches = item.get("matches")
            if isinstance(nested_matches, list):
                nested_filtered = [_filter_item(entry) for entry in nested_matches]
                item["matches"] = [entry for entry in nested_filtered if entry is not None]

            nested_results = item.get("results")
            if isinstance(nested_results, list):
                nested_filtered = [_filter_item(entry) for entry in nested_results]
                item["results"] = [entry for entry in nested_filtered if entry is not None]

            return item

        for key in ("attribute_result", "direct_query_result"):
            value = filtered.get(key)
            if isinstance(value, dict):
                sanitized = _filter_item(dict(value))
                if sanitized is None:
                    filtered[key] = None
                else:
                    filtered[key] = sanitized

        for key in ("criteria_result", "discovery_search", "scope"):
            value = filtered.get(key)
            if not isinstance(value, dict):
                continue
            sanitized_block = dict(value)
            for list_key in ("matches", "results", "top_candidates", "nodes", "objects", "relationships", "documents", "candidates", "discovery_results"):
                entries = sanitized_block.get(list_key)
                if not isinstance(entries, list):
                    continue
                new_entries = []
                for entry in entries:
                    sanitized = _filter_item(entry)
                    if sanitized is not None:
                        new_entries.append(sanitized)
                sanitized_block[list_key] = new_entries
            filtered[key] = sanitized_block

        return filtered

    @staticmethod
    def _build_generic_clarification_question(reason_code: str, candidates: list[Any]) -> str:
        count = len(candidates or [])
        if reason_code == "ambiguous_entity":
            return (
                "I found multiple possible entities for your request. "
                "Please specify which exact entity you mean."
            )
        if reason_code == "temporal_conflict":
            return (
                "I found multiple time-valid candidates. "
                "Please specify the timeframe or preferred source document/date."
            )
        if count > 1:
            return (
                "I found multiple possible answers. "
                "Please specify which candidate or scope should be used."
            )
        return "I need one more detail to answer precisely. Please clarify your intended scope."

    def _extract_clarification_signal(self, user_message: str, query_result: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(query_result, dict):
            return None

        answer_source = str(query_result.get("answer_source") or "").strip().lower()
        result_data = query_result.get("data") if isinstance(query_result.get("data"), dict) else {}
        grounding = query_result.get("grounding") if isinstance(query_result.get("grounding"), dict) else {}
        parsed = grounding.get("parsed") if isinstance(grounding.get("parsed"), dict) else {}
        provenance_snapshot = self._extract_provenance_snapshot(query_result)

        candidates: list[Any] = []
        ambiguity_summary: dict[str, Any] = {}
        reason_code = ""

        direct = grounding.get("direct_query_result") if isinstance(grounding.get("direct_query_result"), dict) else {}
        direct_data = direct.get("data") if isinstance(direct.get("data"), dict) else {}
        resolution_data = direct_data if direct_data else result_data
        direct_resolution_status = str(resolution_data.get("resolution_status") or "").strip().lower()
        if direct_resolution_status.startswith("ambiguous"):
            reason_code = direct_resolution_status if direct_resolution_status in {"ambiguous_entity", "ambiguous_value"} else "ambiguous_value"
            candidates = list(
                resolution_data.get("entity_candidates")
                or resolution_data.get("candidate_values")
                or resolution_data.get("candidates")
                or []
            )
            ambiguity_summary = {
                "resolution_status": direct_resolution_status,
                "entity_hint": resolution_data.get("entity_hint"),
                "top_score": resolution_data.get("top_score"),
                "second_score": resolution_data.get("second_score"),
                "score_gap": resolution_data.get("score_gap"),
                "attribute_name": resolution_data.get("attribute_name"),
                "entity_name": resolution_data.get("entity_name"),
            }

        if not reason_code and "ambiguous" in answer_source:
            reason_code = "ambiguous_value"

        attribute_result = grounding.get("attribute_result") if isinstance(grounding.get("attribute_result"), dict) else {}
        attr_value = attribute_result.get("attribute_value")
        attr_name = attribute_result.get("attribute_name") or parsed.get("attribute_name")
        if (
            not reason_code
            and isinstance(attr_value, list)
            and len(attr_value) > 1
            and self._contains_singular_intent(user_message)
            and not self._contains_plural_intent(user_message)
            and not self._attribute_expects_plural(attr_name)
        ):
            reason_code = "ambiguous_value"
            candidates = list(attr_value)
            ambiguity_summary = {
                "resolution_status": "ambiguous_value",
                "attribute_name": attr_name,
                "entity_name": attribute_result.get("entity_name") or parsed.get("entity_name"),
                "candidate_count": len(attr_value),
            }

        if not reason_code:
            return None

        if reason_code == "ambiguous_value" and self._contains_plural_intent(user_message):
            return None

        return {
            "reason_code": reason_code,
            "expected_input_type": "entity_scope" if reason_code == "ambiguous_entity" else "free_text",
            "candidates": self._normalize_candidate_snapshot(candidates, limit=8),
            "provenance_snapshot": provenance_snapshot,
            "ambiguity_summary": ambiguity_summary,
            "answer_source": answer_source or "unknown",
            "confidence": parsed.get("confidence"),
        }

    def _create_clarification_thread(
        self,
        *,
        original_query: str,
        reason_code: str,
        clarification_question: str,
        expected_input_type: str,
        ambiguity_summary: dict[str, Any] | None,
        candidate_snapshot: list[Any] | None,
        provenance_snapshot: list[dict[str, Any]] | None,
        metadata: dict[str, Any] | None,
    ) -> int:
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO interaction_clarification_thread (
                        original_query,
                        reason_code,
                        clarification_question,
                        expected_input_type,
                        ambiguity_summary,
                        candidate_snapshot,
                        provenance_snapshot,
                        metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING thread_id
                    """,
                    (
                        str(original_query or "").strip(),
                        str(reason_code or "ambiguous_value").strip(),
                        str(clarification_question or "").strip(),
                        str(expected_input_type or "free_text").strip(),
                        Json(ambiguity_summary or {}),
                        Json(candidate_snapshot or []),
                        Json(provenance_snapshot or []),
                        Json(metadata or {}),
                    ),
                )
                row = cur.fetchone()
                return int(row[0])

    def _append_clarification_turn(
        self,
        *,
        thread_id: int,
        role: str,
        message_text: str,
        source_type: str | None = None,
        answer_source: str | None = None,
        confidence: float | None = None,
        ambiguity_flag: bool = False,
        ambiguity_payload: dict[str, Any] | None = None,
        candidate_snapshot: list[Any] | None = None,
        provenance_snapshot: list[dict[str, Any]] | None = None,
        linked_doc_ids: list[int] | None = None,
        linked_object_ids: list[int] | None = None,
    ) -> None:
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(turn_index), 0) + 1 FROM interaction_clarification_turn WHERE thread_id = %s",
                    (int(thread_id),),
                )
                next_idx = int((cur.fetchone() or [1])[0] or 1)
                cur.execute(
                    """
                    INSERT INTO interaction_clarification_turn (
                        thread_id,
                        turn_index,
                        role,
                        message_text,
                        source_type,
                        answer_source,
                        confidence,
                        ambiguity_flag,
                        ambiguity_payload,
                        candidate_snapshot,
                        provenance_snapshot,
                        linked_doc_ids,
                        linked_object_ids
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        int(thread_id),
                        next_idx,
                        str(role or "assistant").strip().lower(),
                        str(message_text or "").strip(),
                        str(source_type or "").strip() or None,
                        str(answer_source or "").strip() or None,
                        float(confidence) if confidence is not None else None,
                        bool(ambiguity_flag),
                        Json(ambiguity_payload or {}),
                        Json(candidate_snapshot or []),
                        Json(provenance_snapshot or []),
                        Json([int(item) for item in (linked_doc_ids or []) if int(item) > 0]),
                        Json([int(item) for item in (linked_object_ids or []) if int(item) > 0]),
                    ),
                )
                cur.execute(
                    "UPDATE interaction_clarification_thread SET updated_at = NOW() WHERE thread_id = %s",
                    (int(thread_id),),
                )

    def _get_clarification_thread_row(self, thread_id: int) -> dict[str, Any] | None:
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT thread_id, status, original_query, reason_code, clarification_question,
                           expected_input_type, ambiguity_summary, candidate_snapshot, provenance_snapshot,
                           metadata, created_at, updated_at, resolved_at
                    FROM interaction_clarification_thread
                    WHERE thread_id = %s
                    """,
                    (int(thread_id),),
                )
                row = cur.fetchone()
                if not row:
                    return None

                cur.execute(
                    """
                    SELECT turn_id, turn_index, role, message_text, source_type, answer_source,
                           confidence, ambiguity_flag, ambiguity_payload, candidate_snapshot,
                           provenance_snapshot, linked_doc_ids, linked_object_ids, created_at
                    FROM interaction_clarification_turn
                    WHERE thread_id = %s
                    ORDER BY turn_index ASC
                    """,
                    (int(thread_id),),
                )
                turns = cur.fetchall() or []

        return {
            "thread_id": int(row[0]),
            "status": row[1],
            "original_query": row[2],
            "reason_code": row[3],
            "clarification_question": row[4],
            "expected_input_type": row[5],
            "ambiguity_summary": row[6] if isinstance(row[6], dict) else {},
            "candidate_snapshot": row[7] if isinstance(row[7], list) else [],
            "provenance_snapshot": row[8] if isinstance(row[8], list) else [],
            "metadata": row[9] if isinstance(row[9], dict) else {},
            "created_at": row[10].isoformat() if row[10] else None,
            "updated_at": row[11].isoformat() if row[11] else None,
            "resolved_at": row[12].isoformat() if row[12] else None,
            "turns": [
                {
                    "turn_id": int(item[0]),
                    "turn_index": int(item[1]),
                    "role": item[2],
                    "message_text": item[3],
                    "source_type": item[4],
                    "answer_source": item[5],
                    "confidence": float(item[6]) if item[6] is not None else None,
                    "ambiguity_flag": bool(item[7]),
                    "ambiguity_payload": item[8] if isinstance(item[8], dict) else {},
                    "candidate_snapshot": item[9] if isinstance(item[9], list) else [],
                    "provenance_snapshot": item[10] if isinstance(item[10], list) else [],
                    "linked_doc_ids": item[11] if isinstance(item[11], list) else [],
                    "linked_object_ids": item[12] if isinstance(item[12], list) else [],
                    "created_at": item[13].isoformat() if item[13] else None,
                }
                for item in turns
            ],
        }

    def _set_clarification_thread_status(self, thread_id: int, status: str) -> None:
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE interaction_clarification_thread
                    SET status = %s,
                        updated_at = NOW(),
                        resolved_at = CASE WHEN %s = 'resolved' THEN NOW() ELSE resolved_at END
                    WHERE thread_id = %s
                    """,
                    (str(status or "pending_clarification"), str(status or "pending_clarification"), int(thread_id)),
                )

    def _update_clarification_prompt(
        self,
        thread_id: int,
        *,
        reason_code: str,
        clarification_question: str,
        expected_input_type: str,
        ambiguity_summary: dict[str, Any] | None,
        candidate_snapshot: list[Any] | None,
        provenance_snapshot: list[dict[str, Any]] | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE interaction_clarification_thread
                    SET status = 'pending_clarification',
                        reason_code = %s,
                        clarification_question = %s,
                        expected_input_type = %s,
                        ambiguity_summary = %s,
                        candidate_snapshot = %s,
                        provenance_snapshot = %s,
                        metadata = COALESCE(metadata, '{}'::jsonb) || %s,
                        updated_at = NOW()
                    WHERE thread_id = %s
                    """,
                    (
                        str(reason_code or "ambiguous_value"),
                        str(clarification_question or "").strip(),
                        str(expected_input_type or "free_text").strip(),
                        Json(ambiguity_summary or {}),
                        Json(candidate_snapshot or []),
                        Json(provenance_snapshot or []),
                        Json(metadata or {}),
                        int(thread_id),
                    ),
                )

    def get_clarification_thread(self, thread_id: int) -> dict[str, Any] | None:
        return self._get_clarification_thread_row(int(thread_id))

    def respond_to_clarification(self, thread_id: int, user_response: str) -> dict[str, Any]:
        response_text = str(user_response or "").strip()
        if not response_text:
            raise ValueError("user_response must not be empty")

        thread = self._get_clarification_thread_row(int(thread_id))
        if not thread:
            raise ValueError(f"Clarification thread {thread_id} not found")
        if str(thread.get("status") or "") != "pending_clarification":
            raise ValueError(f"Clarification thread {thread_id} is not pending")

        self._append_clarification_turn(
            thread_id=int(thread_id),
            role="user",
            message_text=response_text,
            source_type="clarification_response",
        )

        original_query = str(thread.get("original_query") or "").strip()
        candidate_snapshot = list(thread.get("candidate_snapshot") or [])
        selected_candidate = self._match_user_response_to_candidate(candidate_snapshot, response_text)
        if selected_candidate:
            refined_question = (
                f"{original_query}\n"
                f"Clarification from user: selected candidate is '{selected_candidate}'. "
                f"Use exactly this entity and ignore other ambiguous candidates."
            ).strip()
        else:
            refined_question = f"{original_query}\nClarification from user: {response_text}".strip()
        query_result = self.autonomous_query(refined_question, max_steps=3, record_history=False)

        signal = self._extract_clarification_signal(refined_question, query_result)
        if signal and selected_candidate and str(signal.get("reason_code") or "") == "ambiguous_entity":
            forced_question = (
                f"{original_query}\n"
                f"Entity disambiguation decision: '{selected_candidate}'. "
                f"Restrict answer to this exact entity only."
            ).strip()
            forced_result = self.autonomous_query(forced_question, max_steps=3, record_history=False)
            forced_signal = self._extract_clarification_signal(forced_question, forced_result)
            if not forced_signal:
                query_result = forced_result
                signal = None

        if signal:
            question = self._build_generic_clarification_question(
                str(signal.get("reason_code") or "ambiguous_value"),
                list(signal.get("candidates") or []),
            )
            if selected_candidate and str(signal.get("reason_code") or "") == "ambiguous_entity":
                question = (
                    f"I received your selection '{selected_candidate}', but I still found ambiguity. "
                    "Please choose one exact candidate from the latest list and, if available, include a document/file hint."
                )
            self._update_clarification_prompt(
                int(thread_id),
                reason_code=str(signal.get("reason_code") or "ambiguous_value"),
                clarification_question=question,
                expected_input_type=str(signal.get("expected_input_type") or "free_text"),
                ambiguity_summary=signal.get("ambiguity_summary") if isinstance(signal.get("ambiguity_summary"), dict) else {},
                candidate_snapshot=list(signal.get("candidates") or []),
                provenance_snapshot=list(signal.get("provenance_snapshot") or []),
                metadata={
                    "last_query_answer_source": str(query_result.get("answer_source") or ""),
                    "last_question": refined_question,
                    "selected_candidate": selected_candidate,
                },
            )
            self._append_clarification_turn(
                thread_id=int(thread_id),
                role="assistant",
                message_text=question,
                source_type="clarification_prompt",
                answer_source="pending_clarification",
                confidence=float(signal.get("confidence") or 0.0) if signal.get("confidence") is not None else None,
                ambiguity_flag=True,
                ambiguity_payload=signal.get("ambiguity_summary") if isinstance(signal.get("ambiguity_summary"), dict) else {},
                candidate_snapshot=list(signal.get("candidates") or []),
                provenance_snapshot=list(signal.get("provenance_snapshot") or []),
            )
            return {
                "status": "pending_clarification",
                "thread_id": int(thread_id),
                "clarification_question": question,
                "reason_code": str(signal.get("reason_code") or "ambiguous_value"),
                "expected_input_type": str(signal.get("expected_input_type") or "free_text"),
                "candidates": list(signal.get("candidates") or []),
                "provenance_snapshot": list(signal.get("provenance_snapshot") or []),
                "grounding": query_result,
            }

        final_answer = str(query_result.get("answer") or "").strip()
        self._set_clarification_thread_status(int(thread_id), "resolved")
        self._append_clarification_turn(
            thread_id=int(thread_id),
            role="assistant",
            message_text=final_answer,
            source_type="clarification_resolution",
            answer_source=str(query_result.get("answer_source") or "resolved"),
            confidence=None,
            ambiguity_flag=False,
            ambiguity_payload={},
            candidate_snapshot=[],
        )

        return {
            "status": "resolved",
            "thread_id": int(thread_id),
            "answer": final_answer,
            "answer_source": str(query_result.get("answer_source") or "resolved"),
            "grounding": query_result,
        }

    def suggest_similar_entities(
        self,
        entity_name: str,
        class_name: str = "person",
        limit: int = 8,
    ) -> dict[str, Any]:
        name = str(entity_name or "").strip()
        klass = str(class_name or "person").strip().lower() or "person"
        if not name:
            raise ValueError("entity_name must not be empty")

        with self.get_connection() as conn:
            suggestions = object_db.suggest_similar_objects(
                connection=conn,
                object_name=name,
                class_name=klass,
                limit=max(1, int(limit)),
            )

        message = (
            "No similar active entities were found."
            if not suggestions
            else "Similar names were found. Consider alias linking first, then merge only after user confirmation."
        )
        return {
            "entity_name": name,
            "class_name": klass,
            "candidates": suggestions,
            "candidate_count": len(suggestions),
            "message": message,
            "recommended_actions": ["confirm_same_person", "link_alias", "merge_records"],
        }

    def link_entity_alias(
        self,
        primary_object_id: int,
        alias_object_id: int,
        alias_type: str = "same_entity",
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.get_connection() as conn:
            row = object_db.link_object_alias(
                connection=conn,
                primary_object_id=int(primary_object_id),
                alias_object_id=int(alias_object_id),
                alias_type=str(alias_type or "same_entity"),
                confidence=confidence,
                metadata=metadata if isinstance(metadata, dict) else {},
                commit=True,
            )
        return {
            "status": "linked",
            "alias": row,
            "message": "Alias link saved. Query resolution will consider the linked entity group.",
        }

    def merge_entity_instances(
        self,
        canonical_object_id: int,
        duplicate_object_id: int,
        merge_note: str = "",
        keep_alias_link: bool = True,
    ) -> dict[str, Any]:
        with self.get_connection() as conn:
            summary = object_db.merge_object_instances(
                connection=conn,
                canonical_object_id=int(canonical_object_id),
                duplicate_object_id=int(duplicate_object_id),
                merge_note=str(merge_note or "").strip(),
                keep_alias_link=bool(keep_alias_link),
            )
        return {
            "status": "merged",
            "summary": summary,
            "message": "Records merged. Relationships, attributes, and parts were retargeted to the canonical object.",
        }

    def _log_action_execution(self, action_name: str, payload: dict[str, Any], result: dict[str, Any]) -> None:
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO workflow_action_log (action_name, payload, success, message, result_data)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            str(action_name or "").strip().lower(),
                            Json(payload or {}),
                            bool(result.get("success")),
                            str(result.get("message") or ""),
                            Json(result or {}),
                        ),
                    )
        except Exception:
            pass

    def _wire_default_event_handlers(self) -> None:
        self.event_bus.subscribe("task.overdue", lambda e: handle_task_overdue(e, self.notifier))
        self.event_bus.subscribe("sla.breach", lambda e: handle_sla_breach(e, self.notifier))
        self.event_bus.subscribe("resource.conflict_detected", lambda e: handle_resource_conflict(e, self.notifier))
        self.transition_executor.register_default_rules()

    def _build_solf_policy_interpreter(self) -> Any:
        try:
            import solf_parser
            from solf_interpreter import SOLFInterpreter
        except Exception:
            return None

        script_path = Path(__file__).with_name("solf_script.txt")
        if not script_path.exists():
            return None

        try:
            solf_script = script_path.read_text(encoding="utf-8")
            interpreter = SOLFInterpreter()
            parser_adapter = type("Parser", (object,), {"parse": staticmethod(solf_parser.parse_script)})()
            interpreter.set_parser(parser_adapter)
            interpreter.set_debug(False)
            # Suppress verbose SOLF load logs during normal runtime.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                runtime_pre_scripts, runtime_post_scripts = business_rules.load_active_business_rule_solf_script_chunks(limit=300)
                first_script = "\n\n".join(chunk for chunk in [*runtime_pre_scripts, str(solf_script or "").strip()] if chunk)
                if first_script:
                    interpreter.load_program_script(first_script, clear_existing=True)
                runtime_post_script = "\n\n".join(chunk for chunk in runtime_post_scripts if chunk)
                if runtime_post_script:
                    interpreter.load_program_script(runtime_post_script, clear_existing=False)
            self.logger.info("Loaded SOLF policy script from %s", script_path)
            return interpreter
        except Exception:
            self.logger.exception("Failed to load SOLF policy interpreter")
            return None

    def _default_policy(self, reason: str = "default_allow") -> dict[str, Any]:
        return {
            "mode": "allow",
            "reason": reason,
            "policy_clause": None,
        }

    def _invoke_solf_policy(self, clause_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.solf_interpreter is None:
            return self._default_policy("solf_unavailable")

        try:
            self.logger.info("Invoking SOLF policy clause=%s", clause_name)
            result = self.solf_interpreter._invoke_clause(clause_name, [payload])
        except Exception:
            self.logger.exception("SOLF policy clause invocation failed: %s", clause_name)
            return self._default_policy("solf_invocation_failed")

        if not isinstance(result, dict):
            return self._default_policy("missing_or_non_dict_policy")

        mode = str(result.get("mode") or "allow").strip().lower()
        if mode not in {"allow", "deny", "escalate"}:
            mode = "allow"

        normalized = dict(result)
        normalized["mode"] = mode
        normalized["policy_clause"] = clause_name
        return normalized

    def _harden_transition_policy(self, payload: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        entity_type = str(payload.get("entity_type") or "").strip().lower()
        to_state = str(payload.get("to_state") or "").strip().lower()
        event = str(payload.get("event") or "").strip().lower()
        actor_ref = str(payload.get("actor_ref") or "").strip().lower()

        # Enforce terminal workflow-case transitions only via approval events.
        if entity_type == "workflow_case" and to_state in {"approved", "rejected"} and event != "approval_recorded":
            return {
                "mode": "deny",
                "reason": "workflow_case_terminal_state_requires_approval_event",
                "allowed": False,
                "message": "workflow_case can move to approved/rejected only from approval_recorded event",
                "policy_clause": "workflow_transition_policy",
            }

        # Require explicit completion event for project tasks to become done.
        if entity_type == "project_task" and to_state == "done" and event != "task_completed":
            return {
                "mode": "deny",
                "reason": "project_task_done_requires_completion_event",
                "allowed": False,
                "message": "project_task can move to done only with task_completed event",
                "policy_clause": "workflow_transition_policy",
            }

        # Mark compliance escalations as escalate mode for downstream handling.
        if entity_type == "compliance_action" and to_state == "escalated" and event == "compliance_deadline_breach":
            hardened = dict(policy)
            hardened["mode"] = "escalate"
            hardened["reason"] = str(hardened.get("reason") or "compliance_deadline_breach_escalation")
            hardened["allowed"] = True
            return hardened

        # Prevent system/manual actor from forcing terminal workflow-case states.
        if entity_type == "workflow_case" and to_state in {"approved", "rejected"} and actor_ref == "system":
            return {
                "mode": "deny",
                "reason": "manual_terminal_transition_not_allowed",
                "allowed": False,
                "message": "manual transition to terminal workflow_case state is not allowed",
                "policy_clause": "workflow_transition_policy",
            }

        return policy

    def _invoke_solf_clause_raw(self, clause_name: str, call_args: list[Any]) -> Any:
        if self.solf_interpreter is None:
            return None
        try:
            return self.solf_interpreter._invoke_clause(clause_name, call_args)
        except Exception:
            return None

    def evaluate_solf_clause(
        self,
        clause_name: str,
        payload: dict[str, Any] | None = None,
        args: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a specific SOLF clause/predicate and return raw evaluation result.

        This is an explicit interaction endpoint for users who want direct predicate
        execution, distinct from policy and workflow internal calls.
        """
        raw_clause_name = str(clause_name or "").strip()
        if not raw_clause_name:
            return {
                "success": False,
                "message": "clause_name is required",
                "clause_name": "",
                "args": [],
                "result": None,
            }

        try:
            safe_clause = self._sanitize_identifier(raw_clause_name)
        except Exception:
            return {
                "success": False,
                "message": f"Unsafe SOLF clause name: {raw_clause_name}",
                "clause_name": raw_clause_name,
                "args": [],
                "result": None,
            }

        if self.solf_interpreter is None:
            return {
                "success": False,
                "message": "SOLF interpreter unavailable",
                "clause_name": safe_clause,
                "args": [],
                "result": None,
            }

        call_args: list[Any]
        if isinstance(args, list) and args:
            call_args = list(args)
        else:
            call_args = [payload if isinstance(payload, dict) else {}]

        try:
            result = self.solf_interpreter._invoke_clause(safe_clause, call_args)
        except Exception as exc:
            self.logger.exception("SOLF clause execution failed: %s", safe_clause)
            return {
                "success": False,
                "message": f"SOLF clause execution failed: {exc}",
                "clause_name": safe_clause,
                "args": call_args,
                "result": None,
            }

        return {
            "success": True,
            "message": "SOLF clause executed",
            "clause_name": safe_clause,
            "args": call_args,
            "result": result,
        }

    def _as_string_key_dict(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {str(k): v for k, v in value.items()}

    def _resolve_solf_placeholders(self, value: Any, payload: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {str(k): self._resolve_solf_placeholders(v, payload) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_solf_placeholders(v, payload) for v in value]
        if isinstance(value, str) and value.startswith("_") and len(value) > 1:
            key = value[1:]
            if key in payload:
                return payload.get(key)
        return value

    def _sanitize_identifier(self, value: str) -> str:
        identifier = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
            raise ValueError(f"Unsafe SQL identifier: {identifier}")
        return identifier

    def _normalize_solf_identifier(self, value: str) -> str:
        """Normalize dynamic SOLF key forms like ['field'] into field."""
        text = str(value or "").strip()
        if text.startswith("['") and text.endswith("']") and len(text) > 4:
            return text[2:-2]
        if text.startswith('["') and text.endswith('"]') and len(text) > 4:
            return text[2:-2]
        if text.startswith("[") and text.endswith("]") and len(text) > 2:
            inner = text[1:-1].strip().strip("'\"")
            return inner
        return text

    def _execute_sql_plan(self, sql_plan: dict[str, Any]) -> dict[str, Any]:
        plan = self._as_string_key_dict(sql_plan)
        operation = str(plan.get("operation") or "").strip().lower()
        table = self._sanitize_identifier(str(plan.get("table") or "").strip())

        allowed_tables = {
            "projects",
            "project_tasks",
            "project_milestones",
            "project_members",
            "crm_tasks",
            "invoices",
            "workflow_cases",
            "approvals",
            "compliance_actions",
            "person",
            "organization",
            "company",
            "document",
            "invoice",
            "payment",
            "project",
            "project_task",
            "project_milestone",
            "project_risk",
            "workflow_case",
            "approval",
            "compliance_action",
            "employee_profile",
            "employment_contract",
            "party",
            "customer",
            "supplier",
            "product",
            "service",
            "warehouse",
            "inventory_item",
            "vat_return",
            "tax_declaration",
            "social_contribution_report",
        }
        if table not in allowed_tables:
            return {"success": False, "message": f"Table not allowed: {table}"}

        where = {
            self._normalize_solf_identifier(str(k)): v
            for k, v in self._as_string_key_dict(plan.get("where")).items()
        }
        values = {
            self._normalize_solf_identifier(str(k)): v
            for k, v in self._as_string_key_dict(plan.get("values")).items()
        }
        returning = plan.get("returning") if isinstance(plan.get("returning"), list) else []
        returning_cols = [self._sanitize_identifier(str(c)) for c in returning]

        if operation == "insert":
            if not values:
                return {"success": False, "message": "Insert plan missing values."}
            cols = [self._sanitize_identifier(k) for k in values.keys()]
            params = list(values.values())
            placeholders = ", ".join(["%s"] * len(cols))
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
            if returning_cols:
                sql += f" RETURNING {', '.join(returning_cols)}"
            try:
                with self.get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, params)
                        row = cur.fetchone() if returning_cols else None
            except Exception as exc:
                return {
                    "success": False,
                    "operation": operation,
                    "table": table,
                    "message": f"SQL insert failed: {exc}",
                }
            return {
                "success": True,
                "operation": operation,
                "table": table,
                "returned": dict(zip(returning_cols, row)) if row and returning_cols else None,
            }

        if operation == "update":
            if not values:
                return {"success": False, "message": "Update plan missing values."}
            if not where:
                return {"success": False, "message": "Unsafe update without where clause."}

            set_cols = [self._sanitize_identifier(k) for k in values.keys()]
            where_cols = [self._sanitize_identifier(k) for k in where.keys()]
            set_expr = ", ".join([f"{c}=%s" for c in set_cols])
            where_expr = " AND ".join([f"{c}=%s" for c in where_cols])
            sql = f"UPDATE {table} SET {set_expr}, updated_at=NOW() WHERE {where_expr}"
            if returning_cols:
                sql += f" RETURNING {', '.join(returning_cols)}"
            params = list(values.values()) + list(where.values())
            try:
                with self.get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, params)
                        row = cur.fetchone() if returning_cols else None
                        affected = cur.rowcount
            except Exception as exc:
                return {
                    "success": False,
                    "operation": operation,
                    "table": table,
                    "message": f"SQL update failed: {exc}",
                }
            return {
                "success": affected > 0,
                "operation": operation,
                "table": table,
                "affected_rows": affected,
                "returned": dict(zip(returning_cols, row)) if row and returning_cols else None,
            }

        if operation == "select":
            where_cols = [self._sanitize_identifier(k) for k in where.keys()]
            where_expr = " AND ".join([f"{c}=%s" for c in where_cols]) if where_cols else "TRUE"
            sql = f"SELECT * FROM {table} WHERE {where_expr}"
            try:
                with self.get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, list(where.values()))
                        rows = cur.fetchall() or []
                        columns = [desc[0] for desc in cur.description or []]
            except Exception as exc:
                return {
                    "success": False,
                    "operation": operation,
                    "table": table,
                    "message": f"SQL select failed: {exc}",
                }
            return {
                "success": True,
                "operation": operation,
                "table": table,
                "row_count": len(rows),
                "rows": [dict(zip(columns, row)) for row in rows],
            }

        return {"success": False, "message": f"Unsupported operation in sql plan: {operation}"}

    def _compose_action_plan(self, action_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        action = str(action_name or "").strip().lower()
        raw = self._invoke_solf_clause_raw("action_plan", [action, payload])
        plan = self._as_string_key_dict(raw)
        return plan or None

    def _compose_scheduler_command(self, command_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        command = str(command_name or "").strip().lower()
        raw = self._invoke_solf_clause_raw("scheduler_command", [command, payload])
        plan = self._as_string_key_dict(raw)
        return plan or None

    def _extract_json_array(self, raw_text: str) -> list[Any]:
        text = str(raw_text or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return []

    def _extract_solf_clause_request(self, text: str) -> dict[str, Any] | None:
        raw = str(text or "").strip()
        if not raw:
            return None

        lowered = raw.lower()
        has_clause_token = bool(re.search(r"\b(predicate|clause|solf)\b", lowered))
        has_action_verb = bool(re.search(r"\b(run|invoke|execute|call|evaluate|trigger)\b", lowered))
        if not has_clause_token or not (has_action_verb or lowered.startswith("solf ")):
            return None

        clause_name = ""
        name_patterns = [
            r"\b(?:predicate|clause)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"\bsolf\s+(?:predicate|clause)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"\b(?:run|invoke|execute|call|evaluate|trigger)\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:predicate|clause)\b",
            r"\bsolf\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        ]
        for pattern in name_patterns:
            match = re.search(pattern, raw, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = str(match.group(1) or "").strip()
            if candidate.lower() not in {"predicate", "clause", "solf"}:
                clause_name = candidate
                break
        if not clause_name:
            return None

        args: list[Any] = []
        args_match = re.search(r"\bargs?\s*[:=]?\s*(\[[\s\S]*\])", raw, flags=re.IGNORECASE)
        if args_match:
            args = self._extract_json_array(args_match.group(1))

        payload = self._extract_json_object(raw)
        if not isinstance(payload, dict):
            payload = {}

        return {
            "clause_name": clause_name,
            "payload": payload,
            "args": args,
            "original_text": raw,
        }

    def _format_direct_solf_query_result(self, question: str, evaluated: dict[str, Any]) -> dict[str, Any]:
        success = bool(evaluated.get("success"))
        clause_name = str(evaluated.get("clause_name") or "").strip()
        message = str(evaluated.get("message") or "").strip()
        if success:
            result_text = json.dumps(evaluated.get("result"), ensure_ascii=False)
            answer = f"[source: solf_clause] SOLF clause {clause_name} executed. Result: {result_text}".strip()
        else:
            answer = f"[source: solf_clause] SOLF clause {clause_name or 'unknown'} failed: {message or 'execution failed'}".strip()
        return {
            "intent": "solf_clause",
            "source": "solf_clause",
            "question": question,
            "answer": answer,
            "success": success,
            "data": evaluated,
            "clause_name": clause_name,
        }

    def query(self, question: str) -> dict[str, Any]:
        """Tool: Resolve user query via SQL-first + Qdrant fallback query engine.
        
        Results are cached for 1 hour to avoid redundant embedding/search calls.
        """
        if self.query_cache:
            cached = self.query_cache.get(question)
            if cached is not None:
                # Mark cache hit in result metadata.
                cached = dict(cached)
                cached["_cache_hit"] = True
                return cached

        clause_request = self._extract_solf_clause_request(question)
        if isinstance(clause_request, dict):
            evaluated = self.evaluate_solf_clause(
                clause_name=str(clause_request.get("clause_name") or ""),
                payload=clause_request.get("payload") if isinstance(clause_request.get("payload"), dict) else {},
                args=clause_request.get("args") if isinstance(clause_request.get("args"), list) else [],
            )
            result = self._format_direct_solf_query_result(question, evaluated)
            if self.query_cache:
                self.query_cache.put(question, result)
            return result
        
        result = self.query_engine.answer(question)
        if isinstance(result, dict):
            result = dict(result)
            result["evidence_bundle"] = self._build_query_evidence_bundle(question, result)
        
        if self.query_cache:
            self.query_cache.put(question, result)
        
        return result

    def ingest_document(
        self,
        source: str,
        description: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Tool: Ingest a local or gs:// source through the existing ingest pipeline."""
        try:
            import ingest
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("ingest pipeline dependencies are not available") from exc

        user_context = {
            "description": description,
            "tags": tags or [],
            "metadata": metadata or {},
        }
        return ingest.run_ingest(source_path_or_uri=source, user_context=user_context)

    def ingest_information(
        self,
        information_text: str,
        description: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Tool: Ingest raw information text without providing an external document file."""
        if not information_text.strip():
            raise ValueError("information_text must not be empty")

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", delete=False) as tmp:
            tmp.write(information_text)
            tmp_path = tmp.name

        try:
            return self.ingest_document(
                source=tmp_path,
                description=description,
                tags=tags,
                metadata=metadata,
            )
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    def generate_document(
        self,
        document_type: str,
        subject: str,
        context: dict[str, Any] | None = None,
        template: str = "",
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """Tool: Generate a simple structured document from payload context."""
        doc_type = str(document_type or "").strip().lower()
        doc_subject = str(subject or "").strip()
        if not doc_type:
            raise ValueError("document_type must not be empty")
        if not doc_subject:
            raise ValueError("subject must not be empty")

        ctx = context if isinstance(context, dict) else {}
        title = f"{doc_type.replace('_', ' ').title()}: {doc_subject}"
        lines = [
            f"# {title}",
            "",
            f"Generated At: {datetime.now(timezone.utc).isoformat()}",
            "",
        ]

        if template:
            lines.extend(["Template", "--------", template.strip(), ""])

        if ctx:
            lines.extend(["Context", "-------"])
            for key, value in ctx.items():
                if isinstance(value, (dict, list)):
                    rendered = json.dumps(value, ensure_ascii=False)
                else:
                    rendered = str(value)
                lines.append(f"- {key}: {rendered}")
            lines.append("")

        content = "\n".join(lines).strip() + "\n"
        out_path = None
        if output_path:
            out_file = Path(output_path)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(content, encoding="utf-8")
            out_path = str(out_file)

        return {
            "success": True,
            "document_type": doc_type,
            "subject": doc_subject,
            "content": content,
            "output_path": out_path,
        }

    def chat(self, user_message: str, use_query_tool: bool = True) -> dict[str, Any]:
        """Tool: Chat interaction with optional grounding through the query tool."""
        user_message = str(user_message or "").strip()
        if not user_message:
            raise ValueError("user_message must not be empty")

        clause_request = self._extract_solf_clause_request(user_message)
        if isinstance(clause_request, dict):
            evaluated = self.evaluate_solf_clause(
                clause_name=str(clause_request.get("clause_name") or ""),
                payload=clause_request.get("payload") if isinstance(clause_request.get("payload"), dict) else {},
                args=clause_request.get("args") if isinstance(clause_request.get("args"), list) else [],
            )
            query_like = self._format_direct_solf_query_result(user_message, evaluated)
            assistant_text = str(query_like.get("answer") or "").strip()
            self.state.history.append({"role": "user", "content": user_message})
            self.state.history.append({"role": "assistant", "content": assistant_text})
            return {
                "answer": assistant_text,
                "answer_source": "solf_clause",
                "answer_sources": ["solf_clause"],
                "grounding": query_like,
                "model": "deterministic_solf",
            }

        query_result: dict[str, Any] | None = None
        if use_query_tool:
            query_result = self.autonomous_query(user_message, max_steps=3, record_history=False)

        clarification_signal = self._extract_clarification_signal(user_message, query_result)
        if clarification_signal:
            clarification_question = self._build_generic_clarification_question(
                str(clarification_signal.get("reason_code") or "ambiguous_value"),
                list(clarification_signal.get("candidates") or []),
            )
            thread_id = self._create_clarification_thread(
                original_query=user_message,
                reason_code=str(clarification_signal.get("reason_code") or "ambiguous_value"),
                clarification_question=clarification_question,
                expected_input_type=str(clarification_signal.get("expected_input_type") or "free_text"),
                ambiguity_summary=(
                    clarification_signal.get("ambiguity_summary")
                    if isinstance(clarification_signal.get("ambiguity_summary"), dict)
                    else {}
                ),
                candidate_snapshot=list(clarification_signal.get("candidates") or []),
                provenance_snapshot=list(clarification_signal.get("provenance_snapshot") or []),
                metadata={
                    "answer_source": str((query_result or {}).get("answer_source") or ""),
                    "trigger": "generic_ambiguity_guardrail",
                },
            )

            self._append_clarification_turn(
                thread_id=thread_id,
                role="user",
                message_text=user_message,
                source_type="chat_input",
            )
            self._append_clarification_turn(
                thread_id=thread_id,
                role="assistant",
                message_text=clarification_question,
                source_type="clarification_prompt",
                answer_source="pending_clarification",
                confidence=float(clarification_signal.get("confidence") or 0.0)
                if clarification_signal.get("confidence") is not None
                else None,
                ambiguity_flag=True,
                ambiguity_payload=(
                    clarification_signal.get("ambiguity_summary")
                    if isinstance(clarification_signal.get("ambiguity_summary"), dict)
                    else {}
                ),
                candidate_snapshot=list(clarification_signal.get("candidates") or []),
                provenance_snapshot=list(clarification_signal.get("provenance_snapshot") or []),
            )

            self.state.history.append({"role": "user", "content": user_message})
            self.state.history.append({"role": "assistant", "content": clarification_question})

            return {
                "answer": clarification_question,
                "status": "pending_clarification",
                "thread_id": thread_id,
                "clarification_question": clarification_question,
                "reason_code": str(clarification_signal.get("reason_code") or "ambiguous_value"),
                "expected_input_type": str(clarification_signal.get("expected_input_type") or "free_text"),
                "candidates": list(clarification_signal.get("candidates") or []),
                "provenance_snapshot": list(clarification_signal.get("provenance_snapshot") or []),
                "answer_source": "pending_clarification",
                "answer_sources": ["pending_clarification"],
                "grounding": query_result,
                "model": "deterministic_clarification",
            }

        history_text = "\n".join(
            f"{item['role']}: {item['content']}" for item in self.state.history[-10:]
        )
        grounding_text = json.dumps(query_result, ensure_ascii=False) if query_result else "{}"

        prompt = (
            "You are an IDMS assistant.\n"
            "Use the grounding result when present.\n"
            "If grounding source is sql_exact or qdrant_sql_fallback, prefer it over speculation.\n"
            "Be concise and factual.\n\n"
            f"Grounding:\n{grounding_text}\n\n"
            f"Recent conversation:\n{history_text or 'N/A'}\n\n"
            f"User: {user_message}"
        )

        # Select model based on query source: Flash (deterministic), Pro (complex)
        answer_source = (
            query_result.get("answer_source", "unknown")
            if isinstance(query_result, dict)
            else "model_only"
        )
        
        deterministic_sources = {
            "sql_exact", "sql_pattern_match",
            "keyword_payload_fallback", "keyword_candidates",
            "identifier_multilang_gate", "sql_lookup"
        }
        parsed_confidence = None
        if isinstance(query_result, dict):
            grounding = query_result.get("grounding") if isinstance(query_result.get("grounding"), dict) else {}
            parsed_payload = grounding.get("parsed") if isinstance(grounding.get("parsed"), dict) else {}
            parsed_confidence = parsed_payload.get("confidence")

        selected_model = self._select_model_by_confidence(parsed_confidence, default=EXTRACT_MODEL)
        if answer_source in deterministic_sources:
            selected_model = EXTRACT_MODEL
        self._record_model_usage("interaction_chat", selected_model, parsed_confidence)
        
        response = generate_content_with_openrouter_fallback(
            primary_call=lambda: self.client.models.generate_content(
                model=selected_model,
                contents=[prompt],
            ),
            model=selected_model,
            contents=[prompt],
            temperature=0.0,
            call_name="interaction_chat",
            complexity="simple",
        )
        assistant_text = (response.text or "").strip()
        if isinstance(query_result, dict):
            query_sources = [
                str(
                    query_result.get("answer_source")
                    or query_result.get("source")
                    or "unknown"
                ).strip().lower()
            ]
        else:
            query_sources = ["model_only"]
        assistant_text = f"[source: {'+'.join(query_sources)}] {assistant_text}".strip()

        self.state.history.append({"role": "user", "content": user_message})
        self.state.history.append({"role": "assistant", "content": assistant_text})

        return {
            "answer": assistant_text,
            "answer_source": query_sources[0],
            "answer_sources": query_sources,
            "grounding": query_result,
            "evidence_bundle": self._build_query_evidence_bundle(user_message, query_result) if isinstance(query_result, dict) else None,
            "model": selected_model,
        }

    def _extract_json_object(self, raw_text: str) -> dict[str, Any]:
        text = str(raw_text or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        # Fallback for fenced or mixed outputs.
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _safe_confidence(self, value: Any, default: float = 1.0) -> float:
        try:
            score = float(value)
        except Exception:
            score = float(default)
        return max(0.0, min(1.0, score))

    def _select_model_by_confidence(self, confidence: Any, *, default: str = EXTRACT_MODEL) -> str:
        score = self._safe_confidence(confidence, default=1.0)
        return CHAT_MODEL if score < float(FLASH_CONFIDENCE_THRESHOLD) else str(default)

    def _record_model_usage(self, call_name: str, model: str, confidence: Any = None) -> dict[str, Any]:
        score = self._safe_confidence(confidence, default=1.0)
        stats = self.model_usage_stats
        stats["total_calls"] = int(stats.get("total_calls", 0)) + 1

        is_chat = str(model or "").strip() == str(CHAT_MODEL)
        if is_chat:
            stats["chat_calls"] = int(stats.get("chat_calls", 0)) + 1
        else:
            stats["flash_calls"] = int(stats.get("flash_calls", 0)) + 1

        escalated = is_chat and score < float(FLASH_CONFIDENCE_THRESHOLD)
        if escalated:
            stats["escalations"] = int(stats.get("escalations", 0)) + 1

        by_call = stats.get("by_call_name")
        if not isinstance(by_call, dict):
            by_call = {}
            stats["by_call_name"] = by_call

        call_key = str(call_name or "unknown").strip().lower()
        call_stats = by_call.get(call_key)
        if not isinstance(call_stats, dict):
            call_stats = {
                "total": 0,
                "flash": 0,
                "chat": 0,
                "escalations": 0,
            }
            by_call[call_key] = call_stats

        call_stats["total"] = int(call_stats.get("total", 0)) + 1
        if is_chat:
            call_stats["chat"] = int(call_stats.get("chat", 0)) + 1
        else:
            call_stats["flash"] = int(call_stats.get("flash", 0)) + 1
        if escalated:
            call_stats["escalations"] = int(call_stats.get("escalations", 0)) + 1

        total_calls = int(stats.get("total_calls", 0))
        chat_calls = int(stats.get("chat_calls", 0))
        escalation_count = int(stats.get("escalations", 0))
        chat_rate = (chat_calls / total_calls) if total_calls else 0.0
        escalation_rate = (escalation_count / total_calls) if total_calls else 0.0

        snapshot = {
            "total_calls": total_calls,
            "flash_calls": int(stats.get("flash_calls", 0)),
            "chat_calls": chat_calls,
            "escalations": escalation_count,
            "chat_rate": round(chat_rate, 4),
            "escalation_rate": round(escalation_rate, 4),
        }
        self.logger.info(
            "model_usage call=%s model=%s confidence=%.3f threshold=%.3f total=%s flash=%s chat=%s escalations=%s",
            call_key,
            model,
            score,
            float(FLASH_CONFIDENCE_THRESHOLD),
            snapshot["total_calls"],
            snapshot["flash_calls"],
            snapshot["chat_calls"],
            snapshot["escalations"],
        )
        return snapshot

    def get_model_usage_stats(self) -> dict[str, Any]:
        stats = self.model_usage_stats
        total_calls = int(stats.get("total_calls", 0))
        chat_calls = int(stats.get("chat_calls", 0))
        escalation_count = int(stats.get("escalations", 0))
        return {
            "total_calls": total_calls,
            "flash_calls": int(stats.get("flash_calls", 0)),
            "chat_calls": chat_calls,
            "escalations": escalation_count,
            "chat_rate": round((chat_calls / total_calls), 4) if total_calls else 0.0,
            "escalation_rate": round((escalation_count / total_calls), 4) if total_calls else 0.0,
            "by_call_name": stats.get("by_call_name", {}),
        }

    def _get_command_nlp(self) -> Any | None:
        if self._command_nlp_initialized:
            return self._command_nlp
        self._command_nlp_initialized = True
        if spacy is None:
            self._command_nlp = None
            return None

        for model_name in ("en_core_web_sm", "de_core_news_sm"):
            try:
                self._command_nlp = spacy.load(model_name)
                return self._command_nlp
            except Exception:
                continue
        self._command_nlp = None
        return None

    def _extract_domain_definition_json(self, payload: dict[str, Any], text: str) -> dict[str, Any]:
        candidates = [
            payload.get("definition"),
            payload.get("data"),
            payload.get("definition_json"),
            payload.get("json"),
            payload.get("payload"),
        ]
        for candidate in candidates:
            if isinstance(candidate, dict):
                return dict(candidate)
            if isinstance(candidate, str):
                parsed = self._extract_json_object(candidate)
                if parsed:
                    return parsed

        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
        if fenced:
            parsed = self._extract_json_object(fenced.group(1))
            if parsed:
                return parsed

        parsed = self._extract_json_object(text)
        return parsed if isinstance(parsed, dict) else {}

    def _resolve_domain_definition_operation(self, payload: dict[str, Any], text: str) -> str:
        explicit = str(payload.get("operation") or payload.get("crud_operation") or "").strip().lower()
        if explicit:
            mapping = {
                "add": "create",
                "insert": "create",
                "new": "create",
                "create": "create",
                "update": "update",
                "modify": "update",
                "edit": "update",
                "change": "update",
                "replace": "update",
                "delete": "delete",
                "remove": "delete",
                "drop": "delete",
                "read": "read",
                "get": "read",
                "show": "read",
                "view": "read",
                "list": "list",
            }
            if explicit in mapping:
                return mapping[explicit]

        lowered = str(text or "").strip().lower()
        op_aliases = {
            "create": ["create", "add", "insert", "new", "register"],
            "update": ["update", "modify", "edit", "change", "replace", "set"],
            "delete": ["delete", "remove", "drop", "deactivate"],
            "list": ["list", "all", "browse", "overview"],
            "read": ["get", "show", "read", "view", "find"],
        }
        for op_name, aliases in op_aliases.items():
            if any(re.search(rf"\b{re.escape(alias)}\b", lowered) for alias in aliases):
                return op_name

        nlp = self._get_command_nlp()
        if nlp is not None and lowered:
            try:
                lemmas = {str(token.lemma_ or token.text).strip().lower() for token in nlp(lowered)}
            except Exception:
                lemmas = set()
            for op_name, aliases in op_aliases.items():
                if lemmas.intersection(aliases):
                    return op_name
        return ""

    def _resolve_domain_definition_type(self, payload: dict[str, Any], text: str) -> str:
        explicit = str(payload.get("definition_type") or payload.get("type") or "").strip().lower()
        if explicit:
            if explicit in {"account_definition", "account", "coa", "chart_of_accounts", "chart of accounts", "ledger_account", "konto"}:
                return "account_definition"
            if explicit in {"hr_department_definition", "department", "department_definition", "dept", "abteilung"}:
                return "hr_department_definition"
            if explicit in {"hr_role_definition", "role", "role_definition", "position", "job_role", "funktion", "stelle"}:
                return "hr_role_definition"

        lowered = str(text or "").strip().lower()
        type_aliases = {
            "account_definition": ["account definition", "account", "coa", "chart of accounts", "ledger account", "gl account", "konto", "konten"],
            "hr_department_definition": ["department definition", "department", "hr department", "dept", "abteilung"],
            "hr_role_definition": ["role definition", "role", "job role", "position", "funktion", "stelle"],
        }
        for definition_type, aliases in type_aliases.items():
            if any(alias in lowered for alias in aliases):
                return definition_type

        nlp = self._get_command_nlp()
        if nlp is not None and lowered:
            try:
                lemmas = {str(token.lemma_ or token.text).strip().lower() for token in nlp(lowered)}
            except Exception:
                lemmas = set()
            if lemmas.intersection({"account", "ledger", "konto", "coa"}):
                return "account_definition"
            if lemmas.intersection({"department", "dept", "abteilung"}):
                return "hr_department_definition"
            if lemmas.intersection({"role", "position", "funktion", "stelle"}):
                return "hr_role_definition"

        return ""

    def _llm_extract_domain_definition_request(
        self,
        text: str,
        operation_hint: str,
        definition_type_hint: str,
    ) -> dict[str, Any]:
        prompt = (
            "You extract CRUD intent for domain definitions from user text.\n"
            "Return JSON only with keys: operation, definition_type, definition, confidence, errors.\n"
            "operation must be one of: create, update, delete, read, list.\n"
            "definition_type must be one of: account_definition, hr_department_definition, hr_role_definition.\n"
            "definition must be a JSON object with extracted fields.\n"
            "If uncertain, keep unknown fields empty and add a short error message to errors.\n"
            f"operation_hint: {operation_hint or 'none'}\n"
            f"definition_type_hint: {definition_type_hint or 'none'}\n"
            f"user_text:\n{text}\n"
        )
        selected_model = EXTRACT_MODEL
        self._record_model_usage("domain_definition_crud_parser", selected_model, 1.0)
        response = generate_content_with_openrouter_fallback(
            primary_call=lambda: self.client.models.generate_content(
                model=selected_model,
                contents=[prompt],
            ),
            model=selected_model,
            contents=[prompt],
            temperature=0.0,
            call_name="domain_definition_crud_parser",
            complexity="simple",
        )
        parsed = self._extract_json_object(response.text or "")
        return parsed if isinstance(parsed, dict) else {}

    def _get_solf_symbols_snapshot(self) -> dict[str, Any]:
        """Extract lightweight SOLF class/clause symbols for planner grounding."""
        script_path = Path(__file__).with_name("solf_script.txt")
        if not script_path.exists():
            return {
                "classes": [],
                "clauses": [],
                "source": str(script_path),
                "available": False,
            }

        try:
            text = script_path.read_text(encoding="utf-8")
        except Exception:
            return {
                "classes": [],
                "clauses": [],
                "source": str(script_path),
                "available": False,
            }

        class_names = sorted({
            str(match.group(1)).strip()
            for match in re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*≔\s*\{", text, flags=re.MULTILINE)
        })
        clause_names = sorted({
            str(match.group(1)).strip()
            for match in re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\([^\)]*\)\s*⦃", text, flags=re.MULTILINE)
        })
        return {
            "classes": class_names[:300],
            "clauses": clause_names[:600],
            "class_count": len(class_names),
            "clause_count": len(clause_names),
            "source": str(script_path),
            "available": True,
        }

    def _coerce_workflow_design_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        out = payload if isinstance(payload, dict) else {}
        workflow_steps = out.get("workflow_steps") if isinstance(out.get("workflow_steps"), list) else []
        gaps = out.get("gaps") if isinstance(out.get("gaps"), list) else []
        solf_candidates = out.get("solf_candidates") if isinstance(out.get("solf_candidates"), list) else []
        next_questions = out.get("next_questions") if isinstance(out.get("next_questions"), list) else []
        next_action = str(out.get("next_action") or "refine_with_context").strip().lower()
        if next_action not in {"finalize", "refine_with_context", "map_to_solf"}:
            next_action = "refine_with_context"
        return {
            "understanding": str(out.get("understanding") or "").strip(),
            "workflow_steps": workflow_steps,
            "gaps": gaps,
            "solf_candidates": solf_candidates,
            "next_questions": next_questions,
            "next_action": next_action,
        }

    def _workflow_design_iteration(
        self,
        request_text: str,
        iteration_index: int,
        context: dict[str, Any],
        previous_output: dict[str, Any] | None,
        solf_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = (
            "You are an IDMS workflow-design planner.\n"
            "Goal: iteratively produce a structured workflow plan for the request.\n"
            "Do NOT generate Python code. Focus on process steps, data requirements, SOLF mapping candidates, and open gaps.\n"
            "Return JSON only with keys: understanding, workflow_steps, gaps, solf_candidates, next_questions, next_action.\n"
            "next_action must be one of: finalize, refine_with_context, map_to_solf.\n"
            "workflow_steps: list of objects with keys: step_id, name, purpose, required_inputs, expected_outputs, retrieval_or_action.\n"
            "solf_candidates: list of objects with keys: class_name, clause_name, usage_reason, confidence.\n"
            "gaps: list of concise unresolved items.\n"
            "Use internal context and SOLF symbols where relevant.\n\n"
            f"Request:\n{request_text}\n\n"
            f"Iteration: {iteration_index}\n"
            f"Internal context JSON:\n{json.dumps(context or {}, ensure_ascii=False)}\n\n"
            f"Available SOLF symbols:\n{json.dumps(solf_snapshot, ensure_ascii=False)}\n\n"
            f"Previous iteration output JSON:\n{json.dumps(previous_output or {}, ensure_ascii=False)}"
        )
        selected_model = EXTRACT_MODEL
        self._record_model_usage("workflow_design_iteration", selected_model, 1.0)
        response = generate_content_with_openrouter_fallback(
            primary_call=lambda: self.client.models.generate_content(
                model=selected_model,
                contents=[prompt],
            ),
            model=selected_model,
            contents=[prompt],
            temperature=0.0,
            call_name="workflow_design_iteration",
            complexity="complex",
        )
        parsed = self._extract_json_object(response.text or "")
        return self._coerce_workflow_design_payload(parsed)

    def design_workflow_interaction(
        self,
        request_text: str,
        context: dict[str, Any] | None = None,
        max_iterations: int = 3,
    ) -> dict[str, Any]:
        """Iterative LLM planner for abstract workflow/process requests.

        Produces stepwise structured output that can be refined using internal context
        and SOLF symbol grounding across multiple iterations.
        """
        normalized_request = str(request_text or "").strip()
        if not normalized_request:
            raise ValueError("request_text must not be empty")

        iterations = min(max(1, int(max_iterations)), 8)
        working_context = context if isinstance(context, dict) else {}
        solf_snapshot = self._get_solf_symbols_snapshot()

        run_trace: list[dict[str, Any]] = []
        current_output: dict[str, Any] = {}
        for idx in range(iterations):
            iter_no = idx + 1
            current_output = self._workflow_design_iteration(
                request_text=normalized_request,
                iteration_index=iter_no,
                context=working_context,
                previous_output=current_output,
                solf_snapshot=solf_snapshot,
            )
            run_trace.append(
                {
                    "iteration": iter_no,
                    "next_action": current_output.get("next_action"),
                    "workflow_step_count": len(current_output.get("workflow_steps") or []),
                    "gap_count": len(current_output.get("gaps") or []),
                    "solf_candidate_count": len(current_output.get("solf_candidates") or []),
                    "payload": current_output,
                }
            )

            next_action = str(current_output.get("next_action") or "refine_with_context").strip().lower()
            has_steps = bool(current_output.get("workflow_steps"))
            has_gaps = bool(current_output.get("gaps"))
            if next_action == "finalize" and has_steps:
                break
            if idx >= iterations - 1:
                break

            if next_action in {"map_to_solf", "refine_with_context"}:
                working_context = {
                    **working_context,
                    "latest_workflow_steps": current_output.get("workflow_steps"),
                    "latest_gaps": current_output.get("gaps"),
                    "latest_solf_candidates": current_output.get("solf_candidates"),
                    "latest_next_questions": current_output.get("next_questions"),
                    "has_unresolved_gaps": has_gaps,
                }

        summary = {
            "request": normalized_request,
            "iterations_executed": len(run_trace),
            "final_next_action": str(current_output.get("next_action") or "").strip(),
            "workflow_step_count": len(current_output.get("workflow_steps") or []),
            "gap_count": len(current_output.get("gaps") or []),
            "solf_candidate_count": len(current_output.get("solf_candidates") or []),
        }
        return {
            "success": True,
            "summary": summary,
            "final_plan": current_output,
            "iterations": run_trace,
            "solf_snapshot": {
                "available": bool(solf_snapshot.get("available")),
                "class_count": int(solf_snapshot.get("class_count") or 0),
                "clause_count": int(solf_snapshot.get("clause_count") or 0),
            },
        }

    def _coerce_complex_interaction_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        out = payload if isinstance(payload, dict) else {}
        objectives = out.get("objectives") if isinstance(out.get("objectives"), list) else []
        planned_steps = out.get("planned_steps") if isinstance(out.get("planned_steps"), list) else []
        proposed_actions = out.get("proposed_actions") if isinstance(out.get("proposed_actions"), list) else []
        retrieval_plan = out.get("retrieval_plan") if isinstance(out.get("retrieval_plan"), list) else []
        solf_candidates = out.get("solf_candidates") if isinstance(out.get("solf_candidates"), list) else []
        missing_information = out.get("missing_information") if isinstance(out.get("missing_information"), list) else []
        next_action = str(out.get("next_action") or "refine_with_context").strip().lower()
        if next_action not in {
            "finalize",
            "refine_with_context",
            "execute_query",
            "execute_interaction",
            "ask_user_input",
            "map_to_solf",
        }:
            next_action = "refine_with_context"
        return {
            "understanding": str(out.get("understanding") or "").strip(),
            "objectives": objectives,
            "planned_steps": planned_steps,
            "proposed_actions": proposed_actions,
            "retrieval_plan": retrieval_plan,
            "solf_candidates": solf_candidates,
            "missing_information": missing_information,
            "user_prompt": str(out.get("user_prompt") or "").strip(),
            "response_draft": str(out.get("response_draft") or "").strip(),
            "next_action": next_action,
        }

    def _complex_interaction_iteration(
        self,
        request_text: str,
        iteration_index: int,
        context: dict[str, Any],
        previous_output: dict[str, Any] | None,
        solf_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = (
            "You are an IDMS complex interaction planner.\n"
            "Goal: iteratively resolve abstract/complex query or interaction requests.\n"
            "Do NOT generate Python code.\n"
            "Return JSON only with keys: understanding, objectives, planned_steps, proposed_actions, retrieval_plan, solf_candidates, missing_information, user_prompt, response_draft, next_action.\n"
            "next_action must be one of: finalize, refine_with_context, execute_query, execute_interaction, ask_user_input, map_to_solf.\n"
            "When information is missing, set next_action=ask_user_input and provide a concise user_prompt.\n"
            "planned_steps: structured list of step objects (step_id, name, purpose, inputs, outputs).\n"
            "proposed_actions: list of intended interaction/tool actions without code generation.\n"
            "retrieval_plan: list of data retrieval intents/filters needed to answer correctly.\n"
            "solf_candidates: list with class_name, clause_name, usage_reason, confidence.\n\n"
            f"Request:\n{request_text}\n\n"
            f"Iteration: {iteration_index}\n"
            f"Internal context JSON:\n{json.dumps(context or {}, ensure_ascii=False)}\n\n"
            f"Available SOLF symbols:\n{json.dumps(solf_snapshot, ensure_ascii=False)}\n\n"
            f"Previous iteration output JSON:\n{json.dumps(previous_output or {}, ensure_ascii=False)}"
        )
        selected_model = EXTRACT_MODEL
        self._record_model_usage("complex_interaction_iteration", selected_model, 1.0)
        response = generate_content_with_openrouter_fallback(
            primary_call=lambda: self.client.models.generate_content(
                model=selected_model,
                contents=[prompt],
            ),
            model=selected_model,
            contents=[prompt],
            temperature=0.0,
            call_name="complex_interaction_iteration",
            complexity="complex",
        )
        parsed = self._extract_json_object(response.text or "")
        return self._coerce_complex_interaction_payload(parsed)

    def resolve_complex_interaction(
        self,
        request_text: str,
        context: dict[str, Any] | None = None,
        max_iterations: int = 3,
    ) -> dict[str, Any]:
        """Iteratively resolve a complex query/interaction into actionable structured output.

        This planner is not workflow-only; it supports abstract interaction requests,
        query decomposition, SOLF mapping hints, and user clarification prompts.
        """
        normalized_request = str(request_text or "").strip()
        if not normalized_request:
            raise ValueError("request_text must not be empty")

        iterations = min(max(1, int(max_iterations)), 8)
        working_context = context if isinstance(context, dict) else {}
        solf_snapshot = self._get_solf_symbols_snapshot()

        run_trace: list[dict[str, Any]] = []
        current_output: dict[str, Any] = {}
        needs_user_input = False
        user_prompt = ""

        for idx in range(iterations):
            iter_no = idx + 1
            current_output = self._complex_interaction_iteration(
                request_text=normalized_request,
                iteration_index=iter_no,
                context=working_context,
                previous_output=current_output,
                solf_snapshot=solf_snapshot,
            )
            run_trace.append(
                {
                    "iteration": iter_no,
                    "next_action": current_output.get("next_action"),
                    "objective_count": len(current_output.get("objectives") or []),
                    "planned_step_count": len(current_output.get("planned_steps") or []),
                    "missing_information_count": len(current_output.get("missing_information") or []),
                    "payload": current_output,
                }
            )

            next_action = str(current_output.get("next_action") or "refine_with_context").strip().lower()
            if next_action == "ask_user_input":
                prompt_text = str(current_output.get("user_prompt") or "").strip()
                user_prompt = prompt_text or "Please provide the missing details so I can continue."
                needs_user_input = True
                break

            has_plan = bool(current_output.get("planned_steps")) or bool(current_output.get("response_draft"))
            if next_action == "finalize" and has_plan:
                break

            if idx >= iterations - 1:
                break

            working_context = {
                **working_context,
                "latest_objectives": current_output.get("objectives"),
                "latest_planned_steps": current_output.get("planned_steps"),
                "latest_proposed_actions": current_output.get("proposed_actions"),
                "latest_retrieval_plan": current_output.get("retrieval_plan"),
                "latest_solf_candidates": current_output.get("solf_candidates"),
                "latest_missing_information": current_output.get("missing_information"),
            }

        summary = {
            "request": normalized_request,
            "iterations_executed": len(run_trace),
            "final_next_action": str(current_output.get("next_action") or "").strip(),
            "needs_user_input": bool(needs_user_input),
            "objective_count": len(current_output.get("objectives") or []),
            "planned_step_count": len(current_output.get("planned_steps") or []),
            "missing_information_count": len(current_output.get("missing_information") or []),
        }
        result = {
            "success": True,
            "summary": summary,
            "final_resolution": current_output,
            "iterations": run_trace,
            "solf_snapshot": {
                "available": bool(solf_snapshot.get("available")),
                "class_count": int(solf_snapshot.get("class_count") or 0),
                "clause_count": int(solf_snapshot.get("clause_count") or 0),
            },
        }
        if needs_user_input:
            result["user_prompt"] = user_prompt
        return result

    def _build_interaction_capability_profile(self, solf_snapshot: dict[str, Any]) -> dict[str, Any]:
        action_capabilities = [
            "query",
            "chat",
            "perform_action",
            "evaluate_solf_clause",
            "design_workflow_interaction",
            "resolve_complex_interaction",
            "resolve_complex_interaction_production",
        ]
        return {
            "planner_scope": "generic_interaction_resolution",
            "non_goals": [
                "freeform legal compliance claims without sources",
                "final numeric computation outside deterministic code",
            ],
            "available_actions": action_capabilities,
            "generic_action_types": ["query", "interaction", "chat", "solf"],
            "deterministic_calculators": ["payroll_net_v1"],
            "required_external_fact_fields": [
                "canonical_key",
                "value",
                "source_url",
                "effective_date",
                "jurisdiction",
            ],
            "solf": {
                "available": bool(solf_snapshot.get("available")),
                "class_count": int(solf_snapshot.get("class_count") or 0),
                "clause_count": int(solf_snapshot.get("clause_count") or 0),
            },
        }

    def _coerce_complex_production_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        out = payload if isinstance(payload, dict) else {}
        next_action = str(out.get("next_action") or "refine_with_context").strip().lower()
        if next_action not in {
            "finalize",
            "refine_with_context",
            "ask_user_input",
            "retrieve_external",
            "calculate",
            "execute_query",
            "execute_interaction",
            "map_to_solf",
        }:
            next_action = "refine_with_context"
        return {
            "understanding": str(out.get("understanding") or "").strip(),
            "workflow_steps": out.get("workflow_steps") if isinstance(out.get("workflow_steps"), list) else [],
            "required_information": out.get("required_information") if isinstance(out.get("required_information"), list) else [],
            "external_retrieval_requests": out.get("external_retrieval_requests") if isinstance(out.get("external_retrieval_requests"), list) else [],
            "mapping_plan": out.get("mapping_plan") if isinstance(out.get("mapping_plan"), list) else [],
            "execution_actions": out.get("execution_actions") if isinstance(out.get("execution_actions"), list) else [],
            "missing_information": out.get("missing_information") if isinstance(out.get("missing_information"), list) else [],
            "user_prompt": str(out.get("user_prompt") or "").strip(),
            "response_draft": str(out.get("response_draft") or "").strip(),
            "calculator_id": str(out.get("calculator_id") or "").strip().lower(),
            "next_action": next_action,
        }

    def _complex_interaction_production_iteration(
        self,
        request_text: str,
        iteration_index: int,
        context: dict[str, Any],
        previous_output: dict[str, Any],
        capability_profile: dict[str, Any],
        llm_model: str,
    ) -> dict[str, Any]:
        prompt = (
            "You are an IDMS production-grade interaction planner.\\n"
            "Return JSON only with keys: understanding, workflow_steps, required_information, external_retrieval_requests, mapping_plan, execution_actions, missing_information, user_prompt, response_draft, calculator_id, next_action.\\n"
            "Use only these next_action values: finalize, refine_with_context, ask_user_input, retrieve_external, calculate, execute_query, execute_interaction, map_to_solf.\\n"
            "required_information items must include: canonical_key, label, source_preference (internal|user|external), required (true|false), reason.\\n"
            "external_retrieval_requests items must include: canonical_key, retrieval_query, jurisdiction, required_fields.\\n"
            "execution_actions items must include: action_type (query|interaction|chat|solf), name, payload, args.\\n"
            "Do not generate code. Keep workflow generic and reusable.\\n\\n"
            f"Request:\\n{request_text}\\n\\n"
            f"Iteration: {iteration_index}\\n\\n"
            f"IDMS capability profile JSON:\\n{json.dumps(capability_profile or {}, ensure_ascii=False)}\\n\\n"
            f"Current context JSON:\\n{json.dumps(context or {}, ensure_ascii=False)}\\n\\n"
            f"Previous output JSON:\\n{json.dumps(previous_output or {}, ensure_ascii=False)}"
        )
        selected_model = str(llm_model or EXTRACT_MODEL)
        self._record_model_usage("complex_interaction_production_iteration", selected_model, 1.0)
        response = generate_content_with_openrouter_fallback(
            primary_call=lambda: self.client.models.generate_content(
                model=selected_model,
                contents=[prompt],
            ),
            model=selected_model,
            contents=[prompt],
            temperature=0.0,
            call_name="complex_interaction_production_iteration",
            complexity="complex",
        )
        parsed = self._extract_json_object(response.text or "")
        return self._coerce_complex_production_payload(parsed)

    def _canonicalize_fact_key(self, key: Any) -> str:
        token = re.sub(r"[^a-z0-9]+", "_", str(key or "").strip().lower()).strip("_")
        aliases = {
            "gross_salary": "gross_salary_monthly",
            "gross_monthly_salary": "gross_salary_monthly",
            "gross_salary_chf": "gross_salary_monthly",
            "ahv_iv_eo": "ahv_iv_eo_rate_employee",
            "ahv_iv_eo_rate": "ahv_iv_eo_rate_employee",
            "alv": "alv_rate_employee",
            "alv_rate": "alv_rate_employee",
            "children_allowance": "children_allowance_monthly",
            "child_allowance": "children_allowance_monthly",
            "quellensteuer": "withholding_tax_rate_employee",
            "withholding_tax": "withholding_tax_rate_employee",
            "withholding_tax_rate": "withholding_tax_rate_employee",
        }
        return aliases.get(token, token)

    def _normalize_requirement_item(self, item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            raw_key = item.get("canonical_key") or item.get("field") or item.get("key") or item.get("name")
            source_pref = str(item.get("source_preference") or item.get("source") or "external").strip().lower()
            if source_pref not in {"internal", "user", "external"}:
                source_pref = "external"
            return {
                "canonical_key": self._canonicalize_fact_key(raw_key),
                "label": str(item.get("label") or raw_key or "").strip(),
                "source_preference": source_pref,
                "required": bool(item.get("required", True)),
                "reason": str(item.get("reason") or "").strip(),
            }
        key = self._canonicalize_fact_key(item)
        return {
            "canonical_key": key,
            "label": str(item or "").strip(),
            "source_preference": "external",
            "required": True,
            "reason": "",
        }

    def _collect_initial_facts_from_context(self, context: dict[str, Any]) -> dict[str, Any]:
        facts: dict[str, Any] = {}

        def _merge(container: Any) -> None:
            if not isinstance(container, dict):
                return
            for raw_key, value in container.items():
                if isinstance(value, (str, int, float, bool)):
                    key = self._canonicalize_fact_key(raw_key)
                    if key:
                        facts[key] = value

        _merge(context)
        _merge(context.get("known_facts"))
        _merge(context.get("international_data"))
        _merge(context.get("data"))
        return facts

    def _coerce_external_fact_payload(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        facts_raw = payload.get("facts") if isinstance(payload, dict) else []
        if not isinstance(facts_raw, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in facts_raw:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "canonical_key": self._canonicalize_fact_key(
                        item.get("canonical_key") or item.get("field") or item.get("key")
                    ),
                    "value": item.get("value"),
                    "value_type": str(item.get("value_type") or "").strip(),
                    "unit": str(item.get("unit") or "").strip(),
                    "jurisdiction": str(item.get("jurisdiction") or "").strip(),
                    "effective_date": str(item.get("effective_date") or "").strip(),
                    "source_url": str(item.get("source_url") or "").strip(),
                    "source_title": str(item.get("source_title") or "").strip(),
                    "confidence": item.get("confidence"),
                    "notes": str(item.get("notes") or "").strip(),
                }
            )
        return normalized

    def _retrieve_external_facts_via_llm(
        self,
        request_text: str,
        retrieval_requests: list[dict[str, Any]],
        llm_model: str,
    ) -> list[dict[str, Any]]:
        prompt = (
            "You are an IDMS external fact retriever.\\n"
            "Return JSON only with key facts (array).\\n"
            "Each fact item must include: canonical_key, value, value_type, unit, jurisdiction, effective_date, source_url, source_title, confidence, notes.\\n"
            "Do not include facts without source_url and effective_date.\\n\\n"
            f"User request:\\n{request_text}\\n\\n"
            f"Retrieval requests JSON:\\n{json.dumps(retrieval_requests or [], ensure_ascii=False)}"
        )
        selected_model = str(llm_model or EXTRACT_MODEL)
        self._record_model_usage("complex_interaction_external_retrieval", selected_model, 1.0)
        response = generate_content_with_openrouter_fallback(
            primary_call=lambda: self.client.models.generate_content(
                model=selected_model,
                contents=[prompt],
            ),
            model=selected_model,
            contents=[prompt],
            temperature=0.0,
            call_name="complex_interaction_external_retrieval",
            complexity="complex",
        )
        payload = self._extract_json_object(response.text or "")
        return self._coerce_external_fact_payload(payload)

    def _is_valid_iso_date(self, value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return True
        except Exception:
            return False

    def _verify_external_source_live(self, source_url: str, timeout_seconds: int = 5) -> dict[str, Any]:
        url = str(source_url or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return {
                "reachable": False,
                "http_status": None,
                "final_url": "",
                "content_type": "",
                "content_hash": "",
                "error": "invalid_url",
            }

        timeout = min(max(1, int(timeout_seconds)), 20)
        try:
            request = urllib.request.Request(
                url,
                method="GET",
                headers={"User-Agent": "IDMS-SourceVerifier/1.0"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
                final_url = str(getattr(response, "geturl", lambda: url)() or url)
                content_type = str(response.headers.get("Content-Type") or "").strip().lower()
                sample = response.read(2048)
                content_hash = hashlib.sha256(sample or b"").hexdigest()
                return {
                    "reachable": bool(200 <= status < 400),
                    "http_status": status,
                    "final_url": final_url,
                    "content_type": content_type,
                    "content_hash": content_hash,
                    "error": "",
                }
        except urllib.error.HTTPError as exc:
            return {
                "reachable": False,
                "http_status": int(getattr(exc, "code", 0) or 0) or None,
                "final_url": url,
                "content_type": "",
                "content_hash": "",
                "error": f"http_error:{int(getattr(exc, 'code', 0) or 0)}",
            }
        except Exception as exc:
            return {
                "reachable": False,
                "http_status": None,
                "final_url": url,
                "content_type": "",
                "content_hash": "",
                "error": str(exc),
            }

    def _is_source_domain_approved(self, source_url: str, approved_domains: list[str] | None) -> bool:
        domains = [str(d or "").strip().lower() for d in (approved_domains or []) if str(d or "").strip()]
        if not domains:
            return True
        url = str(source_url or "").strip()
        if not url:
            return False
        try:
            host = str(urllib.parse.urlparse(url).hostname or "").strip().lower()
        except Exception:
            return False
        if not host:
            return False
        for domain in domains:
            d = domain.lstrip(".")
            if host == d or host.endswith(f".{d}"):
                return True
        return False

    def _validate_external_fact(
        self,
        fact: dict[str, Any],
        strict_provenance: bool,
        verify_external_sources: bool = False,
        timeout_seconds: int = 5,
        enforce_approved_domains: bool = False,
        approved_source_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        source_url = str(fact.get("source_url") or "").strip().lower()
        effective_date = str(fact.get("effective_date") or "").strip()
        provenance_ok = source_url.startswith("http://") or source_url.startswith("https://")
        effective_date_ok = self._is_valid_iso_date(effective_date)
        domain_approved = self._is_source_domain_approved(source_url, approved_source_domains)
        source_verification = {
            "checked": False,
            "reachable": None,
            "http_status": None,
            "final_url": "",
            "content_type": "",
            "content_hash": "",
            "error": "",
        }
        if verify_external_sources and provenance_ok:
            live = self._verify_external_source_live(source_url, timeout_seconds=timeout_seconds)
            source_verification = {
                "checked": True,
                **live,
            }
        usable = bool(fact.get("canonical_key")) and fact.get("value") is not None
        if strict_provenance and (not provenance_ok or not effective_date_ok):
            usable = False
        if strict_provenance and enforce_approved_domains and not domain_approved:
            usable = False
        if strict_provenance and verify_external_sources and source_verification.get("checked") and not source_verification.get("reachable"):
            usable = False
        return {
            **fact,
            "usable": usable,
            "validation": {
                "provenance_ok": provenance_ok,
                "effective_date_ok": effective_date_ok,
                "domain_approved": domain_approved,
                "source_verification": source_verification,
            },
        }

    def _execute_generic_complex_actions(
        self,
        request_text: str,
        next_action: str,
        execution_actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        actions = [a for a in execution_actions if isinstance(a, dict)]
        if not actions:
            if next_action == "execute_query":
                actions = [{"action_type": "query", "payload": {"question": request_text}}]
            elif next_action == "map_to_solf":
                actions = [{"action_type": "solf", "name": "", "payload": {}, "args": []}]

        results: list[dict[str, Any]] = []
        executed = False

        for action in actions:
            action_type = str(action.get("action_type") or "").strip().lower()
            payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
            args = action.get("args") if isinstance(action.get("args"), list) else []
            name = str(action.get("name") or "").strip()

            if action_type == "query":
                question = str(payload.get("question") or request_text).strip()
                out = self.query(question)
                executed = True
                results.append({"action_type": action_type, "success": True, "result": out})
                continue

            if action_type == "chat":
                message = str(payload.get("message") or request_text).strip()
                use_query_tool = bool(payload.get("use_query_tool", True))
                out = self.chat(message, use_query_tool=use_query_tool)
                executed = True
                results.append({"action_type": action_type, "success": True, "result": out})
                continue

            if action_type == "interaction":
                action_name = str(name or payload.get("action_name") or "").strip().lower()
                action_payload = payload.get("action_payload") if isinstance(payload.get("action_payload"), dict) else payload
                if action_name:
                    out = self.perform_action(action_name, action_payload)
                    executed = True
                    results.append({"action_type": action_type, "name": action_name, "success": bool(out.get("success", True)), "result": out})
                else:
                    results.append({"action_type": action_type, "success": False, "error": "missing_action_name"})
                continue

            if action_type == "solf":
                clause_name = str(name or payload.get("clause_name") or "").strip()
                clause_payload = payload.get("clause_payload") if isinstance(payload.get("clause_payload"), dict) else payload
                if clause_name:
                    out = self.evaluate_solf_clause(clause_name=clause_name, payload=clause_payload, args=args)
                    executed = True
                    results.append({"action_type": action_type, "clause_name": clause_name, "success": bool(out.get("success", True)), "result": out})
                else:
                    results.append({"action_type": action_type, "success": False, "error": "missing_clause_name"})
                continue

            results.append({"action_type": action_type or "unknown", "success": False, "error": "unsupported_action_type"})

        return {
            "executed": executed,
            "count": len(results),
            "results": results,
        }

    def _as_float(self, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace(",", "")
        if text.endswith("%"):
            text = text[:-1].strip()
            try:
                return float(text) / 100.0
            except Exception:
                return None
        try:
            return float(text)
        except Exception:
            return None

    def _calculate_payroll_net_v1(self, fact_map: dict[str, Any]) -> dict[str, Any]:
        gross = self._as_float(fact_map.get("gross_salary_monthly"))
        if gross is None:
            return {
                "success": False,
                "calculator_id": "payroll_net_v1",
                "missing_inputs": ["gross_salary_monthly"],
                "message": "gross_salary_monthly is required for payroll_net_v1",
            }

        rate_keys = [
            "ahv_iv_eo_rate_employee",
            "alv_rate_employee",
            "withholding_tax_rate_employee",
            "pension_rate_employee",
            "nbu_rate_employee",
        ]
        deductions: dict[str, float] = {}
        for key in rate_keys:
            raw = self._as_float(fact_map.get(key))
            if raw is None:
                continue
            rate = raw if raw <= 1.0 else raw / 100.0
            deductions[key] = round(gross * rate, 2)

        fixed_deductions = self._as_float(fact_map.get("fixed_deductions_monthly")) or 0.0
        children_allowance = self._as_float(fact_map.get("children_allowance_monthly")) or 0.0
        total_deductions = round(sum(deductions.values()) + fixed_deductions, 2)
        net_salary = round(gross - total_deductions + children_allowance, 2)

        return {
            "success": True,
            "calculator_id": "payroll_net_v1",
            "currency": str(fact_map.get("currency") or "CHF").strip() or "CHF",
            "gross_salary_monthly": round(gross, 2),
            "deductions": deductions,
            "fixed_deductions_monthly": round(fixed_deductions, 2),
            "children_allowance_monthly": round(children_allowance, 2),
            "total_deductions_monthly": total_deductions,
            "net_salary_monthly": net_salary,
        }

    def _run_deterministic_calculation(self, calculator_id: str, fact_map: dict[str, Any]) -> dict[str, Any]:
        selected = str(calculator_id or "").strip().lower()
        if not selected:
            has_payroll_signal = bool(
                fact_map.get("gross_salary_monthly")
                and (
                    fact_map.get("ahv_iv_eo_rate_employee")
                    or fact_map.get("alv_rate_employee")
                    or fact_map.get("withholding_tax_rate_employee")
                )
            )
            selected = "payroll_net_v1" if has_payroll_signal else ""

        if selected == "payroll_net_v1":
            return self._calculate_payroll_net_v1(fact_map)

        return {
            "success": False,
            "calculator_id": selected,
            "message": "No deterministic calculator executed",
        }

    def resolve_complex_interaction_production(
        self,
        request_text: str,
        context: dict[str, Any] | None = None,
        max_iterations: int = 3,
        llm_model: str = "",
        max_retrieval_rounds: int = 2,
        strict_provenance: bool = True,
        run_calculation: bool = True,
        execute_generic_actions: bool = True,
        verify_external_sources: bool = False,
        external_source_timeout_seconds: int = 5,
        enforce_approved_domains: bool = False,
        approved_source_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_request = str(request_text or "").strip()
        if not normalized_request:
            raise ValueError("request_text must not be empty")

        iterations = min(max(1, int(max_iterations)), 8)
        retrieval_rounds = min(max(1, int(max_retrieval_rounds)), 6)
        selected_model = str(llm_model or EXTRACT_MODEL)
        domains = [str(d or "").strip().lower() for d in (approved_source_domains or []) if str(d or "").strip()]
        if enforce_approved_domains and not domains:
            domains = ["admin.ch", "zg.ch", "ahv-iv.ch", "seco.admin.ch", "estv.admin.ch"]

        working_context = context if isinstance(context, dict) else {}
        solf_snapshot = self._get_solf_symbols_snapshot()
        capability_profile = self._build_interaction_capability_profile(solf_snapshot)

        run_trace: list[dict[str, Any]] = []
        current_output: dict[str, Any] = {}
        initial_facts = self._collect_initial_facts_from_context(working_context)
        fact_map = dict(initial_facts)
        external_facts: list[dict[str, Any]] = []
        generic_execution: dict[str, Any] = {"executed": False, "count": 0, "results": []}
        needs_user_input = False
        user_prompt = ""
        required_items: list[dict[str, Any]] = []

        for idx in range(iterations):
            iter_no = idx + 1
            current_output = self._complex_interaction_production_iteration(
                request_text=normalized_request,
                iteration_index=iter_no,
                context=working_context,
                previous_output=current_output,
                capability_profile=capability_profile,
                llm_model=selected_model,
            )

            required_items = [
                self._normalize_requirement_item(item)
                for item in (current_output.get("required_information") or [])
                if self._normalize_requirement_item(item).get("canonical_key")
            ]

            requested_external = current_output.get("external_retrieval_requests") or []
            retrieval_needed = [item for item in requested_external if isinstance(item, dict)]
            retrieval_attempted = False
            if retrieval_needed and iter_no <= retrieval_rounds:
                retrieval_attempted = True
                fetched = self._retrieve_external_facts_via_llm(
                    request_text=normalized_request,
                    retrieval_requests=retrieval_needed,
                    llm_model=selected_model,
                )
                validated = [
                    self._validate_external_fact(
                        item,
                        strict_provenance,
                        verify_external_sources=verify_external_sources,
                        timeout_seconds=external_source_timeout_seconds,
                        enforce_approved_domains=enforce_approved_domains,
                        approved_source_domains=domains,
                    )
                    for item in fetched
                ]
                for fact in validated:
                    if fact.get("usable") and fact.get("canonical_key"):
                        fact_map[str(fact.get("canonical_key"))] = fact.get("value")
                external_facts.extend(validated)

            missing_required: list[str] = []
            for item in required_items:
                if not item.get("required", True):
                    continue
                key = str(item.get("canonical_key") or "").strip()
                if not key:
                    continue
                if key not in fact_map or fact_map.get(key) in {None, ""}:
                    missing_required.append(key)

            next_action = str(current_output.get("next_action") or "refine_with_context").strip().lower()
            if next_action == "ask_user_input" or missing_required:
                needs_user_input = True
                prompt_text = str(current_output.get("user_prompt") or "").strip()
                if prompt_text:
                    user_prompt = prompt_text
                else:
                    labels: list[str] = []
                    for key in missing_required:
                        label = next(
                            (
                                str(entry.get("label") or key)
                                for entry in required_items
                                if str(entry.get("canonical_key") or "") == key
                            ),
                            key,
                        )
                        labels.append(label)
                    joined = ", ".join(labels) if labels else "the missing details"
                    user_prompt = f"Please provide {joined} so I can complete the response."

            run_trace.append(
                {
                    "iteration": iter_no,
                    "next_action": next_action,
                    "required_information_count": len(required_items),
                    "missing_required_count": len(missing_required),
                    "retrieval_attempted": retrieval_attempted,
                    "payload": current_output,
                }
            )

            if needs_user_input:
                break
            if execute_generic_actions and next_action in {"execute_query", "execute_interaction", "map_to_solf"}:
                generic_execution = self._execute_generic_complex_actions(
                    request_text=normalized_request,
                    next_action=next_action,
                    execution_actions=current_output.get("execution_actions") or [],
                )
                break
            if next_action in {"finalize", "calculate"}:
                break
            if idx >= iterations - 1:
                break

            working_context = {
                **working_context,
                "latest_required_information": required_items,
                "latest_external_retrieval_requests": retrieval_needed,
                "latest_known_fact_keys": sorted(list(fact_map.keys())),
                "latest_missing_information": current_output.get("missing_information"),
            }

        calculation_result = {
            "success": False,
            "calculator_id": str(current_output.get("calculator_id") or "").strip().lower(),
            "message": "Calculation skipped",
        }
        if run_calculation and not needs_user_input:
            calculation_result = self._run_deterministic_calculation(
                calculator_id=str(current_output.get("calculator_id") or "").strip().lower(),
                fact_map=fact_map,
            )

        summary = {
            "request": normalized_request,
            "iterations_executed": len(run_trace),
            "final_next_action": str(current_output.get("next_action") or "").strip(),
            "needs_user_input": bool(needs_user_input),
            "required_information_count": len(required_items),
            "fact_count": len(fact_map),
            "external_fact_count": len(external_facts),
            "calculation_executed": bool(calculation_result.get("success")),
            "generic_actions_executed": bool(generic_execution.get("executed")),
            "llm_model": selected_model,
        }

        result = {
            "success": True,
            "summary": summary,
            "final_resolution": current_output,
            "iterations": run_trace,
            "capability_profile": capability_profile,
            "mapped_facts": fact_map,
            "external_facts": external_facts,
            "generic_execution": generic_execution,
            "calculation": calculation_result,
            "provenance_policy": {
                "strict_provenance": bool(strict_provenance),
                "verify_external_sources": bool(verify_external_sources),
                "external_source_timeout_seconds": min(max(1, int(external_source_timeout_seconds)), 20),
                "enforce_approved_domains": bool(enforce_approved_domains),
                "approved_source_domains": domains,
                "required_fields": capability_profile.get("required_external_fact_fields"),
            },
            "solf_snapshot": {
                "available": bool(solf_snapshot.get("available")),
                "class_count": int(solf_snapshot.get("class_count") or 0),
                "clause_count": int(solf_snapshot.get("clause_count") or 0),
            },
        }
        if needs_user_input:
            result["user_prompt"] = user_prompt
        return result

    def _expected_fields_for_definition(self, definition_type: str, operation: str) -> list[str]:
        normalized_type = str(definition_type or "").strip().lower()
        normalized_op = str(operation or "").strip().lower()
        if normalized_type == "account_definition":
            if normalized_op in {"delete", "read"}:
                return ["legal_entity_ref", "account_number"]
            return ["legal_entity_ref", "account_number", "account_name", "account_type"]
        if normalized_type == "hr_department_definition":
            if normalized_op in {"delete", "read"}:
                return ["department_code"]
            return ["department_code", "department_name", "active"]
        if normalized_type == "hr_role_definition":
            if normalized_op in {"delete", "read"}:
                return ["role_code"]
            return ["role_code", "role_name", "role_family", "seniority_level", "active"]
        return []

    def _handle_domain_definition_crud_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            import domain_db
        except Exception as exc:
            return {
                "action": "domain_definition_crud",
                "success": False,
                "message": f"domain_db unavailable: {exc}",
            }

        raw_text_parts = [
            str(payload.get("text") or ""),
            str(payload.get("request") or ""),
            str(payload.get("instruction") or ""),
            str(payload.get("command") or ""),
            str(payload.get("message") or ""),
        ]
        raw_text = "\n".join(part for part in raw_text_parts if part.strip()).strip()

        operation = self._resolve_domain_definition_operation(payload, raw_text)
        definition_type = self._resolve_domain_definition_type(payload, raw_text)
        definition = self._extract_domain_definition_json(payload, raw_text)

        # Also accept direct key-value payloads when JSON block is not provided.
        if not definition:
            direct_keys = {
                "legal_entity_ref", "account_number", "account_name", "account_type",
                "department_code", "department_name",
                "role_code", "role_name", "role_family", "seniority_level",
                "active", "metadata",
            }
            direct_payload = {k: v for k, v in payload.items() if k in direct_keys}
            if direct_payload:
                definition = direct_payload

        needs_llm = (not operation) or (not definition_type) or (operation in {"create", "update", "delete", "read"} and not definition)
        llm_payload: dict[str, Any] = {}
        if needs_llm and raw_text:
            llm_payload = self._llm_extract_domain_definition_request(raw_text, operation, definition_type)
            if not operation:
                operation = str(llm_payload.get("operation") or "").strip().lower()
            if not definition_type:
                definition_type = str(llm_payload.get("definition_type") or "").strip().lower()
            llm_definition = llm_payload.get("definition") if isinstance(llm_payload.get("definition"), dict) else {}
            if llm_definition:
                merged = dict(llm_definition)
                merged.update(definition)
                definition = merged

        op_map = {
            "add": "create",
            "insert": "create",
            "new": "create",
            "modify": "update",
            "edit": "update",
            "change": "update",
            "remove": "delete",
            "drop": "delete",
            "get": "read",
            "show": "read",
            "view": "read",
        }
        operation = op_map.get(operation, operation)
        definition_type = domain_db.normalize_definition_type(definition_type)

        if operation not in {"create", "update", "delete", "read", "list"}:
            return {
                "action": "domain_definition_crud",
                "success": False,
                "message": "Could not resolve CRUD operation. Use one of create/update/delete/read/list.",
                "resolved": {
                    "operation": operation,
                    "definition_type": definition_type,
                },
            }

        if not definition_type:
            return {
                "action": "domain_definition_crud",
                "success": False,
                "message": "Could not resolve definition type. Specify account_definition, hr_department_definition, or hr_role_definition.",
                "resolved": {
                    "operation": operation,
                    "definition_type": definition_type,
                },
            }

        if operation == "list":
            with self.get_connection() as connection:
                result = domain_db.list_domain_definitions(
                    connection=connection,
                    definition_type=definition_type,
                    filters=definition,
                    limit=int(payload.get("limit") or 200),
                    offset=int(payload.get("offset") or 0),
                    include_inactive=bool(payload.get("include_inactive", False)),
                )
            return {
                "action": "domain_definition_crud",
                **result,
                "resolved": {
                    "operation": operation,
                    "definition_type": definition_type,
                    "definition": definition,
                },
            }

        validation = domain_db.validate_domain_definition_payload(
            definition_type=definition_type,
            payload=definition,
            operation=operation,
        )
        if not validation.get("valid"):
            return {
                "action": "domain_definition_crud",
                "success": False,
                "message": "Validation failed for domain definition payload.",
                "validation": validation,
                "expected_fields": self._expected_fields_for_definition(definition_type, operation),
                "resolved": {
                    "operation": operation,
                    "definition_type": definition_type,
                    "definition": definition,
                    "llm_payload": llm_payload,
                },
            }

        with self.get_connection() as connection:
            if operation == "create":
                result = domain_db.create_domain_definition(connection, definition_type, definition)
            elif operation == "update":
                result = domain_db.update_domain_definition(connection, definition_type, definition)
            elif operation == "delete":
                result = domain_db.delete_domain_definition(connection, definition_type, definition)
            else:
                result = domain_db.get_domain_definition(connection, definition_type, definition)

        return {
            "action": "domain_definition_crud",
            **result,
            "resolved": {
                "operation": operation,
                "definition_type": definition_type,
                "definition": definition,
            },
        }

    def _log_genai_json_response(self, call_name: str, model: str, payload: Any) -> None:
        try:
            payload_text = json.dumps(payload, ensure_ascii=False)
        except Exception:
            payload_text = str(payload)
        self.logger.info(
            "genai_json_response call=%s model=%s payload=%s",
            call_name,
            model,
            payload_text,
        )

    def _planner_step(self, question: str, state: dict[str, Any], step_index: int) -> dict[str, Any]:
        workflow_hint = self._as_string_key_dict(
            self._invoke_solf_clause_raw(
                "business_rule_workflow_execute",
                [{"process": "query_planning", "has_scope": bool(state.get("scope")), "candidates": state.get("scope") or {}}],
            )
        )
        control_flow = str(workflow_hint.get("control_flow") or "").strip().lower()
        if control_flow == "selection":
            selection_plan = self._as_string_key_dict(
                self._invoke_solf_clause_raw(
                    "business_rule_selection_plan",
                    [{"process": "query_planning", "has_scope": bool(state.get("scope"))}],
                )
            )
            next_action = str(selection_plan.get("next_action") or "").strip().lower()
            if next_action in {"resolve_scope", "lookup_attribute", "search_discovery", "search_criteria", "finalize"}:
                return {
                    "action": next_action,
                    "attribute_name": state.get("parsed", {}).get("attribute_name"),
                    "entity_name": state.get("parsed", {}).get("entity_name"),
                    "rationale": "Business-rule selection workflow routing.",
                }

        if control_flow == "sequence" and step_index > 0 and state.get("attribute_result"):
            return {
                "action": "finalize",
                "attribute_name": state.get("parsed", {}).get("attribute_name"),
                "entity_name": state.get("parsed", {}).get("entity_name"),
                "rationale": "Business-rule sequence workflow finalized after grounded attribute result.",
            }

        if control_flow == "iteration" and step_index == 0:
            return {
                "action": "search_criteria",
                "attribute_name": state.get("parsed", {}).get("attribute_name"),
                "entity_name": state.get("parsed", {}).get("entity_name"),
                "rationale": "Business-rule iteration workflow starts with criteria scan.",
            }

        if control_flow == "backtracking" and step_index == 0 and not state.get("scope"):
            return {
                "action": "resolve_scope",
                "attribute_name": state.get("parsed", {}).get("attribute_name"),
                "entity_name": state.get("parsed", {}).get("entity_name"),
                "rationale": "Business-rule backtracking workflow resolves broad scope first.",
            }

        style_data = state.get("query_style") if isinstance(state.get("query_style"), dict) else {}
        preferred_action = str(style_data.get("initial_action") or "").strip().lower()
        if step_index == 0 and preferred_action in {
            "resolve_scope",
            "search_discovery",
            "lookup_attribute",
            "search_criteria",
            "finalize",
        }:
            return {
                "action": preferred_action,
                "attribute_name": state.get("parsed", {}).get("attribute_name"),
                "entity_name": state.get("parsed", {}).get("entity_name"),
                "rationale": "Style-routed initial action.",
            }

        # Proactively route media-oriented questions to Discovery first.
        parsed_payload = state.get("parsed") if isinstance(state.get("parsed"), dict) else {}
        parsed_confidence = parsed_payload.get("confidence")
        media_hint = self._is_media_intent(question, confidence=parsed_confidence)
        if step_index == 0 and media_hint.get("media_intent"):
            return {
                "action": "search_discovery",
                "attribute_name": state.get("parsed", {}).get("attribute_name"),
                "entity_name": state.get("parsed", {}).get("entity_name"),
                "rationale": str(media_hint.get("rationale") or "Question appears media-oriented, so use Discovery search first."),
            }

        prompt = (
            "You are a retrieval planner for IDMS.\n"
            "Plan exactly one next action for a tool-using loop.\n"
            "Return JSON only with keys: action, attribute_name, entity_name, rationale.\n"
            "Allowed action values: resolve_scope, search_discovery, lookup_attribute, search_criteria, finalize.\n"
            "Rules:\n"
            "- Use resolve_scope first when scope is missing.\n"
            "- Use search_discovery when media-oriented or Discovery-specific results should be inspected directly.\n"
            "- Use lookup_attribute when a concrete attribute should be searched.\n"
            "- Use search_criteria when the query asks for list/filter matches.\n"
            "- Use finalize once enough grounded evidence exists.\n\n"
            f"Question: {question}\n"
            f"Step index: {step_index}\n"
            f"State JSON: {json.dumps(state, ensure_ascii=False)}"
        )
        selected_model = self._select_model_by_confidence(parsed_confidence, default=EXTRACT_MODEL)
        self._record_model_usage("interaction_planner_step", selected_model, parsed_confidence)
        response = generate_content_with_openrouter_fallback(
            primary_call=lambda: self.client.models.generate_content(
                model=selected_model,
                contents=[prompt],
            ),
            model=selected_model,
            contents=[prompt],
            temperature=0.0,
            call_name="interaction_planner_step",
            complexity="simple",
        )
        self.logger.info("GenAI planner call completed for step=%s", step_index)
        decision = self._extract_json_object(response.text or "")
        self._log_genai_json_response("planner", selected_model, decision)
        action = str(decision.get("action") or "").strip().lower()
        if action not in {"resolve_scope", "search_discovery", "lookup_attribute", "search_criteria", "finalize"}:
            if not state.get("scope"):
                action = "resolve_scope"
            elif self._scope_is_empty(state.get("scope")):
                action = "finalize"
            elif state.get("parsed", {}).get("intent") == "criteria_lookup" and not state.get("criteria_result"):
                action = "search_criteria"
            elif not state.get("attribute_result"):
                action = "lookup_attribute"
            else:
                action = "finalize"
        decision["action"] = action
        return decision

    def _classify_query_style(self, question: str, parsed: dict[str, Any]) -> dict[str, Any]:
        intent = str(parsed.get("intent") or "").strip().lower()
        attribute_name = str(parsed.get("attribute_name") or "").strip().lower()
        question_lower = str(question or "").strip().lower()
        criteria = parsed.get("criteria") if isinstance(parsed.get("criteria"), dict) else {}
        has_time_window = isinstance(criteria.get("time_window"), dict) and bool(criteria["time_window"])

        if intent == "criteria_lookup" and has_time_window:
            style_hint = "temporal_filtered_criteria"
            initial_action = "search_criteria"
        elif intent == "criteria_lookup":
            style_hint = "criteria_list"
            initial_action = "search_criteria"
        elif attribute_name in {"document_filename", "document_full_path", "document_file_info"} or "document" in question_lower:
            style_hint = "document_retrieval"
            initial_action = "lookup_attribute"
        elif intent == "attribute_lookup":
            style_hint = "attribute_value"
            initial_action = "lookup_attribute"
        else:
            style_hint = "semantic_description"
            initial_action = "resolve_scope"

        payload = {
            "question": question,
            "parsed": parsed,
            "style_hint": style_hint,
            "initial_action": initial_action,
        }
        result = self._invoke_solf_clause_raw("query_style_policy", [payload])
        style_result = self._as_string_key_dict(result)
        mode = str(style_result.get("mode") or "allow").strip().lower()
        if mode not in {"allow", "deny", "escalate"}:
            mode = "allow"

        return {
            "mode": mode,
            "reason": str(style_result.get("reason") or "default_query_style_policy"),
            "query_style": str(style_result.get("query_style") or style_hint),
            "initial_action": str(style_result.get("initial_action") or initial_action),
            "message": str(style_result.get("message") or ""),
            "policy_clause": "query_style_policy",
        }

    def _is_media_intent(self, question: str, confidence: Any = None) -> dict[str, Any]:
        lowered = str(question or "").strip().lower()
        media_tokens = {
            "image",
            "photo",
            "picture",
            "audio",
            "voice",
            "recording",
            "video",
            "clip",
            "transcript",
            "scene",
            "frame",
        }
        token_match = any(token in lowered for token in media_tokens)

        # If keyword match is clear, return immediately without LLM (saves embedding cost).
        if token_match:
            return {
                "media_intent": True,
                "rationale": "Detected media-oriented intent (keyword-based) for Discovery-first planning.",
            }

        # For ambiguous cases, use LLM only when keywords don't provide clear signal.
        prompt = (
            "Classify whether the question is primarily asking about media content "
            "(image/audio/video/transcript/scene/frame) rather than structured attributes.\n"
            "Return JSON only with keys: media_intent, rationale.\n"
            f"Question: {question}"
        )
        try:
            self.logger.info("GenAI media intent classification requested")
            selected_model = self._select_model_by_confidence(confidence, default=EXTRACT_MODEL)
            self._record_model_usage("interaction_media_intent", selected_model, confidence)
            response = generate_content_with_openrouter_fallback(
                primary_call=lambda: self.client.models.generate_content(
                    model=selected_model,
                    contents=[prompt],
                ),
                model=selected_model,
                contents=[prompt],
                temperature=0.0,
                call_name="interaction_media_intent",
                complexity="simple",
            )
            parsed = self._extract_json_object(response.text or "")
            self._log_genai_json_response("media_intent", selected_model, parsed)
            llm_value = bool(parsed.get("media_intent"))
            rationale = str(parsed.get("rationale") or "").strip()
            return {
                "media_intent": llm_value,
                "rationale": rationale or "LLM-classified media intent for Discovery-first planning.",
            }
        except Exception:
            return {
                "media_intent": False,
                "rationale": "Ambiguous intent; proceeding with default routing.",
            }

    def _answer_sources(self, grounding: dict[str, Any]) -> list[str]:
        attribute_result = grounding.get("attribute_result") if isinstance(grounding, dict) else None
        criteria_result = grounding.get("criteria_result") if isinstance(grounding, dict) else None
        criteria_contextual_result = grounding.get("criteria_contextual_result") if isinstance(grounding, dict) else None
        scope = grounding.get("scope") if isinstance(grounding, dict) else None
        discovery_search = grounding.get("discovery_search") if isinstance(grounding, dict) else None
        ordered_sources: list[str] = []

        def _append_source(value: str) -> None:
            if value not in ordered_sources:
                ordered_sources.append(value)

        if isinstance(attribute_result, dict):
            source = str(attribute_result.get("source") or "").strip().lower()
            if source == "discovery":
                _append_source("discovery")
            if attribute_result.get("doc_id") is not None:
                _append_source("sql")
            if attribute_result.get("node_id") is not None:
                _append_source("qdrant_sql")

        if isinstance(criteria_result, dict) and int(criteria_result.get("count", 0) or 0) > 0:
            _append_source("criteria_sql")

        if isinstance(criteria_contextual_result, dict) and str(criteria_contextual_result.get("answer") or "").strip():
            _append_source("criteria_contextual_llm")

        if isinstance(discovery_search, dict) and int(discovery_search.get("candidate_count", 0) or 0) > 0:
            _append_source("discovery")

        if isinstance(scope, dict) and int(scope.get("candidate_count", 0) or 0) > 0:
            _append_source("qdrant")

        if isinstance(scope, dict) and int(scope.get("discovery_candidate_count", 0) or 0) > 0:
            _append_source("discovery")

        if not ordered_sources:
            ordered_sources.append("not_found")

        return ordered_sources

    def _deterministic_criteria_answer(self, question: str, grounding: dict[str, Any]) -> str | None:
        """Build a deterministic list-style answer for criteria matches.

        This avoids count-only LLM summaries for explicit document/note listing asks.
        """
        if not isinstance(grounding, dict):
            return None

        criteria_result = grounding.get("criteria_result")
        if not isinstance(criteria_result, dict):
            return None
        matches = list(criteria_result.get("matches") or [])
        if not matches:
            return None

        parsed = grounding.get("parsed") if isinstance(grounding.get("parsed"), dict) else {}
        parsed_criteria = parsed.get("criteria") if isinstance(parsed.get("criteria"), dict) else {}
        response_format = str(parsed_criteria.get("response_format") or "").strip().lower()

        lines: list[str] = []
        for item in matches[:8]:
            if not isinstance(item, dict):
                continue
            label = str(
                item.get("entity_name")
                or item.get("doc_name")
                or item.get("title")
                or item.get("object_name")
                or ""
            ).strip()
            if not label:
                continue

            theme = str(item.get("entity_type") or item.get("theme") or "").strip()
            doc_name = str(item.get("doc_name") or "").strip()
            doc_path = str(item.get("doc_path") or "").strip()
            description = str(item.get("description") or item.get("doc_desc") or "").strip()
            matched_terms = item.get("matched_terms") if isinstance(item.get("matched_terms"), list) else []

            extras: list[str] = []
            if response_format == "doc_titles_and_files":
                if doc_name and doc_name.lower() != label.lower():
                    extras.append(f"file: {doc_name}")
                elif doc_name:
                    extras.append(f"file: {doc_name}")
                if doc_path:
                    extras.append(f"path: {doc_path}")
            else:
                if theme:
                    extras.append(theme)
                if doc_name and doc_name.lower() != label.lower():
                    extras.append(f"file: {doc_name}")
                if description:
                    extras.append(description[:180])
                elif matched_terms:
                    extras.append("matched: " + ", ".join(str(x) for x in matched_terms[:4]))

            if extras:
                lines.append(f"- {label} ({'; '.join(extras)})")
            else:
                lines.append(f"- {label}")

        if not lines:
            return None

        prompt_lower = str(question or "").lower()
        if "note" in prompt_lower or "notiz" in prompt_lower:
            intro = "Here are the matching notes:"
        else:
            intro = "Here are the matching documents:"

        return intro + "\n" + "\n".join(lines)

    def _build_query_evidence_bundle(self, question: str, query_result: dict[str, Any] | None) -> dict[str, Any]:
        parsed = self.query_engine.parse_query(question)
        parsed_data = {
            "intent": getattr(parsed, "intent", None),
            "entity_name": getattr(parsed, "entity_name", None),
            "attribute_name": getattr(parsed, "attribute_name", None),
            "relation_name": getattr(parsed, "relation_name", None),
            "confidence": getattr(parsed, "confidence", None),
            "language": getattr(parsed, "language", None),
            "criteria": getattr(parsed, "criteria", None) if isinstance(getattr(parsed, "criteria", None), dict) else {},
        }

        result_data = query_result.get("data") if isinstance(query_result, dict) and isinstance(query_result.get("data"), dict) else {}
        answer_sources: list[str] = []
        if isinstance(query_result, dict) and isinstance(query_result.get("answer_sources"), list):
            answer_sources = [str(item).strip() for item in query_result.get("answer_sources") if str(item).strip()]
        elif isinstance(query_result, dict):
            answer_sources = self._answer_sources(query_result.get("grounding") if isinstance(query_result.get("grounding"), dict) else {})

        provenance_snapshot = self._extract_provenance_snapshot(query_result)
        try:
            scope = self.query_engine.resolve_search_scope(question)
        except Exception:
            scope = {}

        entities: list[str] = []
        attributes: list[str] = []
        relationships: list[str] = []

        def _append_unique(target: list[str], value: Any) -> None:
            text = str(value or "").strip()
            if not text or text in target:
                return
            target.append(text)

        _append_unique(entities, parsed_data.get("entity_name"))
        _append_unique(attributes, parsed_data.get("attribute_name"))
        _append_unique(relationships, parsed_data.get("relation_name"))

        if isinstance(result_data, dict):
            _append_unique(entities, result_data.get("entity_name"))
            _append_unique(entities, result_data.get("source_entity_name"))
            _append_unique(entities, result_data.get("related_entity_name"))
            _append_unique(attributes, result_data.get("attribute_name"))
            _append_unique(attributes, result_data.get("attribute_key"))
            _append_unique(relationships, result_data.get("relationship_name"))

        if isinstance(scope, dict):
            for item in list(scope.get("objects") or []):
                if isinstance(item, dict):
                    _append_unique(entities, item.get("object_name") or item.get("entity_name"))
            for item in list(scope.get("documents") or []):
                if isinstance(item, dict):
                    _append_unique(entities, item.get("doc_name") or item.get("doc_key"))
            for item in list(scope.get("relationships") or []):
                if isinstance(item, dict):
                    _append_unique(relationships, item.get("relationship_name"))

        for item in provenance_snapshot:
            if not isinstance(item, dict):
                continue
            _append_unique(entities, item.get("entity_name"))
            _append_unique(entities, item.get("related_entity_name"))
            _append_unique(attributes, item.get("attribute_name"))
            _append_unique(relationships, item.get("relationship_name"))

        return {
            "parsed": parsed_data,
            "route": {
                "source": str(query_result.get("source") or "") if isinstance(query_result, dict) else "",
                "answer_source": str(query_result.get("answer_source") or "") if isinstance(query_result, dict) else "",
                "answer_sources": answer_sources,
                "candidate_count": int(query_result.get("candidate_count", 0) or 0) if isinstance(query_result, dict) else 0,
            },
            "evidence": {
                "primary_data": result_data,
                "provenance_snapshot": provenance_snapshot,
                "search_scope": scope,
                "entities": entities,
                "attributes": attributes,
                "relationships": relationships,
            },
        }

    def _scope_is_empty(self, scope: Any) -> bool:
        if not isinstance(scope, dict):
            return False
        return (
            int(scope.get("candidate_count", 0) or 0) == 0
            and int(scope.get("discovery_candidate_count", 0) or 0) == 0
            and not list(scope.get("node_ids") or [])
            and not list(scope.get("edge_ids") or [])
            and not list(scope.get("doc_ids") or [])
        )

    def search_discovery(self, question: str, limit: int = 8) -> dict[str, Any]:
        """Tool: Search Discovery Engine directly for media-heavy results."""
        return self.query_engine.search_discovery(question, limit=limit)

    def search_criteria(self, question: str, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        """Tool: Execute list/filter criteria queries against structured records."""
        parsed = self.query_engine.parse_query(question)
        criteria_result = self.query_engine.search_by_criteria(
            question=question,
            parsed=parsed,
            scope=scope,
            limit=20,
        )

        if (
            not criteria_result
            and str(getattr(parsed, "intent", "") or "").strip().lower() == "attribute_lookup"
            and self.query_engine._is_value_document_context_question(question)
        ):
            parsed_criteria = parsed.criteria if isinstance(parsed.criteria, dict) else {}
            relation_filters: list[dict[str, Any]] = []
            if isinstance(parsed_criteria.get("relation_filters"), list):
                relation_filters.extend(item for item in parsed_criteria.get("relation_filters") if isinstance(item, dict))
            if isinstance(parsed_criteria.get("relationship_filters"), list):
                relation_filters.extend(item for item in parsed_criteria.get("relationship_filters") if isinstance(item, dict))

            must_terms: list[str] = []
            entity_name = str(getattr(parsed, "entity_name", "") or "").strip()
            if entity_name:
                must_terms.append(entity_name)

            for rel in relation_filters:
                for key in (
                    "target_name",
                    "target_entity_name",
                    "source_name",
                    "source_entity_name",
                    "relationship_type",
                    "relation",
                ):
                    value = str(rel.get(key) or "").strip()
                    if value and value not in must_terms:
                        must_terms.append(value)

            cue_tokens = [
                tok
                for tok in re.findall(r"[a-zA-Z0-9äöüÄÖÜß]{4,}", str(question or "").lower())
                if tok
                not in {
                    "what", "which", "where", "when", "how", "much", "with", "from", "into", "about",
                    "cost", "price", "value", "amount", "total", "tell", "find", "related", "documents",
                    "show", "give", "query", "that", "this", "have", "does", "did", "were", "been",
                    "was", "sind", "ist", "sowie", "oder", "aber", "eine", "einer", "eines", "dieser",
                }
            ]
            for token in cue_tokens[:8]:
                if token and token not in must_terms:
                    must_terms.append(token)

            for hint in ("invoice", "bill", "receipt", "payment", "quotation", "quote", "offer", "rechnung", "beleg", "zahlung"):
                if hint not in must_terms:
                    must_terms.append(hint)

            if must_terms:
                synthetic_criteria = {
                    "entity_type": "",
                    "must_contain": must_terms,
                    "attribute_hints": ["description", "document", "metadata", "amount", "cost", "value", "due_date"],
                    "semantic_only": True,
                    "document_hint": "all",
                    "semantic_frame": "interaction_value_document_context_bridge",
                    "semantic_retrieval_mode": "related",
                    "allow_indirect_relationships": True,
                }
                synthetic_parsed = parsed.__class__(
                    intent="criteria_lookup",
                    entity_name=entity_name or None,
                    attribute_name=None,
                    confidence=max(0.70, float(getattr(parsed, "confidence", 0.0) or 0.0)),
                    language=str(getattr(parsed, "language", "en") or "en"),
                    criteria=synthetic_criteria,
                )
                criteria_result = self.query_engine.search_by_criteria(
                    question=question,
                    parsed=synthetic_parsed,
                    scope=scope,
                    limit=20,
                )

        if not criteria_result:
            return {
                "success": False,
                "count": 0,
                "matches": [],
                "criteria": parsed.criteria if isinstance(parsed.criteria, dict) else {},
            }
        return {
            "success": True,
            **criteria_result,
        }

    def get_entity_attributes(self, entity_name: str) -> dict[str, Any]:
        """Tool: Return the attribute names actually stored in the database for a given entity.

        Use this before lookup_entity_attribute when the user's query does not clearly
        specify which attribute they want. The returned list provides grounded options
        for the agent to choose from.

        Args:
            entity_name: The company or person name exactly as it appears in the query.

        Returns:
            Dict with entity_name and a list of available_attributes (snake_case keys).
        """
        entity_name = str(entity_name or "").strip()
        if not entity_name:
            return {"entity_name": entity_name, "available_attributes": [], "count": 0}
        attrs = self.query_engine._get_entity_attribute_names(entity_name)
        return {
            "entity_name": entity_name,
            "available_attributes": attrs,
            "count": len(attrs),
        }

    def lookup_entity_attribute(
        self, entity_name: str, attribute_name: str
    ) -> dict[str, Any]:
        """Tool: Look up the value of a specific attribute for an entity using exact SQL.

        Call this after get_entity_attributes to retrieve the actual stored value.
        For shareholders, use attribute_name='shareholders'.

        Args:
            entity_name: The company or person name.
            attribute_name: The snake_case attribute key (e.g. 'registered_address',
                'registration_no', 'incorporation_date').

        Returns:
            Dict with entity_name, attribute_name, attribute_value, and source metadata.
        """
        entity_name = str(entity_name or "").strip()
        attribute_name = str(attribute_name or "").strip()
        if not entity_name or not attribute_name:
            return {"found": False, "entity_name": entity_name, "attribute_name": attribute_name}
        result = self.query_engine.lookup_attribute(attribute_name, entity_name)
        if not result:
            return {"found": False, "entity_name": entity_name, "attribute_name": attribute_name}
        return {"found": True, **result}

    def retrieve_document_file(
        self,
        doc_id: int | None = None,
        doc_key: str | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Tool: Retrieve document filename and path via SOLF-governed action flow.

        Resolution priority follows provided identifiers. Returns doc_name, doc_path,
        and original_local_path (when available) for downstream file retrieval.
        """
        payload: dict[str, Any] = {}
        if doc_id is not None:
            payload["doc_id"] = int(doc_id)
        if doc_key:
            payload["doc_key"] = str(doc_key).strip()
        if doc_name:
            payload["doc_name"] = str(doc_name).strip()
        result = self.perform_action("retrieve_document_file", payload)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        row = rows[0] if rows else {}
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        user_metadata = metadata.get("user_metadata") if isinstance(metadata.get("user_metadata"), dict) else {}
        source_ref = user_metadata.get("source_reference") if isinstance(user_metadata.get("source_reference"), dict) else {}
        return {
            "success": bool(result.get("success")),
            "doc_id": row.get("doc_id"),
            "doc_name": row.get("doc_name"),
            "doc_key": row.get("doc_key"),
            "doc_path": row.get("doc_path"),
            "original_local_path": source_ref.get("original_local_path") or user_metadata.get("client_file_path"),
            "file_name": row.get("doc_name") or user_metadata.get("client_file_name") or row.get("doc_key"),
            "raw": result,
        }

    def perform_action(self, action_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute SOLF-guarded workflow action using ActionTool."""
        action = str(action_name or "").strip().lower()
        payload = payload if isinstance(payload, dict) else {}

        def _respond(out: dict[str, Any]) -> dict[str, Any]:
            self._log_action_execution(action, payload, out)
            return out

        # Compose and enforce SOLF semantic plan first.
        semantic_plan = self._compose_action_plan(action, payload)
        if semantic_plan:
            guard_clause = str(semantic_plan.get("guard") or "").strip()
            if guard_clause:
                guard_result = self._invoke_solf_policy(guard_clause, payload)
                if str(guard_result.get("mode") or "allow").lower() == "deny":
                    return _respond({
                        "action": action,
                        "success": False,
                        "message": str(guard_result.get("message") or f"SOLF guard denied action: {guard_clause}"),
                        "policy": guard_result,
                        "semantic_plan": semantic_plan,
                        "trace": {
                            "action_clause": "action_plan",
                            "guard_clause": guard_clause,
                            "sql_clause": str(semantic_plan.get("sql_builder") or "") or None,
                        },
                    })

            sql_builder = str(semantic_plan.get("sql_builder") or "").strip()
            raw_plan_args = self._as_string_key_dict(semantic_plan.get("args")) or payload
            plan_args = self._as_string_key_dict(self._resolve_solf_placeholders(raw_plan_args, payload))
            if sql_builder:
                sql_plan_raw = self._invoke_solf_clause_raw(sql_builder, [plan_args])
                sql_plan = self._as_string_key_dict(self._resolve_solf_placeholders(sql_plan_raw, plan_args))
                if sql_plan:
                    if action == "retrieve_document_file":
                        where_payload: dict[str, Any] = {}
                        if payload.get("doc_id") is not None:
                            where_payload["doc_id"] = int(payload.get("doc_id"))
                        elif payload.get("doc_key"):
                            where_payload["doc_key"] = str(payload.get("doc_key")).strip()
                        elif payload.get("doc_name"):
                            where_payload["doc_name"] = str(payload.get("doc_name")).strip()
                        else:
                            return _respond({
                                "action": action,
                                "success": False,
                                "message": "One of doc_id, doc_key, or doc_name is required.",
                                "semantic_plan": semantic_plan,
                                "sql_plan": sql_plan,
                            })
                        sql_plan["operation"] = "select"
                        sql_plan["table"] = "document"
                        sql_plan["where"] = where_payload

                    # validate_plan has multi-check semantics already implemented in ActionTool.
                    if action == "validate_plan":
                        result = self.action_tool.validate_plan(plan_id=int(payload.get("plan_id")))
                        out = {
                            "action": action,
                            "success": result.success,
                            "message": result.message,
                            "data": result.data,
                            "warnings": result.warnings,
                            "requires_confirmation": result.requires_confirmation,
                            "confirmation_prompt": result.confirmation_prompt,
                            "semantic_plan": semantic_plan,
                            "sql_plan": sql_plan,
                            "trace": {
                                "action_clause": "action_plan",
                                "guard_clause": guard_clause or None,
                                "sql_clause": sql_builder or None,
                            },
                        }
                        if isinstance(result.data, dict):
                            for conflict in (result.data.get("resource_conflicts") or []):
                                self.event_bus.emit(
                                    "resource.conflict_detected",
                                    source_entity="project_tasks",
                                    entity_id=int(payload.get("plan_id")),
                                    payload={
                                        "person_name": conflict.get("assigned_to"),
                                        "conflicting_tasks": conflict,
                                    },
                                    tags=["validation"],
                                )
                            for overdue in (result.data.get("overdue_tasks") or []):
                                self.event_bus.emit(
                                    "task.overdue",
                                    source_entity="project_tasks",
                                    entity_id=int(overdue.get("task_id")),
                                    payload={
                                        "title": overdue.get("name"),
                                        "days_overdue": 1,
                                    },
                                    tags=["validation"],
                                )
                        return _respond(out)

                    sql_exec = self._execute_sql_plan(sql_plan)
                    if sql_exec.get("success"):
                        if action == "create_task":
                            task_id = None
                            returned = self._as_string_key_dict(sql_exec.get("returned"))
                            if returned.get("id") is not None:
                                task_id = int(returned.get("id"))
                            self.event_bus.emit(
                                "task.created",
                                source_entity="project_tasks",
                                entity_id=int(task_id or 0),
                                payload={
                                    "project_id": payload.get("project_id"),
                                    "assigned_to": payload.get("assigned_to"),
                                    "title": payload.get("title"),
                                },
                                tags=["action"],
                            )
                        elif action == "assign_task":
                            self.event_bus.emit(
                                "task.assigned",
                                source_entity="project_tasks",
                                entity_id=int(payload.get("task_id")),
                                payload={"assigned_to": payload.get("assigned_to")},
                                tags=["action"],
                            )
                        elif action == "close_task":
                            self.event_bus.emit(
                                "task.closed",
                                source_entity="project_tasks",
                                entity_id=int(payload.get("task_id")),
                                payload={"status": "done"},
                                tags=["action"],
                            )
                        elif action == "create_workflow_case":
                            case_id = None
                            returned = self._as_string_key_dict(sql_exec.get("returned"))
                            if returned.get("id") is not None:
                                case_id = int(returned.get("id"))
                            self.event_bus.emit(
                                "workflow.case_created",
                                source_entity="workflow_cases",
                                entity_id=int(case_id or 0),
                                payload={
                                    "case_no": payload.get("case_no"),
                                    "case_type": payload.get("case_type"),
                                    "status": payload.get("status"),
                                },
                                tags=["action", "workflow"],
                            )
                        elif action == "record_approval":
                            approval_id = None
                            returned = self._as_string_key_dict(sql_exec.get("returned"))
                            if returned.get("id") is not None:
                                approval_id = int(returned.get("id"))
                            self.event_bus.emit(
                                "workflow.approval_recorded",
                                source_entity="approvals",
                                entity_id=int(approval_id or 0),
                                payload={
                                    "case_ref": payload.get("case_ref"),
                                    "decision": payload.get("decision"),
                                    "approver_ref": payload.get("approver_ref"),
                                },
                                tags=["action", "approval"],
                            )
                        elif action == "create_compliance_action":
                            compliance_id = None
                            returned = self._as_string_key_dict(sql_exec.get("returned"))
                            if returned.get("id") is not None:
                                compliance_id = int(returned.get("id"))
                            self.event_bus.emit(
                                "compliance.action_created",
                                source_entity="compliance_actions",
                                entity_id=int(compliance_id or 0),
                                payload={
                                    "action_no": payload.get("action_no"),
                                    "status": payload.get("status"),
                                    "priority": payload.get("priority"),
                                },
                                tags=["action", "compliance"],
                            )

                    return _respond({
                        "action": action,
                        "success": bool(sql_exec.get("success")),
                        "message": "Action executed via SOLF SQL plan." if sql_exec.get("success") else str(sql_exec.get("message") or "Action failed."),
                        "data": sql_exec,
                        "semantic_plan": semantic_plan,
                        "sql_plan": sql_plan,
                        "trace": {
                            "action_clause": "action_plan",
                            "guard_clause": guard_clause or None,
                            "sql_clause": sql_builder or None,
                        },
                    })

        if action in {"domain_definition_crud", "domain-definition-crud", "domain_definition", "domain-definition"}:
            out = self._handle_domain_definition_crud_action(payload)
            if not isinstance(out, dict):
                out = {
                    "action": "domain_definition_crud",
                    "success": False,
                    "message": "Unexpected response from domain definition CRUD handler.",
                }
            if not out.get("action"):
                out["action"] = "domain_definition_crud"
            return _respond(out)

        if action == "create_task":
            result = self.action_tool.create_task(
                project_id=int(payload.get("project_id")),
                title=str(payload.get("title") or "").strip(),
                hours_estimate=float(payload.get("hours_estimate") or 0),
                assigned_to=(int(payload["assigned_to"]) if payload.get("assigned_to") is not None else None),
                description=str(payload.get("description") or ""),
                priority=str(payload.get("priority") or "medium"),
                due_date=(str(payload.get("due_date")) if payload.get("due_date") else None),
            )
            if result.success and isinstance(result.data, dict):
                self.event_bus.emit(
                    "task.created",
                    source_entity="project_tasks",
                    entity_id=int(result.data.get("task_id") or 0),
                    payload={
                        "project_id": int(payload.get("project_id")),
                        "assigned_to": payload.get("assigned_to"),
                        "title": payload.get("title"),
                    },
                    tags=["action"],
                )
            return _respond({
                "action": action,
                "success": result.success,
                "message": result.message,
                "data": result.data,
                "warnings": result.warnings,
                "requires_confirmation": result.requires_confirmation,
                "confirmation_prompt": result.confirmation_prompt,
            })

        if action == "ingest_document":
            source = str(payload.get("source") or "").strip()
            if not source:
                return _respond({
                    "action": action,
                    "success": False,
                    "message": "source is required",
                })
            raw_tags = payload.get("tags")
            if isinstance(raw_tags, list):
                tags = [str(item).strip() for item in raw_tags if str(item).strip()]
            elif isinstance(raw_tags, str):
                tags = [item.strip() for item in raw_tags.split(",") if item.strip()]
            else:
                tags = []
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            result = self.ingest_document(
                source=source,
                description=str(payload.get("description") or ""),
                tags=tags,
                metadata=metadata,
            )
            self.event_bus.emit(
                "document.ingested",
                source_entity="document",
                entity_id=0,
                payload={
                    "source": source,
                    "description": str(payload.get("description") or ""),
                    "tags": tags,
                },
                tags=["ingestion"],
            )
            return _respond({
                "action": action,
                "success": True,
                "message": "Document ingested.",
                "data": result,
            })

        if action == "ingest_information":
            information_text = str(payload.get("information_text") or payload.get("text") or "").strip()
            if not information_text:
                return _respond({
                    "action": action,
                    "success": False,
                    "message": "information_text is required",
                })
            raw_tags = payload.get("tags")
            if isinstance(raw_tags, list):
                tags = [str(item).strip() for item in raw_tags if str(item).strip()]
            elif isinstance(raw_tags, str):
                tags = [item.strip() for item in raw_tags.split(",") if item.strip()]
            else:
                tags = []
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            result = self.ingest_information(
                information_text=information_text,
                description=str(payload.get("description") or ""),
                tags=tags,
                metadata=metadata,
            )
            self.event_bus.emit(
                "information.ingested",
                source_entity="document",
                entity_id=0,
                payload={
                    "description": str(payload.get("description") or ""),
                    "tags": tags,
                    "text_length": len(information_text),
                },
                tags=["ingestion"],
            )
            return _respond({
                "action": action,
                "success": True,
                "message": "Information text ingested.",
                "data": result,
            })

        if action == "generate_document":
            result = self.generate_document(
                document_type=str(payload.get("document_type") or ""),
                subject=str(payload.get("subject") or ""),
                context=(payload.get("context") if isinstance(payload.get("context"), dict) else {}),
                template=str(payload.get("template") or ""),
                output_path=(str(payload.get("output_path")).strip() if payload.get("output_path") else None),
            )
            self.event_bus.emit(
                "document.generated",
                source_entity="document",
                entity_id=0,
                payload={
                    "document_type": result.get("document_type"),
                    "subject": result.get("subject"),
                    "output_path": result.get("output_path"),
                },
                tags=["generation"],
            )
            return _respond({
                "action": action,
                "success": True,
                "message": "Document generated.",
                "data": result,
            })

        if action == "update_entity":
            entity_type = str(payload.get("entity_type") or "").strip()
            field = str(payload.get("field") or "").strip()
            new_value = payload.get("new_value")
            entity_id_raw = payload.get("entity_id")
            natural_key_field = str(payload.get("natural_key_field") or "").strip()
            natural_key_value = payload.get("natural_key_value")

            if (not entity_id_raw) and entity_type and field and natural_key_field and (natural_key_value is not None):
                sql_plan = {
                    "operation": "update",
                    "table": entity_type,
                    "where": {natural_key_field: natural_key_value},
                    "values": {field: new_value},
                    "returning": ["id"],
                }
                sql_exec = self._execute_sql_plan(sql_plan)
                return _respond({
                    "action": action,
                    "success": bool(sql_exec.get("success")),
                    "message": "Entity updated by natural key." if sql_exec.get("success") else str(sql_exec.get("message") or "Update failed."),
                    "data": sql_exec,
                    "sql_plan": sql_plan,
                })

            result = self.action_tool.update_entity(
                entity_type=entity_type,
                entity_id=int(payload.get("entity_id")),
                field=field,
                new_value=new_value,
            )
            return _respond({
                "action": action,
                "success": result.success,
                "message": result.message,
                "data": result.data,
                "warnings": result.warnings,
                "requires_confirmation": result.requires_confirmation,
                "confirmation_prompt": result.confirmation_prompt,
            })

        if action == "assign_task":
            result = self.action_tool.assign_task(
                task_id=int(payload.get("task_id")),
                assigned_to=int(payload.get("assigned_to")),
            )
            if result.success:
                self.event_bus.emit(
                    "task.assigned",
                    source_entity="project_tasks",
                    entity_id=int(payload.get("task_id")),
                    payload={"assigned_to": int(payload.get("assigned_to"))},
                    tags=["action"],
                )
            return _respond({
                "action": action,
                "success": result.success,
                "message": result.message,
                "data": result.data,
                "warnings": result.warnings,
                "requires_confirmation": result.requires_confirmation,
                "confirmation_prompt": result.confirmation_prompt,
            })

        if action == "close_task":
            result = self.action_tool.close_task(task_id=int(payload.get("task_id")))
            if result.success:
                self.event_bus.emit(
                    "task.closed",
                    source_entity="project_tasks",
                    entity_id=int(payload.get("task_id")),
                    payload={"status": "done"},
                    tags=["action"],
                )
            return _respond({
                "action": action,
                "success": result.success,
                "message": result.message,
                "data": result.data,
                "warnings": result.warnings,
                "requires_confirmation": result.requires_confirmation,
                "confirmation_prompt": result.confirmation_prompt,
            })

        if action == "validate_plan":
            result = self.action_tool.validate_plan(plan_id=int(payload.get("plan_id")))
            if isinstance(result.data, dict):
                for conflict in (result.data.get("resource_conflicts") or []):
                    self.event_bus.emit(
                        "resource.conflict_detected",
                        source_entity="project_tasks",
                        entity_id=int(payload.get("plan_id")),
                        payload={
                            "person_name": conflict.get("assigned_to"),
                            "conflicting_tasks": conflict,
                        },
                        tags=["validation"],
                    )
                for overdue in (result.data.get("overdue_tasks") or []):
                    self.event_bus.emit(
                        "task.overdue",
                        source_entity="project_tasks",
                        entity_id=int(overdue.get("task_id")),
                        payload={
                            "title": overdue.get("name"),
                            "days_overdue": 1,
                        },
                        tags=["validation"],
                    )
            return _respond({
                "action": action,
                "success": result.success,
                "message": result.message,
                "data": result.data,
                "warnings": result.warnings,
                "requires_confirmation": result.requires_confirmation,
                "confirmation_prompt": result.confirmation_prompt,
            })

        return _respond({
            "action": action,
            "success": False,
            "message": f"Unsupported action: {action}",
        })

    def preview_action_plan(self, action_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Dry-run SOLF composition: return semantic plan + SQL plan without execution."""
        action = str(action_name or "").strip().lower()
        payload = payload if isinstance(payload, dict) else {}
        semantic_plan = self._compose_action_plan(action, payload)
        if not semantic_plan:
            return {
                "action": action,
                "success": False,
                "message": "No semantic plan composed by SOLF.",
            }
        sql_builder = str(semantic_plan.get("sql_builder") or "").strip()
        raw_plan_args = self._as_string_key_dict(semantic_plan.get("args")) or payload
        plan_args = self._as_string_key_dict(self._resolve_solf_placeholders(raw_plan_args, payload))
        sql_plan = None
        if sql_builder:
            sql_plan_raw = self._invoke_solf_clause_raw(sql_builder, [plan_args])
            sql_plan = self._as_string_key_dict(self._resolve_solf_placeholders(sql_plan_raw, plan_args))
        return {
            "action": action,
            "success": True,
            "dry_run": True,
            "semantic_plan": semantic_plan,
            "sql_plan": sql_plan,
            "trace": {
                "action_clause": "action_plan",
                "sql_clause": sql_builder or None,
            },
        }

    def bootstrap_db(self, recreate: bool = False) -> dict[str, Any]:
        """Initialize DB tables from sql_db schema helper."""
        try:
            from sql_db import create_tables
        except Exception as exc:
            return {"success": False, "message": f"Unable to import create_tables: {exc}"}

        try:
            with self.get_connection() as conn:
                create_tables(conn, recreate=recreate)
            self.scheduler.ensure_tables()
            return {
                "success": True,
                "message": "Database schema bootstrap completed.",
                "recreate": bool(recreate),
            }
        except Exception as exc:
            return {"success": False, "message": f"Database bootstrap failed: {exc}", "recreate": bool(recreate)}

    def repair_schema(self) -> dict[str, Any]:
        """Repair known legacy schema drifts without dropping tables."""
        try:
            from sql_db import repair_legacy_schema
        except Exception as exc:
            return {"success": False, "message": f"Unable to import repair_legacy_schema: {exc}"}

        try:
            with self.get_connection() as conn:
                repair_legacy_schema(conn)
            self.scheduler.ensure_tables()
            self._ensure_interaction_runtime_tables()
            return {
                "success": True,
                "message": "Schema repair completed.",
            }
        except Exception as exc:
            return {"success": False, "message": f"Schema repair failed: {exc}"}

    def scheduler_command(self, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute scheduler commands from SOLF/agent semantics."""
        cmd = str(command or "").strip().lower()
        payload = payload if isinstance(payload, dict) else {}

        plan = self._compose_scheduler_command(cmd, payload)
        if plan:
            policy = self._invoke_solf_policy("scheduler_command_policy", payload)
            if str(policy.get("mode") or "allow").lower() == "deny":
                return {
                    "command": cmd,
                    "success": False,
                    "message": str(policy.get("message") or "Scheduler command denied by policy."),
                    "policy": policy,
                    "semantic_plan": plan,
                    "trace": {
                        "scheduler_clause": "scheduler_command",
                        "policy_clause": "scheduler_command_policy",
                    },
                }
            plan_args = self._as_string_key_dict(plan.get("args"))
            if plan_args:
                payload = plan_args

        if cmd == "start":
            self.scheduler.start()
            return {
                "command": cmd,
                "success": True,
                "message": "Scheduler started.",
                "trace": {
                    "scheduler_clause": "scheduler_command",
                    "policy_clause": "scheduler_command_policy",
                },
            }
        if cmd == "stop":
            self.scheduler.stop()
            return {
                "command": cmd,
                "success": True,
                "message": "Scheduler stopped.",
                "trace": {
                    "scheduler_clause": "scheduler_command",
                    "policy_clause": "scheduler_command_policy",
                },
            }
        if cmd == "add_job":
            query = str(payload.get("query") or "").strip()
            cron_expression = str(payload.get("cron_expression") or "").strip()
            tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
            job = self.scheduler.add_job(query=query, cron_expression=cron_expression, tags=tags)
            return {
                "command": cmd,
                "success": True,
                "message": "Job added.",
                "job": {
                    "job_id": job.job_id,
                    "query": job.query,
                    "cron_expression": job.cron_expression,
                    "tags": job.tags,
                    "enabled": job.enabled,
                },
                "trace": {
                    "scheduler_clause": "scheduler_command",
                    "policy_clause": "scheduler_command_policy",
                },
            }
        if cmd == "remove_job":
            job_id = str(payload.get("job_id") or "").strip()
            ok = self.scheduler.remove_job(job_id=job_id)
            return {
                "command": cmd,
                "success": ok,
                "message": "Job removed." if ok else "Job not found.",
                "trace": {
                    "scheduler_clause": "scheduler_command",
                    "policy_clause": "scheduler_command_policy",
                },
            }
        if cmd == "pause_job":
            job_id = str(payload.get("job_id") or "").strip()
            ok = self.scheduler.pause_job(job_id=job_id)
            return {
                "command": cmd,
                "success": ok,
                "message": "Job paused." if ok else "Job not found.",
                "trace": {
                    "scheduler_clause": "scheduler_command",
                    "policy_clause": "scheduler_command_policy",
                },
            }
        if cmd == "resume_job":
            job_id = str(payload.get("job_id") or "").strip()
            ok = self.scheduler.resume_job(job_id=job_id)
            return {
                "command": cmd,
                "success": ok,
                "message": "Job resumed." if ok else "Job not found.",
                "trace": {
                    "scheduler_clause": "scheduler_command",
                    "policy_clause": "scheduler_command_policy",
                },
            }
        if cmd == "list_jobs":
            jobs = self.scheduler.list_jobs()
            return {
                "command": cmd,
                "success": True,
                "jobs": [
                    {
                        "job_id": j.job_id,
                        "query": j.query,
                        "cron_expression": j.cron_expression,
                        "tags": j.tags,
                        "enabled": j.enabled,
                        "last_run_at": str(j.last_run_at) if j.last_run_at else None,
                        "next_run_at": str(j.next_run_at) if j.next_run_at else None,
                    }
                    for j in jobs
                ],
                "trace": {
                    "scheduler_clause": "scheduler_command",
                    "policy_clause": "scheduler_command_policy",
                },
            }

        return {"command": cmd, "success": False, "message": f"Unsupported scheduler command: {cmd}"}

    def state_command(self, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """State machine command API for workflow entities."""
        cmd = str(command or "").strip().lower()
        payload = payload if isinstance(payload, dict) else {}

        entity_type = str(payload.get("entity_type") or "").strip().lower()
        entity_id = int(payload.get("entity_id") or 0)

        if cmd == "get":
            state = self.state_machine.get_state(entity_type=entity_type, entity_id=entity_id)
            return {"command": cmd, **state}

        if cmd == "can":
            to_state = str(payload.get("to_state") or "").strip().lower()
            res = self.state_machine.can_transition(entity_type=entity_type, entity_id=entity_id, to_state=to_state)
            return {
                "command": cmd,
                "success": bool(res.success),
                "allowed": bool(res.allowed),
                "entity_type": res.entity_type,
                "entity_id": res.entity_id,
                "from_state": res.from_state,
                "to_state": res.to_state,
                "reason": res.reason,
                "data": res.data,
            }

        if cmd == "transition":
            to_state = str(payload.get("to_state") or "").strip().lower()
            event = str(payload.get("event") or "manual_transition").strip().lower()
            actor_ref = str(payload.get("actor_ref") or "system").strip().lower()
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}

            policy_payload = {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "to_state": to_state,
                "event": event,
                "actor_ref": actor_ref,
                "metadata": metadata,
            }
            policy = self._invoke_solf_policy("workflow_transition_policy", policy_payload)
            policy = self._harden_transition_policy(policy_payload, policy)
            if str(policy.get("mode") or "allow").lower() == "deny":
                return {
                    "command": cmd,
                    "success": False,
                    "allowed": False,
                    "message": str(policy.get("message") or "Transition denied by policy."),
                    "policy": policy,
                }

            res = self.state_machine.transition(
                entity_type=entity_type,
                entity_id=entity_id,
                to_state=to_state,
                event=event,
                actor_ref=actor_ref,
                metadata=metadata,
            )
            if res.success:
                self.event_bus.emit(
                    event_type="workflow.state_changed",
                    source_entity=entity_type,
                    entity_id=entity_id,
                    payload={
                        "from_state": res.from_state,
                        "to_state": res.to_state,
                        "event": event,
                        "actor_ref": actor_ref,
                    },
                    tags=["state_machine", entity_type],
                )
            return {
                "command": cmd,
                "success": bool(res.success),
                "allowed": bool(res.allowed),
                "entity_type": res.entity_type,
                "entity_id": res.entity_id,
                "from_state": res.from_state,
                "to_state": res.to_state,
                "event": res.event,
                "reason": res.reason,
                "policy": policy,
            }

        if cmd == "history":
            limit = int(payload.get("limit") or 20)
            rows = self.state_machine.history(entity_type=entity_type, entity_id=entity_id, limit=limit)
            return {
                "command": cmd,
                "success": True,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "count": len(rows),
                "rows": rows,
            }

        if cmd == "maintenance":
            max_rows = int(payload.get("max_rows") or 200)
            emitted = {"task.overdue": 0, "compliance.deadline_breach": 0, "workflow.case_sla_breach": 0}
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, name, assigned_to, due_date
                        FROM project_tasks
                        WHERE status NOT IN ('done', 'cancelled')
                          AND due_date IS NOT NULL
                          AND due_date < CURRENT_DATE
                        ORDER BY due_date ASC
                        LIMIT %s
                        """,
                        (max_rows,),
                    )
                    overdue_tasks = cur.fetchall() or []

                    cur.execute(
                        """
                        SELECT id, due_date_key, status
                        FROM compliance_actions
                        WHERE status NOT IN ('closed', 'completed', 'submitted', 'cancelled')
                          AND due_date_key IS NOT NULL
                          AND due_date_key < CAST(TO_CHAR(CURRENT_DATE, 'YYYYMMDD') AS BIGINT)
                        ORDER BY due_date_key ASC
                        LIMIT %s
                        """,
                        (max_rows,),
                    )
                    overdue_actions = cur.fetchall() or []

                    cur.execute(
                        """
                        SELECT id, case_no, status, sla_due_at
                        FROM workflow_cases
                        WHERE status NOT IN ('approved', 'rejected', 'cancelled')
                          AND sla_due_at IS NOT NULL
                          AND sla_due_at < NOW()
                        ORDER BY sla_due_at ASC
                        LIMIT %s
                        """,
                        (max_rows,),
                    )
                    overdue_cases = cur.fetchall() or []

            for row in overdue_tasks:
                task_id = int(row[0])
                self.event_bus.emit(
                    event_type="task.overdue",
                    source_entity="project_tasks",
                    entity_id=task_id,
                    payload={
                        "title": row[1],
                        "assigned_to": row[2],
                        "due_date": str(row[3]) if row[3] is not None else None,
                        "days_overdue": 1,
                    },
                    tags=["maintenance", "sla"],
                )
                emitted["task.overdue"] += 1

            for row in overdue_actions:
                action_id = int(row[0])
                self.event_bus.emit(
                    event_type="compliance.deadline_breach",
                    source_entity="compliance_actions",
                    entity_id=action_id,
                    payload={
                        "due_date_key": int(row[1]) if row[1] is not None else None,
                        "status": row[2],
                        "days_overdue": 1,
                    },
                    tags=["maintenance", "compliance"],
                )
                emitted["compliance.deadline_breach"] += 1

            for row in overdue_cases:
                case_id = int(row[0])
                self.event_bus.emit(
                    event_type="workflow.case_sla_breach",
                    source_entity="workflow_cases",
                    entity_id=case_id,
                    payload={
                        "case_no": row[1],
                        "status": row[2],
                        "sla_due_at": str(row[3]) if row[3] is not None else None,
                        "days_overdue": 1,
                    },
                    tags=["maintenance", "workflow"],
                )
                emitted["workflow.case_sla_breach"] += 1

            return {
                "command": cmd,
                "success": True,
                "emitted": emitted,
                "total_emitted": int(sum(emitted.values())),
            }

        return {"command": cmd, "success": False, "message": f"Unsupported state command: {cmd}"}

    def monitor_command(self, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Operational monitoring commands for workflow/event health."""
        cmd = str(command or "").strip().lower()
        payload = payload if isinstance(payload, dict) else {}

        if cmd == "stats":
            window_hours = int(payload.get("window_hours") or 24)
            metrics = self.workflow_monitor.collect_metrics(window_hours=window_hours)
            return {
                "command": cmd,
                "success": True,
                "metrics": metrics,
            }

        if cmd == "health":
            window_hours = int(payload.get("window_hours") or 24)
            status = self.workflow_monitor.health(window_hours=window_hours)
            return {
                "command": cmd,
                "success": True,
                **status,
            }

        if cmd == "snapshot":
            label = str(payload.get("label") or "").strip()
            snap = self.workflow_monitor.snapshot(label=label)
            return {
                "command": cmd,
                "success": True,
                **snap,
            }

        if cmd == "history":
            limit = int(payload.get("limit") or 20)
            rows = self.workflow_monitor.history(limit=limit)
            return {
                "command": cmd,
                "success": True,
                "count": len(rows),
                "rows": rows,
            }

        if cmd == "alerts":
            window_hours = int(payload.get("window_hours") or 24)
            result = self.workflow_reliability.evaluate_alerts(window_hours=window_hours)
            return {
                "command": cmd,
                "success": True,
                **result,
            }

        if cmd == "reconcile":
            window_hours = int(payload.get("window_hours") or 24)
            actor_ref = str(payload.get("actor_ref") or "system")
            result = self.workflow_reliability.reconcile_alerts(
                window_hours=window_hours,
                actor_ref=actor_ref,
            )
            return {
                "command": cmd,
                "success": True,
                **result,
            }

        if cmd == "incidents":
            status = str(payload.get("status") or "open")
            limit = int(payload.get("limit") or 50)
            rows = self.workflow_reliability.list_incidents(status=status, limit=limit)
            return {
                "command": cmd,
                "success": True,
                "status": status,
                "count": len(rows),
                "rows": rows,
            }

        if cmd == "incident":
            incident_id = int(payload.get("incident_id") or 0)
            action = str(payload.get("incident_action") or "")
            actor_ref = str(payload.get("actor_ref") or "system")
            if incident_id <= 0:
                return {"command": cmd, "success": False, "message": "incident_id must be > 0"}
            result = self.workflow_reliability.update_incident_status(
                incident_id=incident_id,
                action=action,
                actor_ref=actor_ref,
            )
            return {
                "command": cmd,
                **result,
            }

        # Dashboard commands (enhanced Phase 4)
        if cmd == "health-summary":
            result = self.dashboard_api.get_health_summary()
            return {
                "command": cmd,
                "success": True,
                **result,
            }

        if cmd == "incident-summary":
            result = self.dashboard_api.get_incident_summary()
            return {
                "command": cmd,
                "success": True,
                **result,
            }

        if cmd == "incidents-by-status":
            status = str(payload.get("status") or "open")
            limit = int(payload.get("limit") or 50)
            offset = int(payload.get("offset") or 0)
            result = self.dashboard_api.get_incidents_by_status(status=status, limit=limit, offset=offset)
            return {
                "command": cmd,
                "success": True,
                **result,
            }

        if cmd == "metrics-timeline":
            metric_key = str(payload.get("metric_key") or "")
            hours = int(payload.get("hours") or 168)
            result = self.dashboard_api.get_metrics_timeline(metric_key=metric_key, hours=hours)
            return {
                "command": cmd,
                "success": True,
                **result,
            }

        if cmd == "incident-trends":
            days = int(payload.get("days") or 7)
            result = self.dashboard_api.get_incident_trend_analysis(days=days)
            return {
                "command": cmd,
                "success": True,
                **result,
            }

        if cmd == "metric-distribution":
            result = self.dashboard_api.get_metric_distribution()
            return {
                "command": cmd,
                "success": True,
                **result,
            }

        if cmd == "sla-compliance":
            result = self.dashboard_api.get_sla_compliance()
            return {
                "command": cmd,
                "success": True,
                **result,
            }

        # Notification commands (Phase 4+ enhancement)
        if cmd == "subscribe-notifications":
            subscriber_ref = str(payload.get("subscriber_ref") or "")
            email = payload.get("email")
            webhook_url = payload.get("webhook_url")
            phone = payload.get("phone")
            result = self.notifier_engine.subscribe(
                subscriber_ref=subscriber_ref,
                email=email,
                webhook_url=webhook_url,
                phone=phone,
            )
            return {
                "command": cmd,
                **result,
            }

        # Audit commands (Phase 5)
        if cmd == "audit-trail":
            entity_type = payload.get("entity_type")
            entity_id = payload.get("entity_id")
            actor_ref = payload.get("actor_ref")
            hours = int(payload.get("hours") or 168)
            result = self.audit_compliance.get_audit_trail(
                entity_type=entity_type,
                entity_id=entity_id,
                actor_ref=actor_ref,
                hours=hours,
            )
            return {
                "command": cmd,
                "success": True,
                **result,
            }

        if cmd == "compliance-status":
            compliance_type = str(payload.get("compliance_type") or "")
            if not compliance_type:
                return {"command": cmd, "success": False, "message": "compliance_type is required"}
            result = self.audit_compliance.get_compliance_status_by_type(compliance_type=compliance_type)
            return {
                "command": cmd,
                "success": True,
                **result,
            }

        if cmd == "compliance-report":
            compliance_type = str(payload.get("compliance_type") or "")
            if not compliance_type:
                return {"command": cmd, "success": False, "message": "compliance_type is required"}
            result = self.audit_compliance.generate_compliance_report(compliance_type=compliance_type)
            return {
                "command": cmd,
                "success": True,
                **result,
            }

        # What-If Analysis / Simulation commands (Interaction Type 6)
        if cmd == "create-scenario":
            base_project_id = int(payload.get("base_project_id") or 0)
            scenario_name = str(payload.get("scenario_name") or "").strip()
            scenario_type = str(payload.get("scenario_type") or "what_if").strip() or "what_if"
            description = payload.get("description")
            actor_ref = str(payload.get("actor_ref") or "system")
            modifications_raw = payload.get("modifications") or {}
            if base_project_id <= 0 or not scenario_name:
                return {"command": cmd, "success": False, "message": "base_project_id and scenario_name are required"}
            try:
                if isinstance(modifications_raw, list):
                    normalized_mods: dict[str, Any] = {}
                    for mod in modifications_raw:
                        if not isinstance(mod, dict):
                            continue
                        scenario_mod = ScenarioModification(
                            param_type=str(mod.get("param_type") or mod.get("modification_type") or "custom"),
                            param_name=str(mod.get("param_name") or mod.get("parameter_name") or "custom_param"),
                            original_value=float(mod.get("original_value", 0) or 0),
                            modified_value=float(mod.get("modified_value", mod.get("value", 0)) or 0),
                            reason=str(mod.get("reason") or mod.get("description") or "")
                        )
                        normalized_mods[scenario_mod.param_name] = {
                            "type": scenario_mod.param_type,
                            "original": scenario_mod.original_value,
                            "modified": scenario_mod.modified_value,
                            "reason": scenario_mod.reason,
                        }
                elif isinstance(modifications_raw, dict):
                    normalized_mods = modifications_raw
                else:
                    normalized_mods = {}

                result = self.project_simulator.create_scenario(
                    base_project_id=base_project_id,
                    scenario_name=scenario_name,
                    scenario_type=scenario_type,
                    description=description,
                    modifications=normalized_mods,
                    actor_ref=actor_ref,
                )
                return {
                    "command": cmd,
                    "success": True,
                    **result,
                }
            except Exception as e:
                return {"command": cmd, "success": False, "message": f"Error creating scenario: {e}"}

        if cmd == "simulate":
            scenario_id = int(payload.get("scenario_id") or 0)
            include_resource_check = bool(payload.get("include_resource_check", True))
            if scenario_id <= 0:
                return {"command": cmd, "success": False, "message": "scenario_id is required"}
            try:
                result = self.project_simulator.simulate_project_schedule(
                    scenario_id=scenario_id,
                    include_resource_check=include_resource_check,
                )
                return {
                    "command": cmd,
                    "success": True,
                    **result,
                }
            except Exception as e:
                return {"command": cmd, "success": False, "message": f"Error simulating schedule: {e}"}

        if cmd == "budget-impact":
            scenario_id = int(payload.get("scenario_id") or 0)
            if scenario_id <= 0:
                return {"command": cmd, "success": False, "message": "scenario_id is required"}
            try:
                result = self.project_simulator.calculate_budget_impact(scenario_id=scenario_id)
                return {
                    "command": cmd,
                    "success": True,
                    **result,
                }
            except Exception as e:
                return {"command": cmd, "success": False, "message": f"Error calculating budget impact: {e}"}

        if cmd == "compare-scenarios":
            scenario1_id = int(payload.get("scenario1_id") or 0)
            scenario2_id = int(payload.get("scenario2_id") or 0)
            if scenario1_id <= 0 or scenario2_id <= 0:
                return {"command": cmd, "success": False, "message": "scenario1_id and scenario2_id are required"}
            try:
                result = self.project_simulator.compare_scenarios(
                    base_scenario_id=scenario1_id,
                    comparison_scenario_id=scenario2_id,
                )
                return {
                    "command": cmd,
                    "success": True,
                    **result,
                }
            except Exception as e:
                return {"command": cmd, "success": False, "message": f"Error comparing scenarios: {e}"}

        if cmd == "scenarios":
            project_id = int(payload.get("project_id") or 0)
            status = str(payload.get("status") or "draft").strip() or "draft"
            if project_id <= 0:
                return {"command": cmd, "success": False, "message": "project_id is required"}
            try:
                result = self.project_simulator.list_scenarios(
                    base_project_id=project_id,
                    status=status,
                )
                return {
                    "command": cmd,
                    "success": True,
                    **result,
                }
            except Exception as e:
                return {"command": cmd, "success": False, "message": f"Error listing scenarios: {e}"}

        if cmd == "scenario-details":
            scenario_id = int(payload.get("scenario_id") or 0)
            if scenario_id <= 0:
                return {"command": cmd, "success": False, "message": "scenario_id is required"}
            try:
                result = self.project_simulator.get_scenario_details(scenario_id=scenario_id)
                return {
                    "command": cmd,
                    "success": True,
                    **result,
                }
            except Exception as e:
                return {"command": cmd, "success": False, "message": f"Error retrieving scenario: {e}"}

        return {"command": cmd, "success": False, "message": f"Unsupported monitor command: {cmd}"}

    def autonomous_query(self, question: str, max_steps: int = 2, record_history: bool = True) -> dict[str, Any]:
        """Autonomous retrieval loop: resolve scope -> attribute lookup -> grounded answer.
        
        Args:
            question: Natural language query.
            max_steps: Maximum planner steps (default: 2 for cost efficiency; typical queries need 1-2).
                      Hard capped at 4 to prevent runaway costs.
        
        Results are cached for 1 hour to avoid redundant planner/search calls.
        """
        question = str(question or "").strip()
        if not question:
            raise ValueError("question must not be empty")

        clause_request = self._extract_solf_clause_request(question)
        if isinstance(clause_request, dict):
            evaluated = self.evaluate_solf_clause(
                clause_name=str(clause_request.get("clause_name") or ""),
                payload=clause_request.get("payload") if isinstance(clause_request.get("payload"), dict) else {},
                args=clause_request.get("args") if isinstance(clause_request.get("args"), list) else [],
            )
            query_like = self._format_direct_solf_query_result(question, evaluated)
            answer_text = str(query_like.get("answer") or "").strip()
            if record_history:
                self.state.history.append({"role": "user", "content": question})
                self.state.history.append({"role": "assistant", "content": answer_text})
            return {
                "answer": answer_text,
                "answer_source": "solf_clause",
                "answer_sources": ["solf_clause"],
                "grounding": {
                    "parsed": {
                        "intent": "solf_clause",
                        "entity_name": None,
                        "attribute_name": None,
                        "confidence": 1.0,
                        "criteria": None,
                    },
                    "query_style": {
                        "mode": "allow",
                        "query_style": "solf_clause",
                        "initial_action": "invoke_solf_clause",
                        "reason": "explicit_solf_predicate_request",
                        "policy_clause": "query_style_policy",
                    },
                    "scope": None,
                    "discovery_search": None,
                    "attribute_result": None,
                    "criteria_result": None,
                    "direct_query_result": query_like,
                    "solf_clause_result": evaluated,
                },
                "policy": {
                    "query": {"mode": "allow", "reason": "explicit_solf_predicate_request"},
                    "style": {"mode": "allow", "reason": "explicit_solf_predicate_request"},
                    "answer": None,
                },
                "trace": [
                    {
                        "step": -1,
                        "tool": "evaluate_solf_clause",
                        "clause_name": evaluated.get("clause_name"),
                        "success": bool(evaluated.get("success")),
                    }
                ],
                "model": "deterministic_solf",
            }

        self.logger.info("Autonomous query started: max_steps=%s question=%s", max_steps, question)
        
        # Check cache first (avoid expensive planner + retrieval for repeated queries).
        if self.query_cache:
            cached = self.query_cache.get(question)
            if cached is not None and "answer" in cached:
                cached = dict(cached)
                cached["_cache_hit"] = True
                self.logger.info("Autonomous query cache hit")
                return cached

        # Hard cap to prevent excessive planner costs.
        max_steps = min(max(1, int(max_steps)), 4)
        parsed = self.query_engine.parse_query(question)
        parsed_payload = {
            "intent": parsed.intent,
            "entity_name": parsed.entity_name,
            "attribute_name": parsed.attribute_name,
            "confidence": parsed.confidence,
            "criteria": parsed.criteria,
        }
        style_policy = self._classify_query_style(question, parsed_payload)
        selected_model = self._select_model_by_confidence(parsed_payload.get("confidence"), default=EXTRACT_MODEL)
        state: dict[str, Any] = {
            "question": question,
            "parsed": parsed_payload,
            "query_style": style_policy,
            "scope": None,
            "discovery_search": None,
            "attribute_result": None,
            "criteria_result": None,
            "criteria_contextual_result": None,
            "trace": [],
        }

        def _attach_evidence_bundle(result: dict[str, Any]) -> dict[str, Any]:
            enriched = dict(result)
            enriched["evidence_bundle"] = self._build_query_evidence_bundle(question, enriched)
            return enriched

        query_policy_payload = {
            "question": question,
            "parsed": state["parsed"],
        }
        query_policy = self._invoke_solf_policy("interaction_query_policy", query_policy_payload)
        state["trace"].append(
            {
                "step": -1,
                "policy": "interaction_query_policy",
                "result": query_policy,
            }
        )
        state["trace"].append(
            {
                "step": -1,
                "policy": "query_style_policy",
                "result": style_policy,
            }
        )

        if style_policy.get("mode") == "deny":
            answer_text = str(style_policy.get("message") or "This query style is blocked by policy.").strip()
            answer_text = f"[source: policy_blocked] {answer_text}".strip()
            if record_history:
                self.state.history.append({"role": "user", "content": question})
                self.state.history.append({"role": "assistant", "content": answer_text})
            return {
                "answer": answer_text,
                "answer_source": "policy_blocked",
                "answer_sources": ["policy_blocked"],
                "grounding": {
                    "parsed": state.get("parsed"),
                    "query_style": state.get("query_style"),
                    "scope": None,
                    "discovery_search": None,
                    "attribute_result": None,
                    "criteria_result": None,
                },
                "policy": {
                    "query": query_policy,
                    "style": style_policy,
                    "answer": None,
                },
                "trace": state.get("trace", []),
                "model": selected_model,
            }

        if query_policy.get("mode") == "deny":
            answer_text = str(query_policy.get("message") or "This interaction is blocked by domain policy.").strip()
            answer_text = f"[source: policy_blocked] {answer_text}".strip()
            if record_history:
                self.state.history.append({"role": "user", "content": question})
                self.state.history.append({"role": "assistant", "content": answer_text})
            return {
                "answer": answer_text,
                "answer_source": "policy_blocked",
                "answer_sources": ["policy_blocked"],
                "grounding": {
                    "parsed": state.get("parsed"),
                    "scope": None,
                    "discovery_search": None,
                    "attribute_result": None,
                },
                "policy": {
                    "query": query_policy,
                    "style": style_policy,
                    "answer": None,
                },
                "trace": state.get("trace", []),
                "model": selected_model,
            }

        if query_policy.get("mode") == "escalate":
            max_steps = min(max(1, int(max_steps)) + 1, 8)

        # Direct factual fallback: if QueryEngine can already answer, use it and skip planner drift.
        # This prevents false [source: not_found] when resolve_scope returns empty but SQL facts exist.
        aggregate_value_question = bool(
            hasattr(self.query_engine, "_looks_like_travel_expense_total_question")
            and self.query_engine._looks_like_travel_expense_total_question(question)
        )
        if (
            parsed.intent in {"attribute_lookup", "semantic_lookup", "relationship_lookup"}
            and (
                aggregate_value_question
                or not self.query_engine._is_value_document_context_question(question)
            )
        ):
            try:
                direct_answer = self.query_engine.answer(question)
            except Exception:
                direct_answer = None

            if isinstance(direct_answer, dict):
                direct_source = str(direct_answer.get("source") or "").strip().lower()
                direct_text = str(direct_answer.get("answer") or "").strip()
                if direct_source and direct_source != "not_found" and direct_text:
                    state["trace"].append(
                        {
                            "step": 0,
                            "tool": "query_engine_direct_fallback",
                            "source": direct_source,
                            "used": True,
                        }
                    )

                    grounding = {
                        "parsed": state.get("parsed"),
                        "query_style": state.get("query_style"),
                        "scope": None,
                        "discovery_search": None,
                        "attribute_result": None,
                        "criteria_result": None,
                        "direct_query_result": direct_answer,
                    }
                    answer_policy_payload = {
                        "question": question,
                        "grounding": grounding,
                    }
                    answer_policy = self._invoke_solf_policy("interaction_answer_policy", answer_policy_payload)
                    state["trace"].append(
                        {
                            "step": len(state.get("trace", [])),
                            "policy": "interaction_answer_policy",
                            "result": answer_policy,
                        }
                    )

                    if answer_policy.get("mode") == "deny":
                        answer_text = str(answer_policy.get("message") or "Answer generation is blocked by domain policy.").strip()
                        answer_text = f"[source: policy_blocked] {answer_text}".strip()
                        if record_history:
                            self.state.history.append({"role": "user", "content": question})
                            self.state.history.append({"role": "assistant", "content": answer_text})
                        return {
                            "answer": answer_text,
                            "answer_source": "policy_blocked",
                            "answer_sources": ["policy_blocked"],
                            "grounding": grounding,
                            "policy": {
                                "query": query_policy,
                                "style": style_policy,
                                "answer": answer_policy,
                            },
                            "trace": state.get("trace", []),
                            "model": selected_model,
                        }

                    answer_text = direct_text
                    answer_sources = [direct_source]
                    result = {
                        "answer": answer_text,
                        "answer_source": direct_source,
                        "answer_sources": answer_sources,
                        "grounding": grounding,
                        "policy": {
                            "query": query_policy,
                            "style": style_policy,
                            "answer": answer_policy,
                        },
                        "trace": state.get("trace", []),
                        "model": selected_model,
                    }
                    if record_history:
                        self.state.history.append({"role": "user", "content": question})
                        self.state.history.append({"role": "assistant", "content": answer_text})
                    if self.query_cache:
                        self.query_cache.put(question, _attach_evidence_bundle(result))
                    return _attach_evidence_bundle(result)

        # Fast path: direct attribute questions should hit SQL/object lookup first.
        # This avoids planner drift when Qdrant/Discovery scope is empty but structured data exists.
        if parsed.intent == "attribute_lookup" and parsed.entity_name and parsed.attribute_name:
            direct = self.query_engine.lookup_attribute(
                attribute_name=parsed.attribute_name,
                entity_name=parsed.entity_name,
                scope=None,
            )
            state["attribute_result"] = direct
            state["trace"].append(
                {
                    "step": 0,
                    "tool": "lookup_attribute_fast_path",
                    "attribute_name": parsed.attribute_name,
                    "entity_name": parsed.entity_name,
                    "found": bool(direct),
                }
            )
            if direct:
                grounding = {
                    "parsed": state.get("parsed"),
                    "query_style": state.get("query_style"),
                    "scope": None,
                    "discovery_search": None,
                    "attribute_result": direct,
                    "criteria_result": None,
                }
                answer_policy_payload = {
                    "question": question,
                    "grounding": grounding,
                }
                answer_policy = self._invoke_solf_policy("interaction_answer_policy", answer_policy_payload)
                state["trace"].append(
                    {
                        "step": len(state.get("trace", [])),
                        "policy": "interaction_answer_policy",
                        "result": answer_policy,
                    }
                )
                if answer_policy.get("mode") == "deny":
                    answer_text = str(answer_policy.get("message") or "Answer generation is blocked by domain policy.").strip()
                    answer_text = f"[source: policy_blocked] {answer_text}".strip()
                    if record_history:
                        self.state.history.append({"role": "user", "content": question})
                        self.state.history.append({"role": "assistant", "content": answer_text})
                    return {
                        "answer": answer_text,
                        "answer_source": "policy_blocked",
                        "answer_sources": ["policy_blocked"],
                        "grounding": grounding,
                        "policy": {
                            "query": query_policy,
                            "style": style_policy,
                            "answer": answer_policy,
                        },
                        "trace": state.get("trace", []),
                        "model": selected_model,
                    }

                display_attr = self.query_engine._get_attribute_display_name(direct["attribute_name"], parsed.language)
                if str(parsed.language or "en").lower() == "de":
                    answer_text = f"[source: sql_exact] {display_attr} für {direct['entity_name']} ist {direct['attribute_value']}."
                else:
                    answer_text = f"[source: sql_exact] {display_attr} for {direct['entity_name']} is {direct['attribute_value']}."

                if record_history:
                    self.state.history.append({"role": "user", "content": question})
                    self.state.history.append({"role": "assistant", "content": answer_text})

                result = {
                    "answer": answer_text,
                    "answer_source": "sql_exact",
                    "answer_sources": ["sql_exact"],
                    "grounding": grounding,
                    "policy": {
                        "query": query_policy,
                        "style": style_policy,
                        "answer": answer_policy,
                    },
                    "trace": state.get("trace", []),
                    "model": selected_model,
                }
                if self.query_cache:
                    self.query_cache.put(question, _attach_evidence_bundle(result))
                return _attach_evidence_bundle(result)

        for step in range(max(1, int(max_steps))):
            self.logger.info("Autonomous query workflow step=%s", step)
            if (
                step == 0
                and self.query_engine._is_value_document_context_question(question)
                and not state.get("criteria_result")
            ):
                decision = {
                    "action": "search_criteria",
                    "attribute_name": str((state.get("parsed") or {}).get("attribute_name") or "").strip() or None,
                    "entity_name": str((state.get("parsed") or {}).get("entity_name") or "").strip() or None,
                    "rationale": "Value + document-context question detected; run criteria retrieval first to ground extraction.",
                }
            else:
                decision = self._planner_step(question, state, step)
            action = decision["action"]
            parsed_intent = str((state.get("parsed") or {}).get("intent") or "").strip().lower()
            has_criteria_result = bool(state.get("criteria_result"))

            # Criteria-intent guardrail: run SQL criteria retrieval before semantic-only
            # discovery loops when the parser has already identified a list/filter ask.
            if parsed_intent == "criteria_lookup" and not has_criteria_result and action in {"search_discovery", "resolve_scope"}:
                action = "search_criteria"
                decision["action"] = action
                decision["rationale"] = (
                    "Parsed intent is criteria_lookup; prioritize criteria retrieval first "
                    "to avoid discovery-only drift on document/note list queries."
                )

            if action == "resolve_scope" and self._scope_is_empty(state.get("scope")):
                if parsed_intent == "criteria_lookup" and not has_criteria_result:
                    action = "search_criteria"
                    decision["action"] = action
                    decision["rationale"] = (
                        "Scope resolution returned no vector/discovery candidates, "
                        "so run SQL criteria retrieval without scope constraints before finalizing."
                    )
                else:
                    action = "finalize"
                    decision["action"] = action
                    decision["rationale"] = (
                        "Scope resolution already returned no Qdrant or Discovery candidates, "
                        "so repeating resolve_scope would not add new evidence."
                    )
            state["trace"].append({"step": step, "planner": decision})

            if action == "resolve_scope":
                scope = self.query_engine.resolve_search_scope(question, limit=12)
                state["scope"] = {
                    "candidate_count": scope.get("candidate_count", 0),
                    "discovery_candidate_count": scope.get("discovery_candidate_count", 0),
                    "qdrant_search_mode": scope.get("qdrant_search_mode"),
                    "qdrant_collection": scope.get("qdrant_collection"),
                    "node_ids": scope.get("node_ids", scope.get("object_ids", [])),
                    "edge_ids": scope.get("edge_ids", scope.get("relationship_ids", [])),
                    "doc_ids": scope.get("doc_ids", []),
                    "top_candidates": (scope.get("candidates") or [])[:3],
                    "discovery_results": (scope.get("discovery_results") or [])[:5],
                    "nodes": (scope.get("nodes") or scope.get("objects") or [])[:5],
                    "edges": (scope.get("edges") or scope.get("relationships") or [])[:5],
                    "documents": (scope.get("documents") or [])[:5],
                }
                continue

            if action == "search_discovery":
                discovery_payload = self.search_discovery(question, limit=8)
                state["discovery_search"] = {
                    "candidate_count": discovery_payload.get("candidate_count", 0),
                    "summary": discovery_payload.get("summary"),
                    "results": (discovery_payload.get("results") or [])[:5],
                }
                state["trace"].append(
                    {
                        "step": step,
                        "tool": "search_discovery",
                        "candidate_count": discovery_payload.get("candidate_count", 0),
                    }
                )
                continue

            if action == "lookup_attribute":
                attribute_name = (
                    str(decision.get("attribute_name") or "").strip()
                    or parsed.attribute_name
                    or ""
                )
                entity_name = str(decision.get("entity_name") or "").strip() or parsed.entity_name
                if not attribute_name:
                    state["trace"].append(
                        {
                            "step": step,
                            "tool": "lookup_attribute",
                            "result": "skipped_missing_attribute_name",
                        }
                    )
                    continue

                lookup_criteria = dict(parsed.criteria) if isinstance(parsed.criteria, dict) else {}
                has_relation_context = bool(
                    str(parsed.relation_name or "").strip()
                    or list(lookup_criteria.get("relation_filters") or [])
                    or list(lookup_criteria.get("relationship_filters") or [])
                )
                if has_relation_context:
                    # Enforce relationship traversal semantics for relation-driven questions
                    # so broad fallbacks cannot drift to unrelated entities.
                    lookup_criteria["strict_relation_lookup"] = True

                result = self.query_engine.lookup_attribute(
                    attribute_name=attribute_name,
                    entity_name=entity_name,
                    relation_name=parsed.relation_name,
                    scope=state.get("scope") if isinstance(state.get("scope"), dict) else None,
                    criteria=lookup_criteria if lookup_criteria else None,
                )

                # Canonical recovery for person->companies shareholding questions when
                # planner emits non-canonical attributes such as shares_held/holds_shares_in.
                if not result:
                    normalized_attr = str(attribute_name or "").strip().lower()
                    relation_filters = list(lookup_criteria.get("relation_filters") or []) + list(lookup_criteria.get("relationship_filters") or [])

                    # LLM plans often keep relationship filters nested under llm_query_plan.filters.
                    llm_plan = lookup_criteria.get("llm_query_plan") if isinstance(lookup_criteria.get("llm_query_plan"), dict) else {}
                    plan_filters = llm_plan.get("filters") if isinstance(llm_plan.get("filters"), dict) else {}
                    nested_relationship_filters = plan_filters.get("relationship_filters") if isinstance(plan_filters.get("relationship_filters"), list) else []
                    for rf in nested_relationship_filters:
                        if not isinstance(rf, dict):
                            continue
                        rel_type = str(rf.get("relationship_type") or rf.get("relation") or "").strip()
                        target_name = str(rf.get("target_name") or rf.get("target_entity_name") or "").strip()
                        mapped = {
                            "relationship_names": [rel_type] if rel_type else [],
                            "target_name": target_name,
                        }
                        relation_filters.append(mapped)

                    relationship_blob = " ".join(
                        " ".join(
                            [
                                str(item.get("relationship_type") or ""),
                                " ".join(str(x) for x in (item.get("relationship_names") or [])),
                                str(item.get("target_name") or ""),
                            ]
                        )
                        for item in relation_filters
                        if isinstance(item, dict)
                    ).lower()
                    share_query_hint = bool(
                        re.search(r"\b(share|shares|shareholder|shareholders|ownership|anteil|gesellschafter|aktion[aä]r)\b", str(question or ""), flags=re.IGNORECASE)
                    )
                    non_canonical_share_attr = normalized_attr in {
                        "shares_held",
                        "holds_shares_in",
                        "shareholdings",
                        "shareholding",
                        "ownership",
                    }
                    relation_share_hint = bool(re.search(r"share|owner|ownership|gesellschafter|aktion", relationship_blob, flags=re.IGNORECASE))

                    if share_query_hint and (non_canonical_share_attr or relation_share_hint):
                        canonical_criteria = dict(lookup_criteria)
                        canonical_criteria.setdefault("question_type", "companies_held")
                        canonical_criteria.setdefault("semantic_frame", "shareholders_person")
                        canonical_criteria.setdefault("relationship_concept", "shareholder")
                        if relation_filters:
                            canonical_criteria["relation_filters"] = relation_filters
                            canonical_criteria["strict_relation_lookup"] = True
                        else:
                            canonical_criteria.pop("strict_relation_lookup", None)

                        recovered = self.query_engine.lookup_attribute(
                            attribute_name="shareholders",
                            entity_name=entity_name,
                            relation_name="shareholder",
                            scope=state.get("scope") if isinstance(state.get("scope"), dict) else None,
                            criteria=canonical_criteria,
                        )
                        if recovered:
                            result = recovered
                            attribute_name = "shareholders"
                            state["trace"].append(
                                {
                                    "step": step,
                                    "tool": "lookup_attribute_shareholding_recovery",
                                    "attribute_name": "shareholders",
                                    "entity_name": entity_name,
                                    "found": True,
                                }
                            )

                    # If canonical recovery still misses, ensure semantic scope is populated
                    # so final answer synthesis can use grounded qdrant/discovery evidence.
                    if not result and share_query_hint and not isinstance(state.get("scope"), dict):
                        try:
                            scope_question = str(question or "").strip()
                            resolved_scope_entity = ""
                            if str(entity_name or "").strip():
                                try:
                                    with self.query_engine._get_connection() as conn:
                                        details = self.query_engine._resolve_entity_name_for_lookup_details(conn, entity_name)
                                    if isinstance(details, dict):
                                        resolved_scope_entity = str(details.get("resolved_name") or "").strip()
                                except Exception:
                                    resolved_scope_entity = ""

                            if resolved_scope_entity and str(entity_name or "").strip():
                                try:
                                    scope_question = re.sub(
                                        re.escape(str(entity_name)),
                                        resolved_scope_entity,
                                        scope_question,
                                        count=1,
                                        flags=re.IGNORECASE,
                                    )
                                except Exception:
                                    # Safe fallback shape when replacement fails.
                                    scope_question = f"What companies does {resolved_scope_entity} hold shares in?"

                            scope = self.query_engine.resolve_search_scope(scope_question, limit=12)
                            state["scope"] = {
                                "candidate_count": scope.get("candidate_count", 0),
                                "discovery_candidate_count": scope.get("discovery_candidate_count", 0),
                                "qdrant_search_mode": scope.get("qdrant_search_mode"),
                                "qdrant_collection": scope.get("qdrant_collection"),
                                "node_ids": scope.get("node_ids", scope.get("object_ids", [])),
                                "edge_ids": scope.get("edge_ids", scope.get("relationship_ids", [])),
                                "doc_ids": scope.get("doc_ids", []),
                                "top_candidates": (scope.get("candidates") or [])[:5],
                                "discovery_results": (scope.get("discovery_results") or [])[:5],
                                "nodes": (scope.get("nodes") or scope.get("objects") or [])[:5],
                                "edges": (scope.get("edges") or scope.get("relationships") or [])[:5],
                                "documents": (scope.get("documents") or [])[:5],
                            }
                            state["trace"].append(
                                {
                                    "step": step,
                                    "tool": "shareholding_scope_recovery",
                                    "scope_question": scope_question,
                                    "resolved_scope_entity": resolved_scope_entity or None,
                                    "candidate_count": int(scope.get("candidate_count", 0) or 0),
                                    "discovery_candidate_count": int(scope.get("discovery_candidate_count", 0) or 0),
                                }
                            )
                        except Exception:
                            pass

                state["attribute_result"] = result
                state["trace"].append(
                    {
                        "step": step,
                        "tool": "lookup_attribute",
                        "attribute_name": attribute_name,
                        "entity_name": entity_name,
                        "found": bool(result),
                    }
                )
                continue

            if action == "search_criteria":
                scope_payload = state.get("scope") if isinstance(state.get("scope"), dict) else None
                criteria_result = self.search_criteria(question, scope=scope_payload)
                state["criteria_result"] = criteria_result if criteria_result.get("success") else None
                state["trace"].append(
                    {
                        "step": step,
                        "tool": "search_criteria",
                        "found": bool(criteria_result.get("success")),
                        "count": int(criteria_result.get("count", 0) or 0),
                    }
                )
                continue

            if action == "finalize":
                break

        grounding = {
            "parsed": state.get("parsed"),
            "query_style": state.get("query_style"),
            "scope": state.get("scope"),
            "discovery_search": state.get("discovery_search"),
            "attribute_result": state.get("attribute_result"),
            "criteria_result": state.get("criteria_result"),
            "criteria_contextual_result": state.get("criteria_contextual_result"),
        }

        # Generic bridge: when criteria retrieval finds key documents and the user asks
        # for a value-style answer (not a list request), run grounded contextual resolution.
        parsed_intent = str((state.get("parsed") or {}).get("intent") or "").strip().lower()
        parsed_criteria = (state.get("parsed") or {}).get("criteria") if isinstance((state.get("parsed") or {}).get("criteria"), dict) else {}
        response_format = str((parsed_criteria or {}).get("response_format") or "").strip().lower()
        criteria_result_payload = state.get("criteria_result") if isinstance(state.get("criteria_result"), dict) else None
        if (
            isinstance(criteria_result_payload, dict)
            and bool(criteria_result_payload.get("success"))
            and self.query_engine._is_value_document_context_question(question)
            and not self.query_engine._criteria_question_prefers_listing(question, response_format)
        ):
            scope_payload = state.get("scope") if isinstance(state.get("scope"), dict) else {}
            scoped_candidates = list(scope_payload.get("top_candidates") or []) if isinstance(scope_payload, dict) else []
            if not scoped_candidates:
                try:
                    scoped_candidates = self.query_engine.qdrant_candidates(question, limit=8)
                except Exception:
                    scoped_candidates = []
            discovery_payload = state.get("discovery_search") if isinstance(state.get("discovery_search"), dict) else {}
            scoped_discovery = list(discovery_payload.get("results") or []) if isinstance(discovery_payload, dict) else []

            criteria_contextual = self.query_engine._criteria_contextual_llm_resolution_fallback(
                question=question,
                parsed=parsed,
                criteria_result=criteria_result_payload,
                candidates=scoped_candidates,
                discovery_results=scoped_discovery,
            )
            if isinstance(criteria_contextual, dict) and str(criteria_contextual.get("answer") or "").strip():
                state["criteria_contextual_result"] = criteria_contextual
                grounding["criteria_contextual_result"] = criteria_contextual
                state["trace"].append(
                    {
                        "step": len(state.get("trace", [])),
                        "tool": "criteria_contextual_llm_resolution",
                        "success": True,
                    }
                )

        parsed_entity_anchor = str((grounding.get("parsed") or {}).get("entity_name") or "").strip()
        parsed_intent_anchor = str((grounding.get("parsed") or {}).get("intent") or "").strip().lower()
        if parsed_entity_anchor and parsed_intent_anchor != "criteria_lookup":
            grounding = self._sanitize_grounding_for_anchor(grounding, parsed_entity_anchor)

        answer_policy_payload = {
            "question": question,
            "grounding": grounding,
        }
        answer_policy = self._invoke_solf_policy("interaction_answer_policy", answer_policy_payload)
        state["trace"].append(
            {
                "step": len(state.get("trace", [])),
                "policy": "interaction_answer_policy",
                "result": answer_policy,
            }
        )

        if answer_policy.get("mode") == "deny":
            answer_text = str(answer_policy.get("message") or "Answer generation is blocked by domain policy.").strip()
            answer_text = f"[source: policy_blocked] {answer_text}".strip()
            if record_history:
                self.state.history.append({"role": "user", "content": question})
                self.state.history.append({"role": "assistant", "content": answer_text})
            return {
                "answer": answer_text,
                "answer_source": "policy_blocked",
                "answer_sources": ["policy_blocked"],
                "grounding": grounding,
                "policy": {
                    "query": query_policy,
                    "style": style_policy,
                    "answer": answer_policy,
                },
                "trace": state.get("trace", []),
                "model": selected_model,
            }

        criteria_contextual_result = grounding.get("criteria_contextual_result") if isinstance(grounding, dict) else None
        if isinstance(criteria_contextual_result, dict) and str(criteria_contextual_result.get("answer") or "").strip():
            answer_text = f"[source: criteria_contextual_llm] {str(criteria_contextual_result.get('answer') or '').strip()}".strip()
            if record_history:
                self.state.history.append({"role": "user", "content": question})
                self.state.history.append({"role": "assistant", "content": answer_text})

            result = {
                "answer": answer_text,
                "answer_source": "criteria_contextual_llm",
                "answer_sources": ["criteria_contextual_llm"],
                "grounding": grounding,
                "policy": {
                    "query": query_policy,
                    "style": style_policy,
                    "answer": answer_policy,
                },
                "trace": state.get("trace", []),
                "model": "deterministic_criteria_contextual",
            }
            if self.query_cache:
                self.query_cache.put(question, _attach_evidence_bundle(result))
            return _attach_evidence_bundle(result)

        # Deterministic criteria-list answer to avoid count-only summaries for
        # explicit list/show document queries.
        deterministic_criteria_answer = self._deterministic_criteria_answer(question, grounding)
        if deterministic_criteria_answer:
            criteria_result = grounding.get("criteria_result") if isinstance(grounding, dict) else None
            criteria_source = str((criteria_result or {}).get("source") or "criteria_sql").strip() or "criteria_sql"
            answer_text = f"[source: {criteria_source}] {deterministic_criteria_answer}".strip()
            answer_sources = [criteria_source]
            if record_history:
                self.state.history.append({"role": "user", "content": question})
                self.state.history.append({"role": "assistant", "content": answer_text})

            result = {
                "answer": answer_text,
                "answer_source": criteria_source,
                "answer_sources": answer_sources,
                "grounding": grounding,
                "policy": {
                    "query": query_policy,
                    "style": style_policy,
                    "answer": answer_policy,
                },
                "trace": state.get("trace", []),
                "model": "deterministic_criteria_listing",
            }
            if self.query_cache:
                self.query_cache.put(question, _attach_evidence_bundle(result))
            return _attach_evidence_bundle(result)

        answer_prompt = (
            "You are an IDMS assistant. Use only grounded retrieval results.\n"
            "If attribute_result exists, answer directly with that value and mention whether it came from SQL, Qdrant-guided SQL, or Discovery.\n"
            "If criteria_result exists, list the strongest matches and mention which criteria terms matched.\n"
            "If criteria_contextual_result exists, prioritize that grounded value answer over listing results.\n"
            "If only semantic scope or Discovery search exists, summarize the best matching entities/documents/media and mention the retrieval source.\n"
            "Do not invent facts not in grounding.\n\n"
            f"Question: {question}\n"
            f"Grounding: {json.dumps(grounding, ensure_ascii=False)}"
        )
        answer_model = self._select_model_by_confidence(parsed_payload.get("confidence"), default=EXTRACT_MODEL)
        self._record_model_usage("interaction_answer_generation", answer_model, parsed_payload.get("confidence"))
        answer_response = generate_content_with_openrouter_fallback(
            primary_call=lambda: self.client.models.generate_content(
                model=answer_model,
                contents=[answer_prompt],
            ),
            model=answer_model,
            contents=[answer_prompt],
            temperature=0.0,
            call_name="interaction_answer_generation",
            complexity="simple",
        )
        answer_text = (answer_response.text or "").strip()

        answer_sources = self._answer_sources(grounding)
        source = answer_sources[0]
        answer_text = f"[source: {'+'.join(answer_sources)}] {answer_text}".strip()

        if record_history:
            self.state.history.append({"role": "user", "content": question})
            self.state.history.append({"role": "assistant", "content": answer_text})

        result = {
            "answer": answer_text,
            "answer_source": source,
            "answer_sources": answer_sources,
            "grounding": grounding,
            "policy": {
                "query": query_policy,
                "style": style_policy,
                "answer": answer_policy,
            },
            "trace": state.get("trace", []),
            "model": answer_model,
        }
        
        # Cache result for 1 hour to avoid re-running similar queries.
        if self.query_cache:
            self.query_cache.put(question, result)
        
        return result

    def build_adk_agent(self) -> Any:
        """Attempt to build an ADK agent wired with these tool callables."""
        if adk is None or not hasattr(adk, "Agent"):
            self.logger.warning("ADK unavailable; agent cannot be built")
            return None

        model_resource = (
            f"projects/{PROJECT_ID}/locations/{LOCATION}/publishers/google/models/{CHAT_MODEL}"
        )
        try:
            self.logger.info("Building ADK agent")
            return adk.Agent(
                name="IDMSInteractionAgent",
                model=model_resource,
                instruction=(
                    "Use tools for data-grounded responses. "
                    "For factual attribute questions: first call get_entity_attributes to discover "
                    "what attributes are available, then call lookup_entity_attribute with the best match. "
                    "For list/filter requests, call search_criteria. "
                    "For file retrieval requests, call retrieve_document_file to return doc_name/doc_path/original_local_path. "
                    "Prefer query tool for general questions before free-form answer generation."
                ),
                tools=[
                    self.query,
                    self.search_discovery,
                    self.search_criteria,
                    self.get_entity_attributes,
                    self.lookup_entity_attribute,
                    self.retrieve_document_file,
                    self.autonomous_query,
                    self.ingest_document,
                    self.ingest_information,
                    self.generate_document,
                    self.chat,
                ],
            )
        except Exception:
            self.logger.exception("Failed to build ADK agent")
            return None


def _parse_json_object(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # Shell-friendly fallback for payloads like {'k': 'v'}
    try:
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    raise ValueError("Expected a JSON object")


def _parse_json_array(raw: str) -> list[Any]:
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    try:
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass

    raise ValueError("Expected a JSON array")


def _parse_csv_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def run_cli() -> int:
    parser = argparse.ArgumentParser(description="IDMS interaction tools (query, chat, ingestion, actions, scheduler, monitor)")
    sub = parser.add_subparsers(dest="command", required=True)

    q = sub.add_parser("query", help="Run factual query against IDMS")
    q.add_argument("question")

    c = sub.add_parser("chat", help="Chat with optional grounding from query tool")
    c.add_argument("message")
    c.add_argument("--no-query-tool", action="store_true")

    auto = sub.add_parser("auto", help="Autonomous loop for retrieval + attribute lookup")
    auto.add_argument("question")
    auto.add_argument("--max-steps", type=int, default=4)

    d = sub.add_parser("discovery", help="Search Discovery Engine directly")
    d.add_argument("question")
    d.add_argument("--limit", type=int, default=8)

    i = sub.add_parser("ingest-doc", help="Ingest a local or gs:// document")
    i.add_argument("source")
    i.add_argument("--description", default="")
    i.add_argument("--tags", default="", help="Comma-separated tags")
    i.add_argument("--metadata-json", default="", help="JSON object")

    t = sub.add_parser("ingest-text", help="Ingest raw information text without document")
    t.add_argument("text")
    t.add_argument("--description", default="")
    t.add_argument("--tags", default="", help="Comma-separated tags")
    t.add_argument("--metadata-json", default="", help="JSON object")

    a = sub.add_parser("adk-agent", help="Create ADK agent and report availability")
    a.add_argument("--json", action="store_true")

    action = sub.add_parser("action", help="Execute SOLF-guarded workflow actions")
    action.add_argument("name", choices=[
        "create_task",
        "update_entity",
        "assign_task",
        "close_task",
        "validate_plan",
        "create_workflow_case",
        "record_approval",
        "create_compliance_action",
        "ingest_document",
        "ingest_information",
        "generate_document",
    ])
    action.add_argument("--payload-json", default="{}", help="JSON object payload for action")
    action.add_argument("--dry-run", action="store_true", help="Compose SOLF action/sql plan without executing")

    solf = sub.add_parser("solf", help="Execute a SOLF clause/predicate directly")
    solf.add_argument("clause_name", help="SOLF clause/predicate name")
    solf.add_argument("--payload-json", default="{}", help="JSON object used as single argument when --args-json is empty")
    solf.add_argument("--args-json", default="", help="Optional JSON array of positional arguments")

    wf = sub.add_parser("workflow-design", help="Iteratively design a workflow plan using LLM + SOLF context")
    wf.add_argument("request")
    wf.add_argument("--context-json", default="{}", help="Optional JSON object with internal rules/metadata context")
    wf.add_argument("--max-iterations", type=int, default=3)

    ix = sub.add_parser("interaction-resolve", help="Iteratively resolve complex query/interaction requests")
    ix.add_argument("request")
    ix.add_argument("--context-json", default="{}", help="Optional JSON object with internal rules/metadata context")
    ix.add_argument("--max-iterations", type=int, default=3)

    sch = sub.add_parser("scheduler", help="Control recurring autonomous jobs")
    sch.add_argument("name", choices=["start", "stop", "add_job", "remove_job", "pause_job", "resume_job", "list_jobs"])
    sch.add_argument("--payload-json", default="{}", help="JSON object payload for scheduler command")

    boot = sub.add_parser("bootstrap-db", help="Create DB schema tables for IDMS")
    boot.add_argument("--recreate", action="store_true", help="Drop and recreate known tables")

    sub.add_parser("repair-schema", help="Repair known legacy schema drift without dropping tables")

    st = sub.add_parser("state", help="Workflow state machine operations")
    st.add_argument("name", choices=["get", "can", "transition", "history", "maintenance"])
    st.add_argument("--entity-type", default="workflow_case", choices=["workflow_case", "compliance_action", "project_task"])
    st.add_argument("--entity-id", default=0, type=int)
    st.add_argument("--to-state", default="")
    st.add_argument("--event", default="manual_transition")
    st.add_argument("--actor-ref", default="system")
    st.add_argument("--metadata-json", default="{}")
    st.add_argument("--limit", type=int, default=20)
    st.add_argument("--max-rows", type=int, default=200)

    mon = sub.add_parser("monitor", help="Workflow operational monitoring, reliability alerts, incidents, and simulation")
    mon.add_argument(
        "name",
        choices=[
            "stats",
            "health",
            "snapshot",
            "history",
            "alerts",
            "reconcile",
            "incidents",
            "incident",
            "health-summary",
            "incident-summary",
            "incidents-by-status",
            "metrics-timeline",
            "incident-trends",
            "metric-distribution",
            "sla-compliance",
            "subscribe-notifications",
            "audit-trail",
            "compliance-status",
            "compliance-report",
            "create-scenario",
            "simulate",
            "budget-impact",
            "compare-scenarios",
            "scenarios",
            "scenario-details",
        ],
    )
    mon.add_argument("--window-hours", type=int, default=24)
    mon.add_argument("--label", default="")
    mon.add_argument("--limit", type=int, default=20)
    mon.add_argument("--status", default="open", choices=["open", "acknowledged", "resolved", "all"])
    mon.add_argument("--incident-id", type=int, default=0)
    mon.add_argument("--incident-action", default="", choices=["", "acknowledge", "resolve"])
    mon.add_argument("--actor-ref", default="system")
    mon.add_argument("--metric-key", default="")
    mon.add_argument("--hours", type=int, default=168)
    mon.add_argument("--days", type=int, default=7)
    mon.add_argument("--offset", type=int, default=0)
    mon.add_argument("--subscriber-ref", default="")
    mon.add_argument("--email", default="")
    mon.add_argument("--webhook-url", default="")
    mon.add_argument("--phone", default="")
    mon.add_argument("--entity-type", default="")
    mon.add_argument("--entity-id", type=int, default=0)
    mon.add_argument("--compliance-type", default="")
    mon.add_argument("--base-project-id", type=int, default=0)
    mon.add_argument("--scenario-name", default="")
    mon.add_argument("--scenario-type", default="what_if")
    mon.add_argument("--description", default="")
    mon.add_argument("--scenario-id", type=int, default=0)
    mon.add_argument("--scenario1-id", type=int, default=0)
    mon.add_argument("--scenario2-id", type=int, default=0)
    mon.add_argument("--project-id", type=int, default=0)
    mon.add_argument("--include-resource-check", action="store_true")
    mon.add_argument("--modifications-json", default="{}")

    args = parser.parse_args()
    tools = IDMSInteractionTools()

    if args.command == "query":
        print(json.dumps(tools.query(args.question), indent=2, ensure_ascii=False))
        return 0

    if args.command == "chat":
        result = tools.chat(args.message, use_query_tool=not args.no_query_tool)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "auto":
        result = tools.autonomous_query(args.question, max_steps=args.max_steps)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "discovery":
        result = tools.search_discovery(args.question, limit=args.limit)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "ingest-doc":
        result = tools.ingest_document(
            source=args.source,
            description=args.description,
            tags=_parse_csv_list(args.tags),
            metadata=_parse_json_object(args.metadata_json),
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "ingest-text":
        result = tools.ingest_information(
            information_text=args.text,
            description=args.description,
            tags=_parse_csv_list(args.tags),
            metadata=_parse_json_object(args.metadata_json),
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "adk-agent":
        agent = tools.build_adk_agent()
        payload = {
            "adk_available": bool(adk is not None),
            "agent_built": bool(agent is not None),
            "agent_name": getattr(agent, "name", None) if agent is not None else None,
        }
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(payload)
        return 0

    if args.command == "action":
        payload = _parse_json_object(args.payload_json)
        result = tools.preview_action_plan(args.name, payload) if args.dry_run else tools.perform_action(args.name, payload)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "solf":
        payload = _parse_json_object(args.payload_json)
        call_args = _parse_json_array(args.args_json) if str(args.args_json or "").strip() else []
        result = tools.evaluate_solf_clause(
            clause_name=args.clause_name,
            payload=payload,
            args=call_args,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "workflow-design":
        result = tools.design_workflow_interaction(
            request_text=args.request,
            context=_parse_json_object(args.context_json),
            max_iterations=args.max_iterations,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "interaction-resolve":
        result = tools.resolve_complex_interaction(
            request_text=args.request,
            context=_parse_json_object(args.context_json),
            max_iterations=args.max_iterations,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "scheduler":
        payload = _parse_json_object(args.payload_json)
        result = tools.scheduler_command(args.name, payload)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "bootstrap-db":
        result = tools.bootstrap_db(recreate=args.recreate)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "repair-schema":
        result = tools.repair_schema()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "state":
        payload = {
            "entity_type": args.entity_type,
            "entity_id": args.entity_id,
            "to_state": args.to_state,
            "event": args.event,
            "actor_ref": args.actor_ref,
            "metadata": _parse_json_object(args.metadata_json),
            "limit": args.limit,
            "max_rows": args.max_rows,
        }
        result = tools.state_command(args.name, payload)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "monitor":
        payload = {
            "window_hours": args.window_hours,
            "label": args.label,
            "limit": args.limit,
            "status": args.status,
            "incident_id": args.incident_id,
            "incident_action": args.incident_action,
            "actor_ref": args.actor_ref,
            "metric_key": args.metric_key,
            "hours": args.hours,
            "days": args.days,
            "offset": args.offset,
            "subscriber_ref": args.subscriber_ref,
            "email": args.email,
            "webhook_url": args.webhook_url,
            "phone": args.phone,
            "entity_type": args.entity_type,
            "entity_id": args.entity_id,
            "compliance_type": args.compliance_type,
            "base_project_id": args.base_project_id,
            "scenario_name": args.scenario_name,
            "scenario_type": args.scenario_type,
            "description": args.description,
            "scenario_id": args.scenario_id,
            "scenario1_id": args.scenario1_id,
            "scenario2_id": args.scenario2_id,
            "project_id": args.project_id,
            "include_resource_check": args.include_resource_check,
            "modifications": _parse_json_object(args.modifications_json),
        }
        result = tools.monitor_command(args.name, payload)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
