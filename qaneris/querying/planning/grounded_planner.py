"""Deterministic query planning: ``GroundedQueryContext`` -> ``GroundedQueryPlan``.

Planning happens after grounding, inside the "physical certainty" region: the meaning of the question
is already fixed. The planner therefore re-reads nothing — no enterprise database, no published
graph, no ``CompanyDataProfile``, no retrieval, no model — and it can only name assets the context
already allowlisted. It never repairs, never guesses a join and never emits native query text.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

from qaneris.common.errors import QueryPlanningError
from qaneris.contracts.query import (
    AggregateFunction,
    GroundedFieldRef,
    GroundedQueryContext,
    GroundedQueryPlan,
    PlanAggregate,
    PlanFilter,
    PlanJoin,
    PlanSort,
    PlanSortTarget,
    QueryContextBinding,
    QueryResultType,
    SortDirection,
    aggregate_function_for,
)
from qaneris.contracts.semantic import BusinessObjective, RankingDirection, SemanticAssetType

#: Phase 1 rows default. ``ranking.limit`` overrides it when the intent layer already fixed a limit.
_DEFAULT_LIMIT = 200

_SORT_DIRECTION = {
    RankingDirection.TOP: SortDirection.DESCENDING,
    RankingDirection.BOTTOM: SortDirection.ASCENDING,
}


class GroundedQueryPlanner:
    """Rule-based planner over the grounded context.

    Aggregates: a bound metric is summed by default, because a governed metric is a measure; a
    structured derivation operation overrides it (``count`` / ``average`` / ``minimum`` /
    ``maximum``), and a query without any metric counts rows. Ranking direction, ranking limit,
    filters, time range and join come from the context verbatim: nothing is re-read from the
    question text.
    """

    def plan(self, context: GroundedQueryContext) -> GroundedQueryPlan:
        datasource_id = self._datasource(context)
        relationships = self._relationships(context, datasource_id)
        data_object_ids = _object_ids(context, relationships)
        self._require_allowlisted_objects(context, data_object_ids)

        aggregates, aggregate_fields = _aggregates(context)
        group_by = _group_by(context)
        sorts, limit = _ordering(context, aggregates, aggregate_fields, group_by)
        selected_fields = list(group_by)
        # A lookup of document fields or Redis adapter virtual fields is a projection.
        # The relational fallback of COUNT for metric-free questions would otherwise
        # make Mongo find and Redis exact-key reads unreachable from BusinessQuery.
        if _projection_lookup(context, data_object_ids) and not context.metrics and group_by:
            aggregates = []
            group_by = []
            if context.data_objects[data_object_ids[0]].name.startswith("redis:"):
                sorts = []
        filters = [
            PlanFilter(field=item.field, operator=item.operator, value=item.value)
            for item in context.filters
        ]

        body: dict[str, object] = {
            "workspace_id": context.workspace_id,
            "datasource_id": datasource_id,
            "data_object_ids": data_object_ids,
            "data_objects": dict(context.data_objects),
            "scan_version": context.scan_version,
            "aggregates": aggregates,
            "selected_fields": selected_fields,
            "filters": filters,
            "group_by": group_by,
            "sorts": sorts,
            "limit": limit,
            "joins": relationships,
            "time_range": context.time_range,
            "time_field": context.time_field,
            "expected_result_type": (
                QueryResultType.SCALAR if aggregates and not group_by else QueryResultType.TABULAR
            ),
            "rationale": _rationale(context, aggregates, group_by, relationships),
        }
        return GroundedQueryPlan.model_validate(
            {**body, "plan_id": _plan_identity(body)}
        )


    @staticmethod
    def _datasource(context: GroundedQueryContext) -> str:
        datasource_ids = context.allowed_datasource_ids
        if len(datasource_ids) != 1:
            raise QueryPlanningError(
                f"查询上下文必须只允许一个数据源，当前为 {datasource_ids}"
            )
        return datasource_ids[0]

    def _relationships(
        self, context: GroundedQueryContext, datasource_id: str
    ) -> list[PlanJoin]:
        """Joins come from confirmed relationships only, and only when two objects are involved."""
        object_ids = _object_ids(context, [])
        if len(object_ids) <= 1:
            return []
        if not context.relationships:
            raise QueryPlanningError(
                "绑定涉及多个数据对象，但没有已确认的企业数据图关系，禁止推测连接"
            )
        if len(object_ids) > 2:
            raise QueryPlanningError(
                f"绑定涉及 {len(object_ids)} 个数据对象，第一阶段只支持一个已确认的直接连接"
            )
        expected = set(object_ids)
        matching = [
            item
            for item in context.relationships
            if {item.from_data_object_id, item.to_data_object_id} == expected
            and item.datasource_id == datasource_id
        ]
        if len(matching) != 1:
            raise QueryPlanningError(
                "第一阶段只接受一个已确认关系用于直接连接，当前匹配到 "
                f"{len(matching)} 个"
            )
        relationship = matching[0]
        return [
            PlanJoin(
                datasource_id=relationship.datasource_id,
                relationship_id=relationship.relationship_id,
                relationship_type=relationship.relationship_type,
                from_data_object_id=relationship.from_data_object_id,
                from_field_path=relationship.from_field_path,
                to_data_object_id=relationship.to_data_object_id,
                to_field_path=relationship.to_field_path,
            )
        ]

    @staticmethod
    def _require_allowlisted_objects(
        context: GroundedQueryContext, data_object_ids: list[str]
    ) -> None:
        unbound = sorted(set(data_object_ids) - set(context.allowed_data_object_ids))
        if unbound:
            raise QueryPlanningError(f"计划引用了允许列表之外的数据对象：{unbound}")


def _projection_lookup(context: GroundedQueryContext, object_ids: list[str]) -> bool:
    if (context.objective is not BusinessObjective.LOOKUP or context.ranking is not None
            or len(object_ids) != 1):
        return False
    locator = context.data_objects.get(object_ids[0])
    if locator is None or locator.name is None:
        return False
    return locator.object_kind == "collection" or (
        locator.name.startswith("redis:")
        and locator.object_kind in {"string", "hash", "list", "set", "zset"}
    )


def _object_ids(context: GroundedQueryContext, joins: list[PlanJoin]) -> list[str]:
    ids = {binding.data_object_id for binding in context.bindings if binding.data_object_id}
    for join in joins:
        ids.add(join.from_data_object_id)
        ids.add(join.to_data_object_id)
    return sorted(ids)


def _aggregates(
    context: GroundedQueryContext,
) -> tuple[list[PlanAggregate], dict[tuple[str, str, str], str]]:
    """One aggregate per metric, plus aliases keyed by physical field identity for sorting.

    The aggregate function is governed: the planner picks it from a structured
    :class:`DerivationSpec` if one exists, otherwise from the metric's
    ``default_aggregation``. A metric without either rule is refused (a "non-additive" metric such
    as a ratio has no business auto-summing). The QueryContextBuilder already rejects such
    metrics; this is the second line of defence.
    """
    derived = {
        derivation.metric: aggregate_function_for(derivation.operation)
        for derivation in context.derivations
        if derivation.metric
    }
    metrics = _metric_bindings(context)
    if not metrics:
        return [PlanAggregate(function=AggregateFunction.COUNT, alias="count")], {}

    aggregates: list[PlanAggregate] = []
    aliases: dict[tuple[str, str, str], str] = {}
    used: set[str] = set()
    for binding in metrics:
        reference = binding.field_ref()
        if reference is None:
            raise QueryPlanningError(
                f"指标“{binding.business_term}”没有物理字段绑定，不能规划"
            )
        overridden = derived.get(binding.business_term)
        if overridden is not None:
            function = overridden
        elif binding.default_aggregation is not None:
            function = binding.default_aggregation
        else:
            raise QueryPlanningError(
                f"指标“{binding.business_term}”缺少受治理聚合语义，"
                f"也没有结构化衍生计算覆盖，第一阶段不得猜 {AggregateFunction.SUM.value}"
            )
        identity = reference.identity()
        if identity in aliases:
            continue
        alias = _alias(function, reference.field_path, used)
        aliases[identity] = alias
        aggregates.append(PlanAggregate(function=function, field=reference, alias=alias))
    return aggregates, aliases


def _metric_bindings(context: GroundedQueryContext) -> list[QueryContextBinding]:
    """Bound metrics in a deterministic order: requested metrics first, then the rest."""
    ordered = list(context.metrics)
    known = {_binding_key(binding) for binding in ordered}
    extras = sorted(
        (
            binding
            for binding in context.bindings
            if binding.asset_type is SemanticAssetType.METRIC
            and _binding_key(binding) not in known
        ),
        key=_binding_key,
    )
    return [*ordered, *extras]


def _group_by(context: GroundedQueryContext) -> list[GroundedFieldRef]:
    references: list[GroundedFieldRef] = []
    seen: set[tuple[str, str, str]] = set()
    for binding in context.dimensions:
        reference = binding.field_ref()
        if reference is None:
            raise QueryPlanningError(
                f"维度“{binding.business_term}”没有物理字段绑定，不能规划"
            )
        if reference.identity() in seen:
            continue
        seen.add(reference.identity())
        references.append(reference)
    return references


def _ordering(
    context: GroundedQueryContext,
    aggregates: list[PlanAggregate],
    aliases: dict[tuple[str, str, str], str],
    group_by: list[GroundedFieldRef],
) -> tuple[list[PlanSort], int]:
    """Ranking is taken verbatim from the intent layer; grouping gets a stable deterministic order."""
    ranking = context.ranking
    if ranking is None:
        return (
            [
                PlanSort(
                    target=PlanSortTarget.FIELD,
                    field=reference,
                    direction=SortDirection.ASCENDING,
                )
                for reference in group_by
            ],
            _DEFAULT_LIMIT,
        )
    target = next(
        (
            binding
            for binding in _metric_bindings(context)
            if binding.business_term == ranking.metric
        ),
        None,
    )
    reference = target.field_ref() if target is not None else None
    alias = aliases.get(reference.identity()) if reference is not None else None
    if alias is None:
        raise QueryPlanningError(
            f"排名指标“{ranking.metric}”没有已落地的聚合，不能规划"
        )
    if not any(item.alias == alias for item in aggregates):
        raise QueryPlanningError(f"排名指标“{ranking.metric}”的聚合不在计划中，不能规划")
    return (
        [
            PlanSort(
                target=PlanSortTarget.AGGREGATE,
                aggregate_alias=alias,
                direction=_SORT_DIRECTION[ranking.direction],
            )
        ],
        ranking.limit,
    )


def _alias(function: AggregateFunction, field_path: str, used: set[str]) -> str:
    base = f"{function.value}_{field_path}" if field_path else function.value
    alias = base
    suffix = 2
    while alias in used:
        alias = f"{base}_{suffix}"
        suffix += 1
    used.add(alias)
    return alias


def _binding_key(binding: QueryContextBinding) -> tuple[str, str, str]:
    return (
        binding.business_term,
        binding.asset_id,
        binding.field_path or binding.relationship_id or "",
    )


def _rationale(
    context: GroundedQueryContext,
    aggregates: list[PlanAggregate],
    group_by: list[GroundedFieldRef],
    joins: list[PlanJoin],
) -> list[str]:
    lines = [
        "计划只引用语义落地允许列表中的物理资产",
        f"数据源 {context.allowed_datasource_ids[0]} 由语义落地唯一确认",
    ]
    for aggregate in aggregates:
        if aggregate.field is None:
            lines.append("没有指标绑定，聚合为 count")
            continue
        lines.append(
            f"指标“{aggregate.field.business_term}”→ {aggregate.field.field_path} "
            f"使用 {aggregate.function.value} 聚合"
        )
    for reference in group_by:
        lines.append(f"按维度“{reference.business_term}”→ {reference.field_path} 分组")
    for item in context.filters:
        lines.append(
            f"过滤“{item.subject}”→ {item.field.field_path}（业务值不参与物理身份）"
        )
    if context.ranking is not None:
        lines.append("排名方向与数量来自意图层显式字段，未重新解析问题文本")
    for join in joins:
        lines.append(
            f"按已确认关系 {join.relationship_id} 直接连接 "
            f"{join.from_field_path} 与 {join.to_field_path}"
        )
    if context.time_range is not None:
        lines.append("时间范围来自规范化事实，未重新解析问题文本")
    return lines


def _plan_identity(body: dict[str, object]) -> str:
    """Deterministic plan id: the same context always yields the same plan, id included."""
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, default=_jsonable)
    return "plan_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"cannot serialize plan field: {type(value).__name__}")
