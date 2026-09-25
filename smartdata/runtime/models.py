"""Durable run checkpoints and public events."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from smartdata.contracts.ask_events import _safe_value
from smartdata.conversation.models import now


class RunStatus(StrEnum):
    CREATED = "CREATED"
    CONTEXTUALIZING = "CONTEXTUALIZING"
    DISCOVERING = "DISCOVERING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    ANSWERING = "ANSWERING"
    WAITING_USER = "WAITING_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"


class Run(BaseModel):
    run_id: str
    conversation_id: str
    user_message_id: str
    workspace_id: str
    status: RunStatus = RunStatus.CREATED
    current_stage: str = "created"
    revision: int = 0
    attempt: int = 1
    cancel_requested: bool = False
    resolved_question: str | None = None
    response_json: dict[str, Any] | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    retryable: bool = False
    max_rows: int = 200
    client_request_id: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    completed_at: datetime | None = None


class RunEvent(BaseModel):
    run_id: str
    sequence: int
    event_type: str
    occurred_at: datetime = Field(default_factory=now)
    public_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("public_payload", mode="before")
    @classmethod
    def sanitize(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _safe_value(value)
