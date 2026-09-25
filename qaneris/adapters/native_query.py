"""Parsing helpers for non-SQL native query payloads."""

from __future__ import annotations

import json
from typing import Any


def parse_query_payload(query: str) -> dict[str, Any]:
    try:
        payload = json.loads(query)
    except json.JSONDecodeError as error:
        raise ValueError("Query must be a JSON object for this datasource") from error
    if not isinstance(payload, dict):
        raise TypeError("Query must be a JSON object for this datasource")
    return payload


def bounded_limit(value: Any, maximum: int) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = maximum
    return max(1, min(requested, maximum))
