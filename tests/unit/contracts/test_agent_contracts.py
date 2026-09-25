from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from smartdata.contracts import (
    AgentTraceEvent,
    ArtifactRef,
    BudgetSpec,
    CompletionCriterion,
    EvidenceRef,
    GoalSpec,
    MemoryRecord,
    Observation,
    PermissionContext,
    SkillSpec,
    TaskError,
    TaskSpec,
    TaskState,
    TaskStatus,
    TaskType,
)


def permission(workspace_id: str = "default") -> PermissionContext:
    return PermissionContext(
        principal_id="user_1",
        workspace_id=workspace_id,
        roles=["analyst"],
        allowed_datasource_ids=["ds_sales"],
    )


def task_spec() -> TaskSpec:
    return TaskSpec(
        task_id="task_1",
        goal_ref="goal_1",
        task_type=TaskType.QUERY,
        objective="查询销售额",
        expected_outputs=["typed_query_result"],
        max_retries=2,
    )


def test_goal_spec_round_trips_with_budget_and_permission_context() -> None:
    goal = GoalSpec(
        id="goal_1",
        objective="分析销售额变化",
        scope=["销售"],
        workspace="default",
        requested_output=["table", "explanation"],
        constraints=["read_only"],
        budget=BudgetSpec(max_tasks=8, max_tool_calls=12),
        permission_context=permission(),
    )

    restored = GoalSpec.model_validate_json(goal.model_dump_json())

    assert restored == goal
    assert restored.budget.max_tasks == 8


def test_goal_spec_rejects_permission_context_from_another_workspace() -> None:
    with pytest.raises(ValidationError, match="same workspace"):
        GoalSpec(
            id="goal_1",
            objective="分析销售额",
            workspace="workspace_a",
            permission_context=permission("workspace_b"),
        )


def test_completion_criterion_and_evidence_are_structured() -> None:
    criterion = CompletionCriterion(
        criterion_id="criterion_1",
        type="evidence",
        description="关键数字具有查询证据",
        required_evidence=["query_trace"],
    )
    artifact = ArtifactRef(
        artifact_id="artifact_1",
        artifact_type="table",
        uri="artifact://goal_1/table_1",
    )
    evidence = EvidenceRef(
        evidence_id="evidence_1",
        source="query",
        claim="销售额为 400.5",
        artifact_ref=artifact,
        query_trace_ref="query_trace_1",
    )

    assert criterion.status == "pending"
    assert evidence.artifact_ref == artifact

    with pytest.raises(ValidationError, match="requires an artifact"):
        EvidenceRef(evidence_id="bad", source="query", claim="unsupported")


def test_task_spec_rejects_self_and_duplicate_dependencies() -> None:
    with pytest.raises(ValidationError, match="depend on itself"):
        TaskSpec(
            task_id="task_1",
            goal_ref="goal_1",
            task_type="query",
            objective="query",
            dependencies=["task_1"],
        )

    with pytest.raises(ValidationError, match="must be unique"):
        TaskSpec(
            task_id="task_1",
            goal_ref="goal_1",
            task_type="query",
            objective="query",
            dependencies=["task_0", "task_0"],
        )


def test_task_contract_freezes_supported_types_and_states() -> None:
    assert {item.value for item in TaskType} == {
        "query",
        "analysis",
        "retrieval",
        "visualization",
        "report",
        "validate",
        "clarify",
    }
    assert {item.value for item in TaskStatus} == {
        "pending",
        "ready",
        "running",
        "completed",
        "failed",
        "blocked",
        "waiting_user",
        "paused",
        "cancelled",
        "budget_limited",
        "insufficient_evidence",
    }


def test_task_state_validates_goal_and_terminal_timestamp() -> None:
    now = datetime.now(UTC)
    observation = Observation(
        observation_id="observation_1",
        task_ref="task_1",
        observation_type="query_result",
        content={"value": 400.5},
        created_at=now,
    )
    error = TaskError(code="timeout", message="timed out", retryable=True, occurred_at=now)
    state = TaskState(
        goal_ref="goal_1",
        task_spec=task_spec(),
        status="completed",
        observations=[observation],
        errors=[error],
        permission_context=permission(),
        started_at=now,
        completed_at=now + timedelta(seconds=1),
    )

    assert state.status == TaskStatus.COMPLETED

    with pytest.raises(ValidationError, match="requires completed_at"):
        TaskState(
            goal_ref="goal_1",
            task_spec=task_spec(),
            status="failed",
            permission_context=permission(),
        )


def test_agent_trace_event_is_immutable_and_json_serializable() -> None:
    event = AgentTraceEvent(
        event_id="event_1",
        event_type="task_started",
        goal_ref="goal_1",
        task_ref="task_1",
        sequence=3,
        occurred_at=datetime.now(UTC),
        payload={"attempt": 1},
    )
    restored = AgentTraceEvent.model_validate_json(event.model_dump_json())

    assert restored == event
    with pytest.raises(ValidationError):
        event.sequence = 4


def test_confirmed_memory_requires_human_confirmation() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="requires confirmed_by"):
        MemoryRecord(
            memory_id="memory_1",
            memory_type="semantic",
            content={"term": "GMV"},
            status="confirmed",
            created_at=now,
            updated_at=now,
        )


def test_skill_spec_exposes_phase_one_registry_shape() -> None:
    skill = SkillSpec(
        skill_id="sales_diagnosis",
        name="销售下降诊断",
        description="定位销售额下降原因",
        trigger={"intent": "diagnosis"},
        inputs={"metric": {"type": "string"}},
        workflow_template=[{"task_type": "query"}, {"task_type": "analysis"}],
        tool_requirements=["semantic_query"],
        examples=[{"input": "为什么销售额下降"}],
        version="1.0.0",
        governance={"owner": "data_team"},
    )

    restored = SkillSpec.model_validate_json(skill.model_dump_json())

    assert restored == skill
    assert "workflow_template" in SkillSpec.model_json_schema()["properties"]
