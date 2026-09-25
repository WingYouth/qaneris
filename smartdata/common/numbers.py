"""Shared number and row normalization helpers."""

from __future__ import annotations

import math
from typing import Any

RESULT_DECIMAL_PLACES = 2


def normalize_number(value: Any) -> Any:
    if isinstance(value, float) and math.isfinite(value):
        return round(value, RESULT_DECIMAL_PLACES)
    return value


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{column: normalize_number(value) for column, value in row.items()} for row in rows]
