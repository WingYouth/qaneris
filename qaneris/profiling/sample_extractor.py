from __future__ import annotations

import json
from typing import Any

from qaneris.common.values import normalize_value
from qaneris.security.redaction import REDACTED_VALUE, is_sensitive_field


def extract_field_samples(path: str, values: list[Any], limit: int) -> list[Any]:
    if limit <= 0:
        return []
    if is_sensitive_field(path):
        return [REDACTED_VALUE] if values else []
    samples: list[Any] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_value(value)
        identity = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        if identity in seen:
            continue
        seen.add(identity)
        samples.append(normalized)
        if len(samples) >= limit:
            break
    return samples
