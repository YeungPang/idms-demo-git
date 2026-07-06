"""
Python function extensions for IDMS SOLF runtime.

The SOLF interpreter attempts to resolve unknown clause names against this module.
These helpers keep DB behavior generic while allowing SOLF clauses to orchestrate
CRUD actions.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

import object_db


LOGGER = logging.getLogger("idms.solf_function")
_ACTIVE_CONNECTION: Any | None = None


WORKFLOW_DOMAIN_ALLOWED_OPERATIONS: set[str] = {
    "db_validate_ledger_payload",
    "resolve_accounting_booking_company",
    "db_accounting_ingest",
    "db_accounting_delete",
    "db_hr_validate_payload",
    "db_hr_ingest",
    "db_hr_delete",
}


def get_allowed_workflow_domain_operations() -> list[str]:
    """Return sorted workflow_domain_operation names that are executable in runtime."""
    return sorted(WORKFLOW_DOMAIN_ALLOWED_OPERATIONS)


def set_connection(connection: Any) -> None:
    """Set shared DB connection for SOLF function calls during one ingest run."""
    global _ACTIVE_CONNECTION
    _ACTIVE_CONNECTION = connection


def clear_connection() -> None:
    """Clear shared DB connection after SOLF function calls complete."""
    global _ACTIVE_CONNECTION
    _ACTIVE_CONNECTION = None


def _connection_scope() -> tuple[Any, bool]:
    if _ACTIVE_CONNECTION is not None:
        return _ACTIVE_CONNECTION, False
    return object_db.get_connection(), True


def _normalize_entity_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("entity"), dict):
        payload = payload.get("entity")
    if not isinstance(payload, dict):
        raise TypeError("Entity payload must be a JSON object")
    return payload


def _build_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(payload.get("attributes") or {})
    metadata.update(
        {
            "entity_id": payload.get("entity_id"),
            "confidence": payload.get("confidence"),
            "doc_id": payload.get("doc_id"),
            "operation": payload.get("operation"),
            "effective_from": payload.get("effective_from"),
            "effective_until": payload.get("effective_until"),
            "recorded_on": payload.get("recorded_on"),
        }
    )
    return metadata


def _extract_temporal_context(payload: dict[str, Any]) -> tuple[Any, Any]:
    effective_from = payload.get("effective_from")
    effective_until = payload.get("effective_until")
    return effective_from, effective_until


def _coerce_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _extract_attr_value(value: Any) -> Any:
    if isinstance(value, dict):
        if "value" in value:
            return value.get("value")
        if "normalized_value" in value:
            return value.get("normalized_value")
    return value


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


def _clamp_confidence(value: Any, default: float = 0.5) -> float:
    conf = _to_float(value, default)
    if conf < 0.0:
        return 0.0
    if conf > 1.0:
        return 1.0
    return conf


def _is_invalidation_candidate(candidate: dict[str, Any]) -> bool:
    operation = str(candidate.get("operation") or "").strip().lower()
    status = str(candidate.get("status") or "").strip().lower()
    state = str(candidate.get("state") or "").strip().lower()
    invalidation_ops = {"delete", "invalidate", "invalidated", "supersede", "superseded", "obsolete", "revoke", "remove"}
    if operation in invalidation_ops or status in invalidation_ops or state in invalidation_ops:
        return True

    for flag_key in ("invalidated", "superseded", "revoked", "deleted", "is_invalid"):
        if _to_bool(candidate.get(flag_key)):
            return True
    return False


def _build_attr_candidates(attr_name: str, raw_value: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    default_confidence = _clamp_confidence(payload.get("confidence"), 0.5)
    default_operation = payload.get("operation")
    default_effective_from = payload.get("effective_from")
    default_effective_until = payload.get("effective_until")
    default_doc_id = payload.get("doc_id")

    def _candidate_dict(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            candidate_value = _extract_attr_value(item)
            provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
            candidate = {
                "value": candidate_value,
                "confidence": _clamp_confidence(item.get("confidence"), default_confidence),
                "effective_from": item.get("effective_from") or default_effective_from,
                "effective_until": item.get("effective_until") or default_effective_until,
                "operation": item.get("operation") or default_operation,
                "status": item.get("status"),
                "state": item.get("state"),
                "invalidated": item.get("invalidated"),
                "superseded": item.get("superseded"),
                "revoked": item.get("revoked"),
                "deleted": item.get("deleted"),
                "is_invalid": item.get("is_invalid"),
                "provenance": provenance,
                "doc_id": item.get("doc_id") or default_doc_id,
            }
            return candidate
        return {
            "value": item,
            "confidence": default_confidence,
            "effective_from": default_effective_from,
            "effective_until": default_effective_until,
            "operation": default_operation,
            "status": None,
            "state": None,
            "invalidated": False,
            "superseded": False,
            "revoked": False,
            "deleted": False,
            "is_invalid": False,
            "provenance": {},
            "doc_id": default_doc_id,
        }

    if isinstance(raw_value, dict) and isinstance(raw_value.get("candidates"), list):
        candidates = [_candidate_dict(item) for item in raw_value.get("candidates")]
    elif isinstance(raw_value, list):
        candidates = [_candidate_dict(item) for item in raw_value]
    else:
        candidates = [_candidate_dict(raw_value)]

    normalized_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_value = candidate.get("value")
        if candidate_value in (None, "", [], {}):
            continue
        normalized_value = _normalize_value(attr_name, candidate_value)
        if not normalized_value:
            continue
        candidate["normalized_value"] = normalized_value
        normalized_candidates.append(candidate)

    return normalized_candidates


def _fetch_active_temporal_attributes(
    connection: Any,
    object_id: int,
    attr_type: str,
    at_date: date,
) -> list[dict[str, Any]]:
    sql = """
    SELECT attr_id, attr_json, valid_from, valid_until
    FROM attribute
    WHERE src_id = %s
      AND src_type = 'object'
      AND attr_type = %s
      AND (valid_until IS NULL OR valid_until > %s)
    ORDER BY valid_from DESC, attr_id DESC
    """
    rows: list[dict[str, Any]] = []
    with connection.cursor() as cursor:
        cursor.execute(sql, (int(object_id), str(attr_type).strip().lower(), at_date))
        for row in cursor.fetchall() or []:
            attr_json = row[1]
            raw_value = _extract_attr_value(attr_json)
            rows.append(
                {
                    "attr_id": int(row[0]),
                    "attr_json": attr_json,
                    "raw_value": raw_value,
                    "normalized_value": _normalize_value(attr_type, raw_value),
                    "valid_from": row[2],
                    "valid_until": row[3],
                }
            )
    return rows


def _rank_attr_candidate(candidate: dict[str, Any]) -> float:
    score = _clamp_confidence(candidate.get("confidence"), 0.5)
    if candidate.get("effective_from") or candidate.get("effective_until"):
        score += 0.08

    provenance = candidate.get("provenance") if isinstance(candidate.get("provenance"), dict) else {}
    if provenance:
        score += min(0.12, 0.03 * len([k for k, v in provenance.items() if v not in (None, "", [], {})]))

    if _is_invalidation_candidate(candidate):
        score -= 1.0

    return score


def _select_winning_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid_candidates = [candidate for candidate in candidates if not _is_invalidation_candidate(candidate)]
    if not valid_candidates:
        return None

    scored = [(_rank_attr_candidate(candidate), candidate) for candidate in valid_candidates]
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _build_arbitrated_attr_payload(
    attr_name: str,
    winner: dict[str, Any],
    all_candidates: list[dict[str, Any]],
    existing_rows: list[dict[str, Any]],
    payload: dict[str, Any],
) -> dict[str, Any]:
    winner_value = winner.get("value")
    arbitration = {
        "version": 1,
        "strategy": "generic_temporal_conflict_arbitration",
        "winner_normalized": winner.get("normalized_value"),
        "winner_confidence": winner.get("confidence"),
        "input_candidates": len(all_candidates),
        "existing_active_rows": len(existing_rows),
        "loser_normalized_values": [
            c.get("normalized_value")
            for c in all_candidates
            if c.get("normalized_value") and c.get("normalized_value") != winner.get("normalized_value")
        ],
        "invalidation_candidates": [
            c.get("normalized_value")
            for c in all_candidates
            if c.get("normalized_value") and _is_invalidation_candidate(c)
        ],
        "doc_id": payload.get("doc_id"),
        "operation": payload.get("operation"),
    }

    if isinstance(winner_value, dict):
        out = dict(winner_value)
        out["arbitration"] = arbitration
        return out

    return {
        "value": winner_value,
        "confidence": winner.get("confidence"),
        "provenance": winner.get("provenance") if isinstance(winner.get("provenance"), dict) else {},
        "arbitration": arbitration,
    }


def _sync_temporal_attribute_with_arbitration(
    connection: Any,
    row: dict[str, Any],
    payload: dict[str, Any],
    attr_name: str,
    attr_value: Any,
    effective_from: Any,
    effective_until: Any,
) -> dict[str, Any] | None:
    normalized_attr_name = str(attr_name or "").strip().lower()
    if not normalized_attr_name:
        return None

    start_date = _coerce_date(effective_from) or date.today()
    existing_rows = _fetch_active_temporal_attributes(
        connection=connection,
        object_id=int(row["object_id"]),
        attr_type=normalized_attr_name,
        at_date=start_date,
    )
    candidates = _build_attr_candidates(normalized_attr_name, attr_value, payload)
    if not candidates:
        return None

    winner = _select_winning_candidate(candidates)
    if winner is None:
        # Explicit invalidation events with no replacement value: close existing facts only.
        for existing in existing_rows:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE attribute SET valid_until = %s WHERE attr_id = %s AND (valid_until IS NULL OR valid_until > %s)",
                    (start_date, int(existing["attr_id"]), start_date),
                )
        connection.commit()
        return {
            "attr_id": None,
            "src_id": int(row["object_id"]),
            "src_type": "object",
            "attr_type": normalized_attr_name,
            "valid_from": start_date,
            "valid_until": start_date,
            "attr_json": {
                "arbitration": {
                    "version": 1,
                    "strategy": "generic_temporal_conflict_arbitration",
                    "doc_id": payload.get("doc_id"),
                    "operation": payload.get("operation"),
                    "decision": "close_without_replacement",
                    "existing_closed": len(existing_rows),
                }
            },
            "closed_only": True,
        }

    winner_norm = winner.get("normalized_value")
    if winner_norm and any(existing.get("normalized_value") == winner_norm for existing in existing_rows):
        # Keep the existing temporal row when the winner is already active.
        return {
            "attr_id": next(
                int(existing["attr_id"]) for existing in existing_rows if existing.get("normalized_value") == winner_norm
            ),
            "src_id": int(row["object_id"]),
            "src_type": "object",
            "attr_type": normalized_attr_name,
            "valid_from": start_date,
            "valid_until": _coerce_date(effective_until),
            "attr_json": existing_rows[0].get("attr_json") if existing_rows else {},
            "unchanged": True,
            "arbitration": {
                "strategy": "generic_temporal_conflict_arbitration",
                "decision": "keep_existing_winner",
            },
        }

    attr_payload = _build_arbitrated_attr_payload(
        attr_name=normalized_attr_name,
        winner=winner,
        all_candidates=candidates,
        existing_rows=existing_rows,
        payload=payload,
    )
    return object_db.upsert_temporal_attribute(
        connection=connection,
        src_id=int(row["object_id"]),
        src_type="object",
        attr_type=normalized_attr_name,
        attr_json=attr_payload,
        valid_from=winner.get("effective_from") or effective_from,
        valid_until=winner.get("effective_until") or effective_until,
        close_previous=True,
    )


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _normalize_email(value: Any) -> str:
    return _normalize_text(value)


def _normalize_phone(value: Any) -> str:
    text = str(value or "")
    digits = re.sub(r"[^0-9+]", "", text)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    return digits


def _normalize_address(value: Any) -> str:
    text = _normalize_text(value)
    text = re.sub(r"\bstr\.\b", "strasse", text)
    text = re.sub(r"\bstr\b", "strasse", text)
    text = re.sub(r"\bstrasse\.\b", "strasse", text)
    text = re.sub(r"\bstras\b", "strasse", text)
    text = re.sub(r"\bst\.\b", "sankt", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_name(value: Any) -> str:
    text = _normalize_text(value)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    legal_suffixes = [
        " gmbh",
        " ag",
        " ltd",
        " llc",
        " sarl",
        " inc",
        " sa",
    ]
    for suffix in legal_suffixes:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return re.sub(r"\s+", " ", text).strip()


def _person_name_variants(value: Any) -> list[str]:
    """Produce search-query variants for a person name.

    Handles:
    - comma-reversed forms: "Example, Alex" → "Alex Example"
    - stripped punctuation: "B. Pang" → "B Pang" (for fuzzy matching)
    - initial-expansion queries: "B. Pang" → also searches "%Pang%" to let
      DB-level candidate scoring pick the right match via the scoring pipeline.
    """
    raw = str(value or "").strip()
    if not raw:
        return []

    variants: list[str] = []

    def _add(candidate: str) -> None:
        cleaned = re.sub(r"\s+", " ", str(candidate or "").strip())
        if cleaned and cleaned not in variants:
            variants.append(cleaned)

    _add(raw)

    # Title-stripped form ("Dr. Pang" → "Pang").
    title_stripped = re.sub(
        r"^(?:dr\.?|prof\.?|mr\.?|mrs\.?|ms\.?|herr|frau)\s+",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()
    if title_stripped and title_stripped != raw:
        _add(title_stripped)
        raw = title_stripped  # continue expansion from stripped form

    if "," in raw:
        last_name, first_names = [part.strip() for part in raw.split(",", 1)]
        if last_name and first_names:
            _add(f"{first_names} {last_name}")

    normalized = re.sub(r"[.,;:_\-]+", " ", raw)
    tokens = [token for token in normalized.split() if token]
    if len(tokens) >= 2:
        _add(" ".join(tokens))

    # Initial-expansion: if any token looks like an initial (single letter or
    # single letter followed by dot), also add a surname-only variant so the
    # DB candidate search can widen the net and scoring resolves the match.
    initial_pattern = re.compile(r"^[A-Za-z]\.?$")
    non_initial_tokens = [t for t in tokens if not initial_pattern.match(t)]
    if len(non_initial_tokens) < len(tokens) and non_initial_tokens:
        _add(" ".join(non_initial_tokens))

    return variants


def _person_name_key(value: Any) -> str:
    normalized = _normalize_name(value)
    if not normalized:
        return ""

    tokens = [token for token in normalized.split() if token]
    if not tokens:
        return ""

    # Ignore common honorifics so titles do not affect identity matching.
    stop_tokens = {"mr", "mrs", "ms", "dr", "prof", "sir", "herr", "frau"}
    filtered = [token for token in tokens if token not in stop_tokens]
    if not filtered:
        filtered = tokens

    return " ".join(sorted(filtered))


def _organization_name_key(value: Any) -> str:
    normalized = _normalize_name(value)
    if not normalized:
        return ""

    tokens = [token for token in normalized.split() if token]
    if not tokens:
        return ""

    # Ignore common legal-form remnants so "ExampleCo" and "ExampleCo AG" map together.
    stop_tokens = {
        "company",
        "co",
        "corp",
        "corporation",
        "group",
        "holding",
        "holdings",
        "international",
        "intl",
    }
    filtered = [token for token in tokens if token not in stop_tokens]
    if not filtered:
        filtered = tokens

    return " ".join(filtered)


def _find_non_person_existing_by_name_key(connection: Any, object_name: str) -> dict[str, Any] | None:
    target_key = _organization_name_key(object_name)
    if not target_key:
        return None

    candidates = object_db.search_objects(
        connection=connection,
        name_query=object_name,
        class_name=None,
        limit=20,
    )

    matches = [cand for cand in candidates if _organization_name_key(cand.get("object_name")) == target_key]
    if len(matches) == 1:
        return matches[0]
    return None


def _normalize_value(key: str, value: Any) -> str:
    lowered = str(key or "").strip().lower()
    if "email" in lowered:
        return _normalize_email(value)
    if "phone" in lowered or "mobile" in lowered or "tel" in lowered:
        return _normalize_phone(value)
    if "address" in lowered:
        return _normalize_address(value)
    if lowered in {"name", "legal_name", "company_name"}:
        return _normalize_name(value)
    return _normalize_text(value)


def _to_key_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    return []


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_resolution_policy(payload: dict[str, Any]) -> dict[str, Any]:
    policy = payload.get("resolution_policy") if isinstance(payload.get("resolution_policy"), dict) else {}
    threshold = _to_float(policy.get("threshold"), 0.72)
    low_threshold = _to_float(policy.get("low_threshold"), threshold)
    high_threshold = _to_float(policy.get("high_threshold"), threshold)
    return {
        "threshold": threshold,
        "low_threshold": low_threshold,
        "high_threshold": high_threshold,
        "margin": _to_float(policy.get("margin"), 0.08),
        "max_candidates": _to_int(policy.get("max_candidates"), 20),
        "name_similarity_weight": _to_float(policy.get("name_similarity_weight"), 0.35),
        "strong_match_score": _to_float(policy.get("strong_match_score"), 0.45),
        "medium_match_score": _to_float(policy.get("medium_match_score"), 0.20),
        "strong_keys": _to_key_list(policy.get("strong_keys")),
        "medium_keys": _to_key_list(policy.get("medium_keys")),
    }


def _resolve_candidate_attributes(connection: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    attrs = object_db.get_temporal_attributes_as_of(
        connection=connection,
        src_id=int(candidate.get("object_id")),
        src_type="object",
    )
    merged = dict(candidate.get("metadata") or {})
    merged.update(attrs)
    return merged


def _score_candidate(
    entity_name: str,
    entity_attrs: dict[str, Any],
    candidate: dict[str, Any],
    candidate_attrs: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    name_similarity = float(candidate.get("similarity") or 0.0)
    score = float(policy["name_similarity_weight"]) * name_similarity

    matched_strong: list[str] = []
    matched_medium: list[str] = []

    for key in policy["strong_keys"]:
        src_value = entity_attrs.get(key)
        tar_value = candidate_attrs.get(key)
        if src_value is None or tar_value is None:
            continue
        if _normalize_value(key, src_value) and _normalize_value(key, src_value) == _normalize_value(key, tar_value):
            score += float(policy["strong_match_score"])
            matched_strong.append(key)

    for key in policy["medium_keys"]:
        src_value = entity_attrs.get(key)
        tar_value = candidate_attrs.get(key)
        if src_value is None or tar_value is None:
            continue
        src_norm = _normalize_value(key, src_value)
        tar_norm = _normalize_value(key, tar_value)
        if not src_norm or not tar_norm:
            continue
        if src_norm == tar_norm:
            score += float(policy["medium_match_score"])
            matched_medium.append(key)

    explanation = {
        "candidate_object_id": int(candidate.get("object_id")),
        "candidate_name": candidate.get("object_name"),
        "name_similarity": name_similarity,
        "matched_strong": matched_strong,
        "matched_medium": matched_medium,
        "score": score,
        "name": entity_name,
    }
    return score, explanation


def _extract_key_matches(
    entity_attrs: dict[str, Any],
    candidate_attrs: dict[str, Any],
    strong_keys: list[str],
    medium_keys: list[str],
) -> tuple[list[str], list[str]]:
    matched_strong: list[str] = []
    matched_medium: list[str] = []

    for key in strong_keys:
        src_value = entity_attrs.get(key)
        tar_value = candidate_attrs.get(key)
        if src_value is None or tar_value is None:
            continue
        src_norm = _normalize_value(key, src_value)
        tar_norm = _normalize_value(key, tar_value)
        if src_norm and src_norm == tar_norm:
            matched_strong.append(key)

    for key in medium_keys:
        src_value = entity_attrs.get(key)
        tar_value = candidate_attrs.get(key)
        if src_value is None or tar_value is None:
            continue
        src_norm = _normalize_value(key, src_value)
        tar_norm = _normalize_value(key, tar_value)
        if src_norm and src_norm == tar_norm:
            matched_medium.append(key)

    return matched_strong, matched_medium


def _resolve_person_by_initials(
    name: str,
    candidates: list[dict[str, Any]],
) -> tuple[bool, dict[str, Any], list[dict[str, Any]]] | None:
    """Try to resolve a partial/initial person name against a candidate list.

    Returns:
        (True, chosen, [])       — exactly one candidate matched; use it.
        (False, first, matches)  — multiple candidates matched; surface as ambiguous.
        None                     — name does not contain initials or no surname; skip this stage.
    """
    raw = str(name or "").strip()
    # Strip title prefix.
    stripped = re.sub(
        r"^(?:dr\.?|prof\.?|mr\.?|mrs\.?|ms\.?|herr|frau)\s+",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()

    # Tokenise into name parts and detect whether the input contains initials.
    tokens = re.findall(r"[A-Za-zÄÖÜäöüß]+", stripped)
    initial_pat = re.compile(r"^[A-Za-z]$")
    initials = [t[0].upper() for t in tokens if initial_pat.match(t)]
    full_tokens = [t for t in tokens if not initial_pat.match(t)]

    # Need at least a surname token to use this stage.
    if not full_tokens:
        return None

    surname = full_tokens[-1].lower()
    given_tokens = full_tokens[:-1]
    has_initials = bool(initials)
    has_partial_given_name = bool(given_tokens)

    # If the input is just a surname (e.g. "Pang"), do not force a match here;
    # let the fuzzy scorer decide or surface ambiguity.
    if not has_initials and not has_partial_given_name:
        return None

    matching: list[dict[str, Any]] = []
    for candidate in candidates:
        cname = str(candidate.get("object_name") or "")
        # Remove comma-reversed form for token parsing.
        if "," in cname:
            parts = cname.split(",", 1)
            cname = f"{parts[1].strip()} {parts[0].strip()}"
        ctokens = re.findall(r"[A-Za-zÄÖÜäöüß]+", cname)
        if not ctokens:
            continue
        # Last token must match surname.
        if ctokens[-1].lower() != surname:
            continue
        given = ctokens[:-1]
        if not given:
            continue

        matched = False

        # Exact / prefix given-name match: "Alex Example" -> "Alex Morgan Example"
        if given_tokens:
            if len(given) >= len(given_tokens) and all(
                given[i].lower().startswith(given_tokens[i].lower())
                for i in range(len(given_tokens))
            ):
                matched = True

        # Initial expansion match: "B. Pang" or "B.K.S. Pang".
        if not matched and initials:
            if len(given) >= len(initials) and all(given[i][0].upper() == initials[i] for i in range(len(initials))):
                matched = True

        if matched:
            matching.append(candidate)

    if not matching:
        return None
    if len(matching) == 1:
        return True, matching[0], []
    # Prefer non-comma-reversed canonical form if there are duplicates (aliases).
    non_comma = [c for c in matching if "," not in str(c.get("object_name") or "")]
    if len(non_comma) == 1:
        return True, non_comma[0], []
    return False, matching[0], matching


def db_resolve_entity(payload: Any) -> dict[str, Any] | bool:
    entity = _normalize_entity_payload(payload)
    object_name = str(entity.get("object_name") or entity.get("name") or "").strip()
    class_name = str(entity.get("class_name") or "entity").strip().lower()
    attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    if not object_name:
        raise ValueError("object_name is required for db_resolve_entity")

    policy = _build_resolution_policy(entity)
    search_queries = [object_name]
    if class_name == "person":
        for variant in _person_name_variants(object_name):
            if variant not in search_queries:
                search_queries.append(variant)

    connection, owns_connection = _connection_scope()
    try:
        exact_cross_class = None
        if class_name != "person":
            exact_cross_class = object_db.get_object_instance(connection, object_name, class_name=None)

        merged_by_id: dict[int, dict[str, Any]] = {}
        for query in search_queries:
            query_candidates = object_db.search_objects(
                connection=connection,
                name_query=query,
                class_name=class_name,
                attributes=attributes,
                limit=int(policy["max_candidates"]),
            )
            for candidate in query_candidates:
                candidate_id = int(candidate.get("object_id"))
                previous = merged_by_id.get(candidate_id)
                if previous is None:
                    merged_by_id[candidate_id] = dict(candidate)
                    continue
                prev_sim = float(previous.get("similarity") or 0.0)
                new_sim = float(candidate.get("similarity") or 0.0)
                if new_sim > prev_sim:
                    merged = dict(previous)
                    merged.update(candidate)
                    merged_by_id[candidate_id] = merged

        candidates = list(merged_by_id.values())
        if not candidates:
            if exact_cross_class is not None:
                return {
                    "resolved": True,
                    "decision": "use_existing",
                    "stage": "exact_cross_class",
                    "object_id": int(exact_cross_class["object_id"]),
                    "object_name": exact_cross_class.get("object_name"),
                    "class_name": exact_cross_class.get("class_name"),
                    "score": None,
                    "second_score": None,
                    "evidence": {
                        "name": object_name,
                        "candidate_object_id": int(exact_cross_class["object_id"]),
                        "candidate_name": exact_cross_class.get("object_name"),
                        "candidate_class_name": exact_cross_class.get("class_name"),
                        "class_name_mismatch": class_name,
                    },
                    "policy": policy,
                }
            return {
                "resolved": False,
                "decision": "create_new",
                "reason": "no_candidates",
                "policy": policy,
            }

        # Stage 0: deterministic person alias resolution via normalized token key
        # (e.g., "Example, Alex" == "Alex Example").
        if class_name == "person":
            target_key = _person_name_key(object_name)
            if target_key:
                keyed_matches: list[dict[str, Any]] = []
                for candidate in candidates:
                    candidate_key = _person_name_key(candidate.get("object_name"))
                    if candidate_key and candidate_key == target_key:
                        keyed_matches.append(candidate)

                if keyed_matches:
                    keyed_matches.sort(
                        key=lambda item: (
                            1 if "," in str(item.get("object_name") or "") else 0,
                            int(item.get("name_rank") or 99),
                            -float(item.get("similarity") or 0.0),
                            str(item.get("object_name") or "").lower(),
                            int(item.get("object_id") or 0),
                        )
                    )
                    chosen = keyed_matches[0]
                    return {
                        "resolved": True,
                        "decision": "use_existing",
                        "stage": "name_key",
                        "object_id": int(chosen["object_id"]),
                        "object_name": chosen.get("object_name"),
                        "class_name": chosen.get("class_name"),
                        "score": None,
                        "second_score": None,
                        "evidence": {
                            "name": object_name,
                            "name_key": target_key,
                            "candidate_object_id": int(chosen["object_id"]),
                            "candidate_name": chosen.get("object_name"),
                            "candidate_count_same_key": len(keyed_matches),
                        },
                        "policy": policy,
                    }

        # Stage 0b: initial-name resolution for name-only person entities.
        # When no attributes are provided and the input name contains initials
        # (e.g. "B. Pang", "C.Y. Pang"), try to find a unique existing candidate
        # whose given-name initial(s) match and whose surname matches.
        # This prevents creating a duplicate entity for a partial-name reference.
        if class_name == "person":
            initial_match = _resolve_person_by_initials(object_name, candidates)
            if initial_match is not None:
                resolved, chosen, ambiguous_candidates = initial_match
                if resolved:
                    return {
                        "resolved": True,
                        "decision": "use_existing",
                        "stage": "initial_name",
                        "object_id": int(chosen["object_id"]),
                        "object_name": chosen.get("object_name"),
                        "class_name": chosen.get("class_name"),
                        "score": None,
                        "second_score": None,
                        "evidence": {
                            "name": object_name,
                            "candidate_name": chosen.get("object_name"),
                        },
                        "policy": policy,
                    }
                # More than one candidate matches the initials — surface as ambiguous.
                return {
                    "resolved": False,
                    "decision": "ambiguous",
                    "stage": "initial_name",
                    "reason": "multiple_initial_matches",
                    "object_id": int(ambiguous_candidates[0]["object_id"]),
                    "object_name": ambiguous_candidates[0].get("object_name"),
                    "class_name": ambiguous_candidates[0].get("class_name"),
                    "score": None,
                    "second_score": None,
                    "evidence": {
                        "name": object_name,
                        "matches": [c.get("object_name") for c in ambiguous_candidates],
                    },
                    "policy": policy,
                }

        # Stage 0c: non-person legal-name key resolution
        # (e.g., "ExampleCo" == "ExampleCo AG", but not "ExampleCo Software AG").
        if class_name != "person":
            target_key = _organization_name_key(object_name)
            if target_key:
                keyed_matches: list[dict[str, Any]] = []
                for candidate in candidates:
                    candidate_key = _organization_name_key(candidate.get("object_name"))
                    if candidate_key and candidate_key == target_key:
                        keyed_matches.append(candidate)

                if len(keyed_matches) == 1:
                    chosen = keyed_matches[0]
                    return {
                        "resolved": True,
                        "decision": "use_existing",
                        "stage": "name_key_org",
                        "object_id": int(chosen["object_id"]),
                        "object_name": chosen.get("object_name"),
                        "class_name": chosen.get("class_name"),
                        "score": None,
                        "second_score": None,
                        "evidence": {
                            "name": object_name,
                            "name_key": target_key,
                            "candidate_object_id": int(chosen["object_id"]),
                            "candidate_name": chosen.get("object_name"),
                        },
                        "policy": policy,
                    }
                if len(keyed_matches) > 1:
                    keyed_matches.sort(
                        key=lambda item: (
                            int(item.get("name_rank") or 99),
                            -float(item.get("similarity") or 0.0),
                            str(item.get("object_name") or "").lower(),
                            int(item.get("object_id") or 0),
                        )
                    )
                    chosen = keyed_matches[0]
                    return {
                        "resolved": False,
                        "decision": "ambiguous",
                        "stage": "name_key_org",
                        "reason": "multiple_name_key_matches",
                        "object_id": int(chosen["object_id"]),
                        "object_name": chosen.get("object_name"),
                        "class_name": chosen.get("class_name"),
                        "score": None,
                        "second_score": None,
                        "evidence": {
                            "name": object_name,
                            "name_key": target_key,
                            "matches": [c.get("object_name") for c in keyed_matches],
                        },
                        "policy": policy,
                    }

        scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        strong_matches: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for candidate in candidates:
            candidate_attrs = _resolve_candidate_attributes(connection, candidate)
            score, explanation = _score_candidate(object_name, attributes, candidate, candidate_attrs, policy)
            scored.append((score, candidate, explanation))

            matched_strong, _ = _extract_key_matches(
                entity_attrs=attributes,
                candidate_attrs=candidate_attrs,
                strong_keys=policy["strong_keys"],
                medium_keys=policy["medium_keys"],
            )
            if matched_strong:
                strong_matches.append((len(matched_strong), candidate, explanation))

        # Stage 1: deterministic resolution from strong identifiers.
        if len(strong_matches) == 1:
            _, best_candidate, best_expl = strong_matches[0]
            return {
                "resolved": True,
                "decision": "use_existing",
                "stage": "strong_keys",
                "object_id": int(best_candidate["object_id"]),
                "object_name": best_candidate.get("object_name"),
                "class_name": best_candidate.get("class_name"),
                "score": None,
                "second_score": None,
                "evidence": best_expl,
                "policy": policy,
            }

        if len(strong_matches) > 1:
            strong_matches.sort(key=lambda item: item[0], reverse=True)
            top_count = strong_matches[0][0]
            top_candidates = [item for item in strong_matches if item[0] == top_count]

            if len(top_candidates) == 1:
                _, best_candidate, best_expl = top_candidates[0]
                return {
                    "resolved": True,
                    "decision": "use_existing",
                    "stage": "strong_keys",
                    "object_id": int(best_candidate["object_id"]),
                    "object_name": best_candidate.get("object_name"),
                    "class_name": best_candidate.get("class_name"),
                    "score": None,
                    "second_score": None,
                    "evidence": best_expl,
                    "policy": policy,
                }

            best_candidate = top_candidates[0][1]
            best_expl = top_candidates[0][2]
            return {
                "resolved": False,
                "decision": "ambiguous",
                "stage": "strong_keys",
                "reason": "conflicting_strong_keys",
                "object_id": int(best_candidate["object_id"]),
                "object_name": best_candidate.get("object_name"),
                "class_name": best_candidate.get("class_name"),
                "score": None,
                "second_score": None,
                "evidence": best_expl,
                "policy": policy,
            }

        # Stage 2: weighted fuzzy scoring.
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_candidate, best_expl = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else -1.0

        low_threshold = float(policy["low_threshold"])
        high_threshold = float(policy["high_threshold"])
        margin = float(policy["margin"])
        if best_score >= high_threshold and (best_score - second_score) >= margin:
            return {
                "resolved": True,
                "decision": "use_existing",
                "stage": "fuzzy",
                "object_id": int(best_candidate["object_id"]),
                "object_name": best_candidate.get("object_name"),
                "class_name": best_candidate.get("class_name"),
                "score": best_score,
                "second_score": second_score,
                "evidence": best_expl,
                "policy": policy,
            }

        return {
            "resolved": False,
            "decision": "create_new" if best_score < low_threshold else "ambiguous",
            "stage": "fuzzy",
            "object_id": int(best_candidate["object_id"]),
            "object_name": best_candidate.get("object_name"),
            "class_name": best_candidate.get("class_name"),
            "score": best_score,
            "second_score": second_score,
            "evidence": best_expl,
            "policy": policy,
        }
    except Exception:
        LOGGER.exception("db_resolve_entity failed for %s/%s", class_name, object_name)
        return False
    finally:
        if owns_connection:
            connection.close()


def _sync_temporal_attributes(connection: Any, row: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
    if not attrs:
        return []

    effective_from, effective_until = _extract_temporal_context(payload)
    rows: list[dict[str, Any]] = []
    for attr_name, attr_value in attrs.items():
        resolved = _sync_temporal_attribute_with_arbitration(
            connection=connection,
            row=row,
            payload=payload,
            attr_name=str(attr_name),
            attr_value=attr_value,
            effective_from=effective_from,
            effective_until=effective_until,
        )
        if resolved is not None:
            rows.append(resolved)

    if rows:
        connection.commit()
    return rows


def db_object_exists(payload: Any) -> dict[str, Any] | bool:
    """Return existing object row or False when object does not exist."""
    entity = _normalize_entity_payload(payload)
    object_name = str(entity.get("object_name") or entity.get("name") or "").strip()
    class_name = str(entity.get("class_name") or "entity").strip().lower()
    if not object_name:
        raise ValueError("object_name is required for db_object_exists")

    connection, owns_connection = _connection_scope()
    try:
        existing = object_db.get_object_instance(connection, object_name, class_name)
        return existing if existing else False
    finally:
        if owns_connection:
            connection.close()


def db_ingest(payload: Any) -> dict[str, Any] | bool:
    """Generic ingest behavior: existence check, then create/update via upsert."""
    entity = _normalize_entity_payload(payload)
    object_name = str(entity.get("object_name") or entity.get("name") or "").strip()
    class_name = str(entity.get("class_name") or "entity").strip().lower()
    if not object_name:
        raise ValueError("object_name is required for db_ingest")

    connection, owns_connection = _connection_scope()
    try:
        resolved_object_id = entity.get("resolved_object_id")
        existing = None
        if resolved_object_id is not None:
            existing = object_db.get_object_instance_by_id(connection, int(resolved_object_id))
        if existing is None:
            existing = object_db.get_object_instance(connection, object_name, class_name)

        # Cross-class lookup: if the class-specific search found nothing, try any class.
        # This prevents creating a duplicate row just because the LLM labelled the same
        # entity as "company" in one doc and "legal_entity" or "entity" in another.
        if existing is None:
            existing = object_db.get_object_instance(connection, object_name, class_name=None)

        # For persons: also try the name-key match (handles "Pang, X" == "X Pang").
        if existing is None and class_name == "person":
            target_key = _person_name_key(object_name)
            if target_key:
                candidates = object_db.search_objects(
                    connection=connection,
                    name_query=object_name,
                    class_name="person",
                    limit=5,
                )
                for cand in candidates:
                    if _person_name_key(cand.get("object_name")) == target_key:
                        existing = cand
                        break

        if existing is None and class_name != "person":
            existing = _find_non_person_existing_by_name_key(connection, object_name)

        if existing is not None:
            row = object_db.update_object_instance_by_id(
                connection=connection,
                object_id=int(existing["object_id"]),
                metadata=_build_metadata(entity),
            )
        else:
            row = object_db.upsert_object_instance(
                connection=connection,
                object_name=object_name,
                class_name=class_name,
                metadata=_build_metadata(entity),
            )

        if not row:
            return False
        temporal_rows = _sync_temporal_attributes(connection, row, entity)
        row["ingest_action"] = "updated" if existing else "created"
        row["exists_before"] = bool(existing)
        row["temporal_attributes_written"] = len(temporal_rows)
        return row
    except Exception:
        LOGGER.exception("db_ingest failed for %s/%s", class_name, object_name)
        return False
    finally:
        if owns_connection:
            connection.close()


def db_update(payload: Any) -> dict[str, Any] | bool:
    """Generic update behavior: update existing object, fallback to upsert if missing."""
    entity = _normalize_entity_payload(payload)
    object_name = str(entity.get("object_name") or entity.get("name") or "").strip()
    class_name = str(entity.get("class_name") or "entity").strip().lower()
    if not object_name:
        raise ValueError("object_name is required for db_update")

    connection, owns_connection = _connection_scope()
    try:
        resolved_object_id = entity.get("resolved_object_id")
        existing = None
        if resolved_object_id is not None:
            existing = object_db.get_object_instance_by_id(connection, int(resolved_object_id))
        if existing is None:
            existing = object_db.get_object_instance(connection, object_name, class_name)

        if existing is None:
            existing = object_db.get_object_instance(connection, object_name, class_name=None)

        if existing is None and class_name == "person":
            target_key = _person_name_key(object_name)
            if target_key:
                candidates = object_db.search_objects(
                    connection=connection,
                    name_query=object_name,
                    class_name="person",
                    limit=5,
                )
                for cand in candidates:
                    if _person_name_key(cand.get("object_name")) == target_key:
                        existing = cand
                        break

        if existing is None and class_name != "person":
            existing = _find_non_person_existing_by_name_key(connection, object_name)

        if existing is not None:
            row = object_db.update_object_instance_by_id(
                connection=connection,
                object_id=int(existing["object_id"]),
                metadata=_build_metadata(entity),
            )
        else:
            row = object_db.upsert_object_instance(
                connection=connection,
                object_name=object_name,
                class_name=class_name,
                metadata=_build_metadata(entity),
            )

        if not row:
            return False
        temporal_rows = _sync_temporal_attributes(connection, row, entity)
        row["update_action"] = "updated" if existing else "created"
        row["exists_before"] = bool(existing)
        row["temporal_attributes_written"] = len(temporal_rows)
        return row
    except Exception:
        LOGGER.exception("db_update failed for %s/%s", class_name, object_name)
        return False
    finally:
        if owns_connection:
            connection.close()


def db_delete(payload: Any) -> dict[str, Any] | bool:
    """Generic delete behavior for object_instance by id or by name/class."""
    entity = _normalize_entity_payload(payload)
    object_id = entity.get("object_id")
    object_name = str(entity.get("object_name") or entity.get("name") or "").strip()
    class_name = str(entity.get("class_name") or "entity").strip().lower()

    if object_id is None and not object_name:
        raise ValueError("object_id or object_name is required for db_delete")

    connection, owns_connection = _connection_scope()
    try:
        with connection.cursor() as cursor:
            if object_id is not None:
                cursor.execute(
                    """
                    DELETE FROM object_instance
                    WHERE object_id = %s
                    RETURNING object_id, object_name, class_name
                    """,
                    (int(object_id),),
                )
            else:
                cursor.execute(
                    """
                    DELETE FROM object_instance
                    WHERE LOWER(object_name) = LOWER(%s)
                      AND class_name = %s
                    RETURNING object_id, object_name, class_name
                    """,
                    (object_name, class_name),
                )

            row = cursor.fetchone()
        connection.commit()

        if not row:
            return {"deleted": False, "object_name": object_name, "class_name": class_name}

        return {
            "deleted": True,
            "object_id": int(row[0]),
            "object_name": row[1],
            "class_name": row[2],
        }
    except Exception:
        LOGGER.exception("db_delete failed for %s/%s", class_name, object_name)
        return False
    finally:
        if owns_connection:
            connection.close()


def ingest(payload: Any) -> dict[str, Any] | bool:
    return db_ingest(payload)


def update(payload: Any) -> dict[str, Any] | bool:
    return db_update(payload)


def delete(payload: Any) -> dict[str, Any] | bool:
    return db_delete(payload)


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_identifier(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    if not _IDENTIFIER_PATTERN.match(text):
        raise ValueError(f"Invalid SQL identifier for {field_name}: {text}")
    return text


def _normalize_columns(raw_columns: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ValueError("columns must be a non-empty list")

    normalized: list[dict[str, Any]] = []
    allowed_sql_types = {
        "text",
        "varchar",
        "integer",
        "bigint",
        "numeric",
        "boolean",
        "date",
        "timestamp",
        "timestamptz",
        "jsonb",
    }

    for idx, item in enumerate(raw_columns, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"columns[{idx}] must be an object")

        name = _safe_identifier(item.get("name"), field_name=f"columns[{idx}].name")
        sql_type_raw = str(item.get("type") or "text").strip().lower()
        if sql_type_raw not in allowed_sql_types:
            raise ValueError(f"Unsupported SQL type for column '{name}': {sql_type_raw}")

        nullable = bool(item.get("nullable", True))
        default_value = item.get("default")

        normalized.append(
            {
                "name": name,
                "type": sql_type_raw,
                "nullable": nullable,
                "default": default_value,
            }
        )
    return normalized


class _PromptVarDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + str(key) + "}"


def _render_prompt_template(template: str, variables: dict[str, Any]) -> str:
    try:
        return str(template or "").format_map(_PromptVarDict(variables or {}))
    except Exception:
        return str(template or "")


def _load_domain_function_module() -> Any | None:
    try:
        import domain_function as _domain_function  # type: ignore

        return _domain_function
    except Exception:
        return None


def workflow_llm_call(payload: Any) -> dict[str, Any] | bool:
    """SOLF-callable LLM action with templated prompts.

    Expected payload fields:
      - prompt_template or prompt
      - variables/context (dict)
      - system_prompt (optional)
      - model (optional)
      - temperature (optional)
      - parse_json (bool, optional)
    """
    try:
        data = _normalize_entity_payload(payload)
        prompt_template = str(data.get("prompt_template") or data.get("prompt") or "").strip()
        if not prompt_template:
            raise ValueError("prompt_template or prompt is required")

        variables = data.get("variables") if isinstance(data.get("variables"), dict) else {}
        if not variables and isinstance(data.get("context"), dict):
            variables = dict(data.get("context") or {})

        final_prompt = _render_prompt_template(prompt_template, variables)
        system_prompt = str(data.get("system_prompt") or "").strip()
        if system_prompt:
            contents = [f"System:\n{system_prompt}", f"User:\n{final_prompt}"]
        else:
            contents = [final_prompt]

        model = str(data.get("model") or "").strip()
        temperature = float(data.get("temperature") if data.get("temperature") is not None else 0.1)
        parse_json = bool(data.get("parse_json", False))

        from llm_fallback import generate_content_with_openrouter_fallback  # lazy import

        response = generate_content_with_openrouter_fallback(
            primary_call=lambda: None,
            model=model,
            contents=contents,
            temperature=temperature,
            response_format={"type": "json_object"} if parse_json else None,
            call_name="workflow_llm_call",
            complexity=str(data.get("complexity") or "complex"),
        )

        text = str(getattr(response, "text", response) or "").strip()
        out: dict[str, Any] = {
            "ok": True,
            "prompt": final_prompt,
            "model": model,
            "text": text,
        }
        if parse_json:
            try:
                out["json"] = json.loads(text)
            except Exception:
                out["json_parse_error"] = True
        return out
    except Exception as exc:
        LOGGER.exception("workflow_llm_call failed")
        return {"ok": False, "error": str(exc)}


def workflow_generate_schema_and_solf(payload: Any) -> dict[str, Any] | bool:
    """Generate SQL DDL + SOLF class/input/output mapping artifacts.

    Payload:
      - schema_name (optional, default public)
      - table_name (required)
      - class_name (required)
      - columns: [{name,type,nullable?,default?}, ...] (required)
      - execute_ddl (optional bool)
      - register_class (optional bool, default true when execute_ddl=true)
    """
    try:
        data = _normalize_entity_payload(payload)
        schema_name = _safe_identifier(data.get("schema_name") or "public", field_name="schema_name")
        table_name = _safe_identifier(data.get("table_name"), field_name="table_name")
        class_name = _safe_identifier(data.get("class_name"), field_name="class_name").lower()
        columns = _normalize_columns(data.get("columns"))

        column_lines: list[str] = ["id BIGSERIAL PRIMARY KEY"]
        for col in columns:
            col_sql = f"{col['name']} {col['type'].upper()}"
            if not col["nullable"]:
                col_sql += " NOT NULL"
            if col["default"] is not None:
                default_repr = col["default"]
                if isinstance(default_repr, str) and not default_repr.upper().startswith("CURRENT_"):
                    default_repr = "'" + default_repr.replace("'", "''") + "'"
                col_sql += f" DEFAULT {default_repr}"
            column_lines.append(col_sql)

        ddl = (
            f"CREATE SCHEMA IF NOT EXISTS {schema_name};\n"
            f"CREATE TABLE IF NOT EXISTS {schema_name}.{table_name} (\n    "
            + ",\n    ".join(column_lines)
            + "\n);"
        )

        attribute_lines = "\n".join(f"    {col['name']}: Ø," for col in columns)
        solf_class = (
            f"{class_name} ≔ {{\n"
            f"    objectType: class,\n"
            f"{attribute_lines}\n"
            f"    ingest: ⦃db_ingest(_entity_payload)⦄,\n"
            f"    update: ⦃db_update(_entity_payload)⦄,\n"
            f"    delete: ⦃db_delete(_entity_payload)⦄\n"
            f"}}"
        )

        input_map_clause = (
            f"{class_name}_input_map(_payload) ⦃\n"
            f"    ↲({{ class_name: {class_name}, object_name: _payload[name], attributes: _payload }})\n"
            f"⦄"
        )
        output_map_clause = (
            f"{class_name}_output_map(_result) ⦃\n"
            f"    ↲({{ class_name: {class_name}, status: _result[status], object_id: _result[object_id], object_name: _result[object_name] }})\n"
            f"⦄"
        )

        executed = False
        class_registered = False
        execute_ddl = bool(data.get("execute_ddl", False))
        register_class = bool(data.get("register_class", execute_ddl))

        if execute_ddl:
            connection, owns_connection = _connection_scope()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(ddl)
                    executed = True

                    if register_class:
                        cursor.execute(
                            """
                            INSERT INTO object_class (class_name, metadata)
                            VALUES (%s, %s::jsonb)
                            ON CONFLICT (class_name)
                            DO UPDATE SET metadata = COALESCE(object_class.metadata, '{}'::jsonb) || EXCLUDED.metadata
                            """,
                            (
                                class_name,
                                json.dumps(
                                    {
                                        "generated_by": "workflow_generate_schema_and_solf",
                                        "schema_name": schema_name,
                                        "table_name": table_name,
                                        "columns": columns,
                                    },
                                    ensure_ascii=False,
                                ),
                            ),
                        )
                        class_registered = True
                connection.commit()
            finally:
                if owns_connection:
                    connection.close()

        return {
            "ok": True,
            "schema_name": schema_name,
            "table_name": table_name,
            "class_name": class_name,
            "columns": columns,
            "ddl": ddl,
            "solf_class": solf_class,
            "solf_input_map_clause": input_map_clause,
            "solf_output_map_clause": output_map_clause,
            "executed": executed,
            "class_registered": class_registered,
        }
    except Exception as exc:
        LOGGER.exception("workflow_generate_schema_and_solf failed")
        return {"ok": False, "error": str(exc)}


def workflow_domain_operation(payload: Any) -> dict[str, Any] | bool:
    """Run domain-specific operations callable from SOLF.

    Payload:
      - operation: one of
        db_validate_ledger_payload, resolve_accounting_booking_company,
        db_accounting_ingest, db_accounting_delete,
        db_hr_validate_payload, db_hr_ingest, db_hr_delete
      - operation_payload: dict (optional, defaults to payload)
    """
    try:
        data = _normalize_entity_payload(payload)
        operation = str(data.get("operation") or "").strip()
        if not operation:
            raise ValueError("operation is required")

        domain_module = _load_domain_function_module()
        if domain_module is None:
            raise RuntimeError("domain_function module is unavailable")

        if operation not in WORKFLOW_DOMAIN_ALLOWED_OPERATIONS:
            raise ValueError(f"Unsupported domain operation: {operation}")

        fn = getattr(domain_module, operation, None)
        if fn is None or not callable(fn):
            raise ValueError(f"Operation '{operation}' is not callable")

        op_payload = data.get("operation_payload") if isinstance(data.get("operation_payload"), dict) else data
        result = fn(op_payload)
        return {"ok": True, "operation": operation, "result": result}
    except Exception as exc:
        LOGGER.exception("workflow_domain_operation failed")
        return {"ok": False, "error": str(exc)}


def workflow_compose_operations(payload: Any) -> dict[str, Any] | bool:
    """Compose multiple operations to achieve a business goal.

    Payload:
      - context: dict (initial context)
      - operations: list of
        { action: 'workflow_llm_call'|'workflow_generate_schema_and_solf'|'workflow_domain_operation',
          payload: {...},
          save_as: 'key' (optional),
          merge_result_json: bool (optional) }
      - stop_on_error: bool (default true)
    """
    try:
        data = _normalize_entity_payload(payload)
        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        operations = data.get("operations") if isinstance(data.get("operations"), list) else []
        stop_on_error = bool(data.get("stop_on_error", True))

        if not operations:
            raise ValueError("operations list is required")

        action_registry = {
            "workflow_llm_call": workflow_llm_call,
            "workflow_generate_schema_and_solf": workflow_generate_schema_and_solf,
            "workflow_domain_operation": workflow_domain_operation,
        }

        step_results: list[dict[str, Any]] = []

        for index, item in enumerate(operations, start=1):
            if not isinstance(item, dict):
                step_result = {"index": index, "ok": False, "error": "operation item must be object"}
                step_results.append(step_result)
                if stop_on_error:
                    break
                continue

            action = str(item.get("action") or "").strip()
            fn = action_registry.get(action)
            if fn is None:
                step_result = {"index": index, "action": action, "ok": False, "error": "unsupported action"}
                step_results.append(step_result)
                if stop_on_error:
                    break
                continue

            op_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            merged_payload = dict(context)
            merged_payload.update(op_payload)
            merged_payload["context"] = dict(context)

            result = fn(merged_payload)
            ok = bool(isinstance(result, dict) and result.get("ok", True))
            step_result = {"index": index, "action": action, "ok": ok, "result": result}
            step_results.append(step_result)

            save_as = str(item.get("save_as") or "").strip()
            if save_as:
                context[save_as] = result

            if isinstance(result, dict) and bool(item.get("merge_result_json", False)):
                for key, value in result.items():
                    if key in {"ok", "error"}:
                        continue
                    context[key] = value

            if not ok and stop_on_error:
                break

        all_ok = all(bool(step.get("ok")) for step in step_results) if step_results else False
        return {
            "ok": all_ok,
            "context": context,
            "step_results": step_results,
        }
    except Exception as exc:
        LOGGER.exception("workflow_compose_operations failed")
        return {"ok": False, "error": str(exc)}
