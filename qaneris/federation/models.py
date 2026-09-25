"""Public, strictly validated federation contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from qaneris.conversation.models import now


class FederationBudget(BaseModel):
    max_sources: int = Field(default=5, ge=2, le=5)
    max_rows_per_source: int = Field(default=200, ge=1, le=1000)
    max_total_rows: int = Field(default=1000, ge=1, le=5000)
    max_intermediate_bytes: int = Field(default=1_000_000, ge=1, le=10_000_000)
    deadline_seconds: int = Field(default=120, ge=1, le=600)


class ExecutionScope(BaseModel):
    workspace_id: str
    allowed_datasource_ids: list[str]
    max_sources: int
    max_rows_per_source: int
    max_total_rows: int
    max_intermediate_bytes: int
    deadline: datetime
    allow_cross_source_join: bool = True


class SourceTaskDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datasource_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=1000)
    purpose: str = Field(min_length=1, max_length=200)
    expected_shape: Literal["scalar", "rows", "time_series"]


class MergePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal[
        "compare_scalars", "combine_scalars", "union_rows", "align_time_series", "keyed_join"
    ]
    input_task_ids: list[str] = Field(default_factory=list)
    mapping_id: str | None = None
    join_mode: Literal["inner", "left", "anti"] | None = None
    scalar_operation: Literal[
        "sum", "difference", "ratio", "percentage_change", "percentage_difference"
    ] | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class FederatedPlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_tasks: list[SourceTaskDraft]
    merge: MergePlan


class SourceTaskStatus(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WAITING_USER = "WAITING_USER"
    SKIPPED = "SKIPPED"


class SourceTask(SourceTaskDraft):
    task_id: str
    run_id: str
    plan_id: str
    ordinal: int
    status: SourceTaskStatus = SourceTaskStatus.PLANNED
    attempt: int = 0
    response_json: dict[str, Any] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    retryable: bool = False
    revision: int = 0


class FederatedPlan(BaseModel):
    plan_id: str
    run_id: str
    workspace_id: str
    question: str
    source_tasks: list[SourceTask]
    merge_plan: MergePlan
    execution_scope: ExecutionScope
    business_query: dict[str, Any]
    created_at: datetime = Field(default_factory=now)


class JoinMappingStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    STALE = "STALE"
    REJECTED = "REJECTED"


class JoinMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mapping_id: str
    workspace_id: str = Field(min_length=1)
    business_key: str = Field(min_length=1)
    left_datasource_id: str = Field(min_length=1)
    left_data_object_id: str = Field(min_length=1)
    left_field_path: str = Field(min_length=1)
    left_scan_version: int | None = None
    left_grain: str = Field(min_length=1)
    right_datasource_id: str = Field(min_length=1)
    right_data_object_id: str = Field(min_length=1)
    right_field_path: str = Field(min_length=1)
    right_scan_version: int | None = None
    right_grain: str = Field(min_length=1)
    cardinality: Literal["ONE_TO_ONE", "MANY_TO_ONE", "ONE_TO_MANY"]
    null_policy: Literal["REJECT", "DROP"]
    status: JoinMappingStatus = JoinMappingStatus.CANDIDATE
    source: str = "manual"
    confidence: float = Field(default=1.0, ge=0, le=1)
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class MergedResult(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    operation: str
    warnings: list[str] = Field(default_factory=list)


class FederatedEvidence(BaseModel):
    source_evidence: list[dict[str, Any]]
    merge_operation: str
    merge_mapping_id: str | None = None
    input_row_counts: dict[str, int]
    output_row_count: int
    truncated: bool
    warnings: list[str] = Field(default_factory=list)
