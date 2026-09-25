from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from qaneris.contracts.semantic import (
    BusinessFilter,
    BusinessObjective,
    BusinessQuery,
    ComparisonSpec,
    RankingSpec,
    SemanticCandidate,
)


class SemanticRetrievalPath(StrEnum):
    """Which recall path produced a retrieval result."""

    TRUSTED = "trusted"
    EXPLORATORY = "exploratory"


class RuleExtraction(BaseModel):
    question: str = Field(min_length=1)
    objective_hint: BusinessObjective | None = None
    explicit_dates: list[date] = Field(default_factory=list)
    time_expression: str | None = None
    filters: list[BusinessFilter] = Field(default_factory=list)
    comparison: ComparisonSpec | None = None
    ranking: RankingSpec | None = None
    numeric_values: list[int | float] = Field(default_factory=list)
    requested_datasource_id: str | None = None


class IntentConflict(BaseModel):
    field: str = Field(min_length=1)
    deterministic_value: Any
    model_value: Any
    resolution: str = "rule"


class ClarificationOption(BaseModel):
    option_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str | None = None


class ClarificationRequest(BaseModel):
    clarification_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    question: str = Field(min_length=1)
    options: list[ClarificationOption] = Field(default_factory=list)
    required: bool = True


class IntentUnderstandingResult(BaseModel):
    business_query: BusinessQuery
    rule_extraction: RuleExtraction
    conflicts: list[IntentConflict] = Field(default_factory=list)
    clarifications: list[ClarificationRequest] = Field(default_factory=list)
    requested_datasource_id: str | None = None

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarifications)


class SemanticRetrievalResult(BaseModel):
    business_query: BusinessQuery
    candidates: list[SemanticCandidate] = Field(default_factory=list)
    workspace_id: str = Field(default="default", min_length=1)
    requested_datasource_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    retrieval_path: SemanticRetrievalPath = SemanticRetrievalPath.TRUSTED
    #: Revision of the published enterprise data graph this read observed. Groundings and plans
    #: carry it downstream so a future execution validator can refuse running a plan against a
    #: rescanned graph.
    scan_version: int | None = None

    def candidates_for(self, asset_type: str) -> list[SemanticCandidate]:
        return [item for item in self.candidates if item.asset_type == asset_type]
