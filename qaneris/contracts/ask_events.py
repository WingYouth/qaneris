"""Public, serializable progress events for the unified Ask pipeline."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from qaneris.common.redaction import SecretRedactor


class AskEventType(StrEnum):
    """Stable event names emitted by :meth:`QanerisService.ask_stream`."""

    ACCEPTED = "accepted"
    INTENT_READY = "intent_ready"
    RETRIEVAL_READY = "retrieval_ready"
    GROUNDING_READY = "grounding_ready"
    CLARIFICATION_REQUIRED = "clarification_required"
    PLAN_READY = "plan_ready"
    QUERY_READY = "query_ready"
    EXECUTION_STARTED = "execution_started"
    RESULT_READY = "result_ready"
    ERROR = "error"
    DONE = "done"


_PRIVATE_REASONING_KEYS = {
    "chain_of_thought",
    "reasoning",
    "reasoning_content",
    "thoughts",
}
_CREDENTIAL_KEYS = {
    "api_key",
    "certificate",
    "certificate_pem",
    "client_secret",
    "connection_string",
    "credential",
    "credentials",
    "password",
    "private_key",
    "secret",
    "token",
}
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|private[_-]?key)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_API_KEY_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_PEM_MATERIAL = re.compile(
    r"-----BEGIN [^-\r\n]+-----.*?-----END [^-\r\n]+-----",
    re.DOTALL,
)


def _normalized_key(value: object) -> str:
    return str(value).strip().casefold().replace("-", "_")


def _safe_value(value: Any) -> Any:
    """Remove private reasoning and credential-shaped values recursively.

    Application code constructs deliberately small payloads, but the contract enforces this final
    boundary as defense in depth. Environment-backed secrets and credentials embedded in URLs are
    redacted as well.
    """

    redactor = SecretRedactor.from_environment()

    def visit(item: Any, path: tuple[str, ...] = ()) -> Any:
        if isinstance(item, BaseModel):
            return visit(item.model_dump(mode="json"), path)
        if isinstance(item, dict):
            safe: dict[str, Any] = {}
            for key, child in item.items():
                name = str(key)
                normalized = _normalized_key(name)
                if normalized in _PRIVATE_REASONING_KEYS:
                    continue
                if normalized in _CREDENTIAL_KEYS or any(
                    normalized.endswith(f"_{marker}")
                    for marker in ("password", "secret", "token", "api_key", "private_key")
                ):
                    safe[name] = "<redacted>"
                    continue
                if normalized == "parameters" and "derivations" not in path:
                    safe[name] = "<redacted>"
                    continue
                safe[name] = visit(child, (*path, normalized))
            return safe
        if isinstance(item, (list, tuple)):
            return [visit(child, path) for child in item]
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, StrEnum):
            return item.value
        if isinstance(item, str):
            safe = redactor.text(item)
            safe = _PEM_MATERIAL.sub("<redacted>", safe)
            safe = _BEARER_TOKEN.sub("Bearer <redacted>", safe)
            safe = _API_KEY_TOKEN.sub("<redacted>", safe)
            return _SECRET_ASSIGNMENT.sub(r"\1=<redacted>", safe)
        return item

    return visit(value)


class AskEvent(BaseModel):
    """One public Ask progress event.

    ``payload`` is intentionally a JSON object instead of an internal domain model. Each event type
    has a small documented projection produced by the application service, allowing new internal
    fields to be added without leaking model reasoning, bound parameter values, connection data or
    database exception details.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: AskEventType
    sequence: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str = Field(min_length=1)

    @field_validator("payload", mode="before")
    @classmethod
    def protect_public_payload(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise TypeError("AskEvent payload must be a JSON object")
        return _safe_value(value)
