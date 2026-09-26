from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from qaneris.contracts.query import (
    ExecutionEvidence,
    GroundedQueryPlan,
    GroundedQueryResult,
)
from qaneris.contracts.semantic import BusinessQuery


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    workspace_id: str = "default"
    datasource_id: str | None = None
    sql: str | None = None
    max_rows: int = Field(default=200, ge=1, le=1000)


class QueryPlanStep(BaseModel):
    id: str
    datasource_id: str
    dataset: str
    purpose: str
    query: str
    query_language: str = "sql"


class NormalizedResult(BaseModel):
    source: str
    dataset: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool = False


class AskStatus(StrEnum):
    COMPLETED = "completed"
    CLARIFICATION_REQUIRED = "clarification_required"
    FAILED = "failed"


class AskClarification(BaseModel):
    """A user-facing clarification with no graph or field identifiers."""

    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)
    actionable: bool = True


class ErrorDetail(BaseModel):
    """A stable machine code with a user-facing message.

    ``sheet`` / ``coordinate`` are the optional location of a deterministic Excel ingestion refusal.
    They name where a file must be fixed; they never carry a formula body or a stored cell value.
    """

    code: str
    message: str
    sheet: str | None = None
    coordinate: str | None = None


class AskResponse(BaseModel):
    question: str
    status: AskStatus = AskStatus.COMPLETED
    answer: str = ""
    business_query: BusinessQuery | None = None
    clarification: list[AskClarification] = Field(default_factory=list)
    plan: GroundedQueryPlan | list[QueryPlanStep] | None = None
    result: GroundedQueryResult | NormalizedResult | None = None
    evidence: ExecutionEvidence | None = None
    error: ErrorDetail | None = None
    analysis: dict[str, Any] = Field(default_factory=dict)
    recommended_questions: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorDetail
