from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class CompletionCriterionType(StrEnum):
    EVIDENCE = "evidence"
    VALIDATION = "validation"
    OUTPUT = "output"
    SAFETY = "safety"


class CompletionCriterionStatus(StrEnum):
    PENDING = "pending"
    SATISFIED = "satisfied"
    FAILED = "failed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class TaskType(StrEnum):
    QUERY = "query"
    ANALYSIS = "analysis"
    RETRIEVAL = "retrieval"
    VISUALIZATION = "visualization"
    REPORT = "report"
    VALIDATE = "validate"
    CLARIFY = "clarify"


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    BUDGET_LIMITED = "budget_limited"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class AgentTraceEventType(StrEnum):
    GOAL_CREATED = "goal_created"
    PLAN_CREATED = "plan_created"
    TASK_READY = "task_ready"
    TASK_STARTED = "task_started"
    TOOL_CALLED = "tool_called"
    OBSERVATION_RECEIVED = "observation_received"
    TASK_VALIDATED = "task_validated"
    TASK_FAILED = "task_failed"
    REPLAN_CREATED = "replan_created"
    WAITING_USER = "waiting_user"
    GOAL_COMPLETED = "goal_completed"


class MemoryType(StrEnum):
    WORKING = "working"
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class SkillStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class PermissionContext(BaseModel):
    principal_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)
    allowed_datasource_ids: list[str] = Field(default_factory=list)
    allowed_data_object_ids: list[str] = Field(default_factory=list)
    allowed_field_paths: list[str] = Field(default_factory=list)
    allowed_metric_ids: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class BudgetSpec(BaseModel):
    max_tasks: int = Field(default=20, ge=1)
    max_tool_calls: int = Field(default=50, ge=0)
    max_retries: int = Field(default=3, ge=0)
    timeout_seconds: float = Field(default=300.0, gt=0)
    max_cost: float | None = Field(default=None, ge=0)


class BudgetUsage(BaseModel):
    tasks: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0)
    cost: float = Field(default=0.0, ge=0)


class GoalSpec(BaseModel):
    id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    scope: list[str] = Field(default_factory=list)
    workspace: str = Field(
        min_length=1, validation_alias=AliasChoices("workspace", "workspace_id")
    )
    requested_output: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    permission_context: PermissionContext

    @model_validator(mode="after")
    def validate_workspace_permission(self) -> GoalSpec:
        if self.workspace != self.permission_context.workspace_id:
            raise ValueError("goal and permission context must use the same workspace")
        return self


class CompletionCriterion(BaseModel):
    criterion_id: str = Field(min_length=1)
    type: CompletionCriterionType
    description: str = Field(min_length=1)
    required_evidence: list[str] = Field(default_factory=list)
    validator: str | None = None
    status: CompletionCriterionStatus = CompletionCriterionStatus.PENDING


class ArtifactRef(BaseModel):
    artifact_id: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    media_type: str | None = None
    checksum: str | None = None
    summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceRef(BaseModel):
    evidence_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    artifact_ref: ArtifactRef | None = None
    query_trace_ref: str | None = None
    agent_trace_ref: str | None = None
    observed_at: datetime | None = None
    freshness_seconds: int | None = Field(default=None, ge=0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_reference(self) -> EvidenceRef:
        if not (self.artifact_ref or self.query_trace_ref or self.agent_trace_ref):
            raise ValueError("evidence requires an artifact, query trace, or agent trace reference")
        return self


class TaskSpec(BaseModel):
    task_id: str = Field(min_length=1)
    goal_ref: str = Field(min_length=1)
    task_type: TaskType
    objective: str = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_outputs: list[str] = Field(default_factory=list)
    capability: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    max_retries: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_dependencies(self) -> TaskSpec:
        if self.task_id in self.dependencies:
            raise ValueError("task cannot depend on itself")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("task dependencies must be unique")
        return self


class Observation(BaseModel):
    observation_id: str = Field(min_length=1)
    task_ref: str = Field(min_length=1)
    observation_type: str = Field(min_length=1)
    content: Any = None
    summary: str | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    created_at: datetime


class TaskError(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class RetryMetadata(BaseModel):
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    next_retry_at: datetime | None = None

    @model_validator(mode="after")
    def validate_attempts(self) -> RetryMetadata:
        if self.attempt > self.max_attempts:
            raise ValueError("retry attempt cannot exceed max_attempts")
        return self


class ResumeMetadata(BaseModel):
    resumable: bool = True
    checkpoint_ref: str | None = None
    resumed_from: str | None = None
    definition_version: str | None = None


class TaskState(BaseModel):
    goal_ref: str = Field(min_length=1)
    task_spec: TaskSpec
    status: TaskStatus = TaskStatus.PENDING
    dependencies: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    observations: list[Observation] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    errors: list[TaskError] = Field(default_factory=list)
    budget_usage: BudgetUsage = Field(default_factory=BudgetUsage)
    permission_context: PermissionContext
    memory_refs: list[str] = Field(default_factory=list)
    trace_refs: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    retry: RetryMetadata = Field(default_factory=RetryMetadata)
    resume: ResumeMetadata = Field(default_factory=ResumeMetadata)

    @model_validator(mode="after")
    def validate_references_and_timestamps(self) -> TaskState:
        if self.goal_ref != self.task_spec.goal_ref:
            raise ValueError("task state and task spec must reference the same goal")
        if not self.dependencies:
            self.dependencies = list(self.task_spec.dependencies)
        if not self.inputs:
            self.inputs = dict(self.task_spec.inputs)
        if self.started_at and self.completed_at and self.started_at > self.completed_at:
            raise ValueError("task cannot complete before it starts")
        terminal = {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BUDGET_LIMITED,
            TaskStatus.INSUFFICIENT_EVIDENCE,
        }
        if self.status in terminal and self.completed_at is None:
            raise ValueError("terminal task state requires completed_at")
        return self


class AgentTraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    event_type: AgentTraceEventType
    goal_ref: str = Field(min_length=1)
    task_ref: str | None = None
    sequence: int = Field(ge=0)
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    correlation_id: str | None = None
    causation_id: str | None = None


class MemoryRecord(BaseModel):
    memory_id: str = Field(min_length=1)
    memory_type: MemoryType
    content: dict[str, Any]
    source_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    status: MemoryStatus = MemoryStatus.CANDIDATE
    confirmed_by: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_confirmation_and_time(self) -> MemoryRecord:
        if self.status == MemoryStatus.CONFIRMED and not self.confirmed_by:
            raise ValueError("confirmed memory requires confirmed_by")
        if self.updated_at < self.created_at:
            raise ValueError("memory cannot be updated before it is created")
        return self


class SkillSpec(BaseModel):
    skill_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    trigger: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    workflow_template: list[dict[str, Any]] = Field(default_factory=list)
    tool_requirements: list[str] = Field(default_factory=list)
    examples: list[dict[str, Any]] = Field(default_factory=list)
    evaluation_score: float | None = Field(default=None, ge=0.0, le=1.0)
    version: str = Field(min_length=1)
    status: SkillStatus = SkillStatus.DRAFT
    governance: dict[str, Any] = Field(default_factory=dict)
