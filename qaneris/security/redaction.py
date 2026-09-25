from __future__ import annotations

from typing import Any

REDACTED_VALUE = "<redacted>"

SENSITIVE_FIELD_TOKENS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "private_key",
    "phone",
    "mobile",
    "email",
    "身份证",
    "手机号",
    "邮箱",
)


def is_sensitive_field(name: str) -> bool:
    return any(token in name.lower() for token in SENSITIVE_FIELD_TOKENS)


def redact_value(value: Any, field_name: str = "") -> Any:
    if field_name and is_sensitive_field(field_name):
        return REDACTED_VALUE
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key): redact_value(item, str(key)) for key, item in value.items()}
