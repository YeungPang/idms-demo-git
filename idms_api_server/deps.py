from __future__ import annotations

import json
from functools import lru_cache

from fastapi import HTTPException

from interaction import IDMSInteractionTools


@lru_cache(maxsize=1)
def get_tools() -> IDMSInteractionTools:
    return IDMSInteractionTools()


def parse_json_dict(raw: str, field_name: str) -> dict:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail=f"{field_name} must decode to a JSON object")
    return parsed


def normalize_tags(raw_tags: str) -> list[str]:
    return [part.strip() for part in str(raw_tags or "").split(",") if part.strip()]
