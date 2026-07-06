from __future__ import annotations

import logging
from typing import Any
from datetime import datetime

from fastapi import APIRouter, HTTPException

from idms_api_server.deps import get_tools
from idms_api_server.routers.ingestion import _persist_note_document, _stable_note_identity
from idms_api_server.schemas import (
    ActionRequest,
    ChatRequest,
    QueryRequest,
    SolfClauseQueryRequest,
    WorkflowDesignRequest,
    ComplexInteractionRequest,
    ClarificationResponseRequest,
    SimilarEntityRequest,
    EntityAliasLinkRequest,
    EntityMergeRequest,
)
import ingest


router = APIRouter(prefix="/api", tags=["interaction"])
LOGGER = logging.getLogger("idms.api")


def _is_quota_exhausted_error(exc: Exception) -> bool:
    text = str(exc or "").upper()
    return "RESOURCE_EXHAUSTED" in text or ("429" in text and "GOOGLE" in text)


def _trim_log_text(value: Any, limit: int = 300) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


@router.post("/chat")
def chat(payload: ChatRequest) -> dict[str, Any]:
    LOGGER.info(
        "interaction_input_trace endpoint=chat use_query_tool=%s message=%s",
        payload.use_query_tool,
        _trim_log_text(payload.message, limit=500),
    )
    
    # Check if message is a note command
    note_data = ingest.parse_note_command_from_chat(payload.message)
    if note_data:
        try:
            tags = note_data.get("tags", [])
            note_date = datetime.now().strftime("%Y-%m-%d")
            # Build metadata
            metadata = {
                "note_title": note_data.get("title", ""),
                "note_date": note_date,
                "note_source": "chat",
                "note_content": note_data.get("content", ""),
                "stable_source_identity": _stable_note_identity(
                    title=note_data.get("title", ""),
                    note_date=note_date,
                    source="chat",
                    tags=tags,
                    content=note_data.get("content", ""),
                ),
            }
            if tags:
                metadata["note_tags"] = tags

            result = _persist_note_document(
                title=note_data.get("title", ""),
                content=note_data.get("content", ""),
                note_date=note_date,
                tags=tags,
                source="chat",
                metadata=metadata,
            )

            return {
                "success": True,
                "result": {
                    "type": "note_ingested",
                    "title": note_data.get("title"),
                    "tags": tags,
                    "run_id": result.get("run_id"),
                    "message": f"Note '{note_data.get('title')}' has been saved and indexed successfully."
                }
            }
        except Exception as exc:
            LOGGER.exception("Failed to ingest note from chat")
            raise HTTPException(status_code=500, detail=f"Failed to ingest note: {str(exc)}") from exc
    
    # Regular chat interaction
    try:
        result = get_tools().chat(payload.message, use_query_tool=payload.use_query_tool)
    except Exception as exc:
        LOGGER.exception("Chat failed")
        if _is_quota_exhausted_error(exc):
            raise HTTPException(
                status_code=429,
                detail="AI quota exhausted (Google Gemini RESOURCE_EXHAUSTED). Please retry later or increase quota.",
            ) from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"success": True, "result": result}


