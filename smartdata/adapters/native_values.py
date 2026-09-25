"""Convert database-native values into bounded platform values."""

from __future__ import annotations

from typing import Any

from smartdata.common.values import normalize_value
from smartdata.security.redaction import redact_mapping


def normalize_native_value(value: Any) -> Any:
    return normalize_value(value)


def normalize_sample_value(value: Any) -> Any:
    return normalize_native_value(value)


def normalize_sample_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_native_value(row)
    return redact_mapping(normalized)
