from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

MAX_TEXT_LENGTH = 500


def normalize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<binary:{len(value)} bytes>"
    if isinstance(value, dict):
        return {str(key): normalize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_value(item) for item in value]
    text = str(value)
    if len(text) > MAX_TEXT_LENGTH:
        return f"{text[:MAX_TEXT_LENGTH]}…"
    return text