@router.post("/interaction/clarification/respond")
def clarification_respond(payload: ClarificationResponseRequest) -> dict[str, Any]:
    try:
        result = get_tools().respond_to_clarification(
            thread_id=int(payload.thread_id),
            user_response=str(payload.user_response or "").strip(),
        )
    except Exception as exc:
        LOGGER.exception("Clarification response failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"success": True, "result": result}


@router.get("/interaction/clarification/{thread_id}")
def clarification_get(thread_id: int) -> dict[str, Any]:
    try:
        result = get_tools().get_clarification_thread(int(thread_id))
    except Exception as exc:
        LOGGER.exception("Clarification thread retrieval failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail=f"Clarification thread {thread_id} not found")
    return {"success": True, "result": result}


@router.post("/interaction/entity/similar")
def interaction_entity_similar(payload: SimilarEntityRequest) -> dict[str, Any]:
    try:
        result = get_tools().suggest_similar_entities(
            entity_name=str(payload.entity_name or "").strip(),
            class_name=str(payload.class_name or "person").strip().lower() or "person",
            limit=int(payload.limit),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        LOGGER.exception("Entity similarity check failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"success": True, "result": result}


@router.post("/interaction/entity/alias/link")
def interaction_entity_alias_link(payload: EntityAliasLinkRequest) -> dict[str, Any]:
    try:
        result = get_tools().link_entity_alias(
            primary_object_id=int(payload.primary_object_id),
            alias_object_id=int(payload.alias_object_id),
            alias_type=str(payload.alias_type or "same_entity").strip().lower() or "same_entity",
            confidence=payload.confidence,
            metadata=payload.metadata if isinstance(payload.metadata, dict) else {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        LOGGER.exception("Entity alias link failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"success": True, "result": result}


@router.post("/interaction/entity/merge")
def interaction_entity_merge(payload: EntityMergeRequest) -> dict[str, Any]:
    try:
        result = get_tools().merge_entity_instances(
            canonical_object_id=int(payload.canonical_object_id),
            duplicate_object_id=int(payload.duplicate_object_id),
            merge_note=str(payload.merge_note or "").strip(),
            keep_alias_link=bool(payload.keep_alias_link),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        LOGGER.exception("Entity merge failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"success": True, "result": result}


@router.post("/query")
def query(payload: QueryRequest) -> dict[str, Any]:
    LOGGER.info(
        "interaction_input_trace endpoint=query max_steps=%s question=%s",
        payload.max_steps,
        _trim_log_text(payload.question, limit=500),
    )
    try:
        result = get_tools().autonomous_query(payload.question, max_steps=payload.max_steps)
    except Exception as exc:
        LOGGER.exception("Query failed")
        if _is_quota_exhausted_error(exc):
            raise HTTPException(
                status_code=429,
                detail="AI quota exhausted (Google Gemini RESOURCE_EXHAUSTED). Please retry later or increase quota.",
            ) from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"success": True, "result": result}


@router.post("/action")
def action(payload: ActionRequest) -> dict[str, Any]:
    try:
        result = get_tools().perform_action(payload.action_name, payload.payload)
    except Exception as exc:
        LOGGER.exception("Action execution failed")
        if _is_quota_exhausted_error(exc):
            raise HTTPException(
                status_code=429,
                detail="AI quota exhausted (Google Gemini RESOURCE_EXHAUSTED). Please retry later or increase quota.",
            ) from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"success": True, "result": result}


@router.post("/solf/predicate")
def solf_predicate(payload: SolfClauseQueryRequest) -> dict[str, Any]:
    try:
        result = get_tools().evaluate_solf_clause(
            clause_name=payload.clause_name,
            payload=payload.payload,
            args=payload.args,
        )
    except Exception as exc:
        LOGGER.exception("SOLF predicate execution failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"success": True, "result": result}


@router.post("/workflow/design")
def workflow_design(payload: WorkflowDesignRequest) -> dict[str, Any]:
    try:
        result = get_tools().design_workflow_interaction(
            request_text=payload.request,
            context=payload.context,
            max_iterations=payload.max_iterations,
        )
    except Exception as exc:
        LOGGER.exception("Workflow design failed")
        if _is_quota_exhausted_error(exc):
            raise HTTPException(
                status_code=429,
                detail="AI quota exhausted (Google Gemini RESOURCE_EXHAUSTED). Please retry later or increase quota.",
            ) from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"success": True, "result": result}


@router.post("/interaction/resolve")
def interaction_resolve(payload: ComplexInteractionRequest) -> dict[str, Any]:
    try:
        if payload.production_mode:
            result = get_tools().resolve_complex_interaction_production(
                request_text=payload.request,
                context=payload.context,
                max_iterations=payload.max_iterations,
                llm_model=payload.llm_model,
                max_retrieval_rounds=payload.max_retrieval_rounds,
                strict_provenance=payload.strict_provenance,
                run_calculation=payload.run_calculation,
                execute_generic_actions=payload.execute_generic_actions,
                verify_external_sources=payload.verify_external_sources,
                external_source_timeout_seconds=payload.external_source_timeout_seconds,
                enforce_approved_domains=payload.enforce_approved_domains,
                approved_source_domains=payload.approved_source_domains,
            )
        else:
            result = get_tools().resolve_complex_interaction(
                request_text=payload.request,
                context=payload.context,
                max_iterations=payload.max_iterations,
            )
    except Exception as exc:
        LOGGER.exception("Complex interaction resolution failed")
        if _is_quota_exhausted_error(exc):
            raise HTTPException(
                status_code=429,
                detail="AI quota exhausted (Google Gemini RESOURCE_EXHAUSTED). Please retry later or increase quota.",
            ) from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"success": True, "result": result}
