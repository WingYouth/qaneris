"""Persisted, user-visible conversation state."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from smartdata.contracts.ask_events import _safe_value


def now() -> datetime:
    return datetime.now(UTC)


class Conversation(BaseModel):
    conversation_id: str
    workspace_id: str = "default"
    title: str = ""
    datasource_scope: list[str] = Field(default_factory=list)
    status: Literal["active", "archived"] = "active"
    revision: int = 0
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

    @field_validator("title")
    @classmethod
    def sanitize_title(cls, value: str) -> str:
        return _safe_value(value)


class Message(BaseModel):
    message_id: str
    conversation_id: str
    role: Literal["user", "assistant"]
    content: str
    run_id: str | None = None
    message_kind: Literal["normal", "clarification"] = "normal"
    created_at: datetime = Field(default_factory=now)

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, value: str) -> str:
        return _safe_value(value)


class SemanticMemory(BaseModel):
    confirmed_business_query: dict[str, Any] | None = None
    selected_datasource_ids: list[str] = Field(default_factory=list)
    active_metrics: list[str] = Field(default_factory=list)
    active_dimensions: list[str] = Field(default_factory=list)
    active_time_expression: str | None = None
    active_filters: list[dict[str, Any]] = Field(default_factory=list)
    requested_output: list[str] = Field(default_factory=list)
    last_run_id: str | None = None
    last_result_shape: dict[str, Any] | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    diagnostic_findings: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=now)
