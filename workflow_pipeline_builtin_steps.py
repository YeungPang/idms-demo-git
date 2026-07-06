"""Built-in reusable python-binding helpers for workflow pipelines.

These helpers let workflow authors compose pause/resume behavior without
creating per-workflow custom Python functions.
"""

from __future__ import annotations

from typing import Any

from workflow_pipeline_executor import PipelineStepPauseRequired


def _to_text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def require_fields(context: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Pause when one or more required fields are missing from context.

    Config:
      - required_fields: list[str] (required)
      - prompt: str (optional)
      - missing_data_desc: str (optional)
      - required_doc_types: list[str] (optional)
    """
    required_fields = _to_text_list(config.get("required_fields"))
    missing = [key for key in required_fields if context.get(key) in (None, "", [])]

    if missing:
        prompt = str(config.get("prompt") or "")
        if not prompt:
            prompt = f"Please provide required fields: {', '.join(missing)}"

        missing_desc = str(config.get("missing_data_desc") or "")
        if not missing_desc:
            missing_desc = f"Missing required fields: {', '.join(missing)}"

        required_doc_types = _to_text_list(config.get("required_doc_types"))

        raise PipelineStepPauseRequired(
            reason="missing_data",
            prompt=prompt,
            missing_data_desc=missing_desc,
            required_doc_types=required_doc_types,
        )

    return {
        "validation_passed": True,
        "validated_fields": required_fields,
        "missing_fields": [],
    }


def require_user_confirmation(context: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Pause until an explicit confirmation value is provided.

    Config:
      - confirmation_key: str (default: "confirmed")
      - accepted_values: list[str] (default: ["yes", "true", "1", "approved"])
      - prompt: str (optional)
    """
    confirmation_key = str(config.get("confirmation_key") or "confirmed").strip() or "confirmed"
    accepted_values = {item.lower() for item in _to_text_list(config.get("accepted_values"))}
    if not accepted_values:
        accepted_values = {"yes", "true", "1", "approved"}

    value = context.get(confirmation_key)
    normalized = str(value).strip().lower() if value is not None else ""

    if normalized not in accepted_values:
        prompt = str(config.get("prompt") or "")
        if not prompt:
            prompt = "Please confirm to continue (set confirmed=yes)."
        raise PipelineStepPauseRequired(
            reason="user_interaction",
            prompt=prompt,
            missing_data_desc=f"Awaiting confirmation in field '{confirmation_key}'",
            required_doc_types=[],
        )

    return {
        "confirmation_passed": True,
        "confirmation_key": confirmation_key,
        "confirmation_value": value,
    }


def require_document_refs(context: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Pause until at least one document reference is available.

    This helper checks context keys used by resume flow:
      - _doc_refs
      - _resumed_doc_refs
      - doc_refs

    Config:
      - min_count: int (default: 1)
      - required_doc_types: list[str] (optional)
      - prompt: str (optional)
      - missing_data_desc: str (optional)
    """
    min_count = int(config.get("min_count") or 1)

    refs: list[Any] = []
    for key in ("_doc_refs", "_resumed_doc_refs", "doc_refs"):
        value = context.get(key)
        if isinstance(value, list):
            refs.extend(value)

    # De-duplicate while preserving order
    seen: set[str] = set()
    deduped: list[Any] = []
    for item in refs:
        marker = str(item)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)

    if len(deduped) < max(1, min_count):
        required_doc_types = _to_text_list(config.get("required_doc_types"))
        prompt = str(config.get("prompt") or "")
        if not prompt:
            prompt = "Please ingest the required document(s), then resume this workflow run."

        missing_desc = str(config.get("missing_data_desc") or "")
        if not missing_desc:
            missing_desc = f"Need at least {max(1, min_count)} document reference(s) before continuing"

        raise PipelineStepPauseRequired(
            reason="missing_data",
            prompt=prompt,
            missing_data_desc=missing_desc,
            required_doc_types=required_doc_types,
        )

    return {
        "document_check_passed": True,
        "doc_refs": deduped,
        "doc_ref_count": len(deduped),
    }


def require_clarification(context: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Pause until user clarification note and/or sufficient evidence docs are provided.

    Config:
      - note_min_length: int (default: 20)
      - require_note: bool (default: true)
      - min_doc_count: int (default: 0)
      - required_doc_types: list[str] (optional)
      - prompt: str (optional)
      - missing_data_desc: str (optional)
    """
    require_note = bool(config.get("require_note", True))
    note_min_length = int(config.get("note_min_length") or 20)
    min_doc_count = int(config.get("min_doc_count") or 0)

    latest = context.get("clarification_latest") if isinstance(context.get("clarification_latest"), dict) else {}
    note_text = str(latest.get("note_text") or "").strip()

    refs: list[Any] = []
    for key in ("_doc_refs", "_resumed_doc_refs", "doc_refs"):
        value = context.get(key)
        if isinstance(value, list):
            refs.extend(value)
    if isinstance(latest.get("doc_refs"), list):
        refs.extend(latest.get("doc_refs") or [])

    seen: set[str] = set()
    deduped_refs: list[Any] = []
    for item in refs:
        marker = str(item)
        if marker in seen:
            continue
        seen.add(marker)
        deduped_refs.append(item)

    has_note = (len(note_text) >= max(1, note_min_length)) if require_note else True
    has_docs = len(deduped_refs) >= max(0, min_doc_count)

    if not has_note or not has_docs:
        required_doc_types = _to_text_list(config.get("required_doc_types"))
        prompt = str(config.get("prompt") or "").strip()
        if not prompt:
            prompt = "Please provide clarification details and supporting documents, then resume this workflow run."

        missing_desc = str(config.get("missing_data_desc") or "").strip()
        if not missing_desc:
            reasons: list[str] = []
            if not has_note:
                reasons.append(f"clarification note length < {max(1, note_min_length)}")
            if not has_docs:
                reasons.append(f"document refs < {max(0, min_doc_count)}")
            missing_desc = "Missing clarification requirements: " + ", ".join(reasons)

        raise PipelineStepPauseRequired(
            reason="missing_data",
            prompt=prompt,
            missing_data_desc=missing_desc,
            required_doc_types=required_doc_types,
        )

    return {
        "clarification_check_passed": True,
        "clarification_note_length": len(note_text),
        "clarification_issue_flags": list(latest.get("issue_flags") or []),
        "clarification_amount_candidates": list(latest.get("amount_candidates") or []),
        "doc_ref_count": len(deduped_refs),
    }


def require_policy_approval_on_flags(context: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Require explicit approval when configured clarification flags are present.

    Config:
      - flag_values: list[str] (default common high-risk clarification flags)
      - approval_key: str (default: policy_approved)
      - accepted_values: list[str] (default: yes/approved/true/1)
      - prompt: str (optional)
      - required_doc_types: list[str] (optional)
    """
    latest = context.get("clarification_latest") if isinstance(context.get("clarification_latest"), dict) else {}
    issue_flags = {str(item).strip().lower() for item in (latest.get("issue_flags") or []) if str(item).strip()}

    configured_flags = {
        str(item).strip().lower()
        for item in _to_text_list(config.get("flag_values"))
        if str(item).strip()
    }
    if not configured_flags:
        configured_flags = {
            "unreceipted_allowance",
            "personal_account_transfer",
            "amount_mismatch",
            "multi_receipt_allocation",
        }

    triggered_flags = sorted([flag for flag in issue_flags if flag in configured_flags])
    if not triggered_flags:
        return {
            "policy_gate_passed": True,
            "policy_gate_required": False,
            "triggered_flags": [],
        }

    approval_key = str(config.get("approval_key") or "policy_approved").strip() or "policy_approved"
    accepted_values = {item.lower() for item in _to_text_list(config.get("accepted_values"))}
    if not accepted_values:
        accepted_values = {"yes", "approved", "true", "1"}

    value = context.get(approval_key)
    normalized = str(value).strip().lower() if value is not None else ""

    if normalized not in accepted_values:
        prompt = str(config.get("prompt") or "").strip()
        if not prompt:
            prompt = (
                "Policy approval required due to clarification risk flags: "
                + ", ".join(triggered_flags)
                + f". Please set {approval_key}=yes and resume."
            )

        required_doc_types = _to_text_list(config.get("required_doc_types"))
        raise PipelineStepPauseRequired(
            reason="approval_required",
            prompt=prompt,
            missing_data_desc=(
                f"Awaiting policy approval in field '{approval_key}' for flags: " + ", ".join(triggered_flags)
            ),
            required_doc_types=required_doc_types,
        )

    return {
        "policy_gate_passed": True,
        "policy_gate_required": True,
        "policy_approval_key": approval_key,
        "policy_approval_value": value,
        "triggered_flags": triggered_flags,
    }


def invoke_solf_action(context: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Invoke a SOLF-callable action function from solf_function.py.

    Config:
      - action: function name in solf_function.py
      - payload: dict merged with context (optional)
      - save_as: key for storing result in returned payload (optional)
      - fail_on_error: bool (default true)
    """
    action = str(config.get("action") or "").strip()
    if not action:
        raise ValueError("invoke_solf_action requires config.action")

    import solf_function

    fn = getattr(solf_function, action, None)
    if fn is None or not callable(fn):
        raise ValueError(f"Unsupported SOLF action: {action}")

    payload = config.get("payload") if isinstance(config.get("payload"), dict) else {}
    merged_payload = dict(context)
    merged_payload.update(payload)
    merged_payload["context"] = dict(context)

    result = fn(merged_payload)

    fail_on_error = bool(config.get("fail_on_error", True))
    if fail_on_error and isinstance(result, dict) and result.get("ok") is False:
        raise ValueError(str(result.get("error") or f"action {action} failed"))

    save_as = str(config.get("save_as") or "action_result").strip() or "action_result"
    return {
        save_as: result,
        "last_action": action,
    }
