"""Public diagnostic contracts and durable task state."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartdata.conversation.models import now


class DiagnosticBudget(BaseModel):
    max_evidence_questions: int = 5
    max_adaptive_rounds: int = 2
    max_questions_per_round: int = 3
    max_observation_rows: int = 50


class EvidenceQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=1000)
    purpose: str = Field(min_length=1, max_length=200)
    priority: int = Field(default=1, ge=1)


class DiagnosticDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["query", "clarify", "stop"]
    questions: list[EvidenceQuestion] = Field(default_factory=list)
    clarification: str | None = None

    @model_validator(mode="after")
    def valid_action(self):
        if self.action == "query" and not self.questions:
            raise ValueError("query decision requires questions")
        if self.action == "clarify" and not self.clarification:
            raise ValueError("clarify decision requires clarification")
        if self.action != "query" and self.questions:
            raise ValueError("only query decision may contain questions")
        return self


class TaskStatus(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    WAITING_USER = "WAITING_USER"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class DiagnosticTask(BaseModel):
    id: str
    run_id: str
    round: int
    ordinal: int
    question: str
    purpose: str
    status: TaskStatus = TaskStatus.PLANNED
    attempt: int = 0
    ask_response_json: dict[str, Any] | None = None
    evidence_ref: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    retryable: bool = False
    revision: int = 0
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    completed_at: datetime | None = None


class DiagnosticCheckpoint(BaseModel):
    run_id: str
    current_round: int = 0
    questions_planned: int = 0
    questions_completed: int = 0
    remaining_budget: int = 0
    diagnostic_datasource_id: str | None = None
    pending_task_id: str | None = None
    verified_observation_refs: list[str] = Field(default_factory=list)
    revision: int = 0


class DiagnosticObservation(BaseModel):
    observation_id: str
    task_id: str
    question: str
    purpose: str
    status: Literal["verified", "unavailable"]
    reason: str | None = None
    safe_answer: str = ""
    result_shape: dict[str, Any] = Field(default_factory=dict)
    bounded_rows: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ref: str | None = None
    datasource_id: str | None = None
    scan_version: int | None = None
    observed_at: datetime = Field(default_factory=now)


class DiagnosticFinding(BaseModel):
    finding_id: str
    run_id: str
    target_summary: str
    statement: str
    metric: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(min_length=1)
    datasource_id: str
    scan_version: int | None = None
    observed_at: datetime = Field(default_factory=now)


class DiagnosticPlanningContext(BaseModel):
    question: str
    semantic_memory: dict[str, Any]
    observations: list[DiagnosticObservation]
    round: int
    remaining_budget: int


class DiagnosticSynthesisContext(BaseModel):
    question: str
    observations: list[DiagnosticObservation]
    unavailable: list[DiagnosticObservation] = Field(default_factory=list)
