"""Independent validation of every model-authored federated draft."""

import re
from datetime import UTC, datetime
from uuid import uuid4

from smartdata.federation.governance import FederationFailure, JoinMappingGovernance
from smartdata.federation.models import (
    ExecutionScope,
    FederatedPlan,
    FederatedPlanDraft,
    SourceTask,
)

_NATIVE = re.compile(
    r"(?i)\b(select|insert|update|delete|drop|alter|create|from|where|join|"
    r"hgetall|smembers|lrange|mget|redis_command|pipeline|script|eval|exec)\b"
    r"|\$match|\$group|\bdb\.\w+"
)


class FederatedPlanValidator:
    def __init__(self, governance: JoinMappingGovernance):
        self.governance = governance

    def validate(
        self, draft: FederatedPlanDraft, scope: ExecutionScope, run_id: str,
        question: str, business_query: dict,
    ) -> FederatedPlan:
        if datetime.now(UTC) >= scope.deadline:
            raise FederationFailure("federation_deadline_exceeded")
        if not 2 <= len(draft.source_tasks) <= scope.max_sources:
            raise FederationFailure("federation_source_limit", blocked=True)
        if len(draft.source_tasks) * scope.max_rows_per_source > scope.max_total_rows:
            raise FederationFailure("federation_budget_exceeded", blocked=True)
        allowed = set(scope.allowed_datasource_ids)
        sources = [item.datasource_id for item in draft.source_tasks]
        if any(source not in allowed for source in sources):
            raise FederationFailure("forbidden_datasource", blocked=True)
        if len(set(sources)) != len(sources):
            raise FederationFailure("duplicate_source_task", blocked=True)
        if any(_NATIVE.search(item.question) or _NATIVE.search(item.purpose)
               for item in draft.source_tasks):
            raise FederationFailure("native_query_forbidden", blocked=True)
        if draft.merge.options:
            raise FederationFailure("unsupported_merge_options", blocked=True)
        ids = [f"source_{index + 1}" for index in range(len(sources))]
        if draft.merge.input_task_ids and draft.merge.input_task_ids != ids:
            raise FederationFailure("unknown_source_task_reference", blocked=True)
        if draft.merge.operation == "keyed_join":
            if not scope.allow_cross_source_join or not draft.merge.mapping_id:
                raise FederationFailure("unconfirmed_mapping", blocked=True)
            if len(sources) != 2 or not draft.merge.join_mode:
                raise FederationFailure("invalid_join_plan", blocked=True)
            mapping = self.governance.executable(
                draft.merge.mapping_id, scope.workspace_id, sources
            )
            if sources != [mapping.left_datasource_id, mapping.right_datasource_id]:
                raise FederationFailure("join_mapping_source_mismatch", blocked=True)
        elif draft.merge.mapping_id or draft.merge.join_mode:
            raise FederationFailure("unexpected_join_mapping", blocked=True)
        expected_shape = {
            "compare_scalars": "scalar", "combine_scalars": "scalar",
            "union_rows": "rows", "align_time_series": "time_series", "keyed_join": "rows",
        }[draft.merge.operation]
        if any(task.expected_shape != expected_shape for task in draft.source_tasks):
            raise FederationFailure("merge_shape_mismatch", blocked=True)
        if draft.merge.operation == "combine_scalars" and not draft.merge.scalar_operation:
            raise FederationFailure("missing_scalar_operation", blocked=True)
        if draft.merge.operation == "combine_scalars" and \
                draft.merge.scalar_operation == "percentage_difference":
            raise FederationFailure("unsupported_scalar_operation", blocked=True)
        if draft.merge.operation not in {"compare_scalars", "combine_scalars"} \
                and draft.merge.scalar_operation:
            raise FederationFailure("unexpected_scalar_operation", blocked=True)
        plan_id = str(uuid4())
        tasks = [SourceTask(
            **item.model_dump(), task_id=ids[index], run_id=run_id,
            plan_id=plan_id, ordinal=index,
        ) for index, item in enumerate(draft.source_tasks)]
        return FederatedPlan(
            plan_id=plan_id, run_id=run_id, workspace_id=scope.workspace_id,
            question=question, source_tasks=tasks,
            merge_plan=draft.merge.model_copy(update={"input_task_ids": ids}),
            execution_scope=scope, business_query=business_query,
        )
