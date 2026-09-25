"""Deterministic planning and plan validation over a grounded query context.

Every scenario here starts from a grounding (or a context built from one) and asserts that planning
adds no meaning: the plan only names allowlisted graph identities, joins only confirmed
relationships, and anything unresolved stops before planning.
"""

from __future__ import annotations

import json

import pytest
from _grounded_fixtures import (
    CUSTOMERS,
    DS,
    EDGE,
    ORDERS,
    ORDERS_PAY,
    ORDERS_REGION,
    OTHER_DS,
    _result,
    dimension,
    grounded_query,
    metric,
    region_query,
    relationship,
    sales_context,
)

from smartdata.common.errors import QueryContextBuildError, QueryPlanningError
from smartdata.contracts.query import (
    AggregateFunction,
    FilterOperator,
    GroundedDataObjectRef,
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
    TimeRange,
    TimeSpec,
)
from smartdata.contracts.semantic import (
    BusinessFilter,
    BusinessQuery,
    DerivationSpec,
    RankingSpec,
    SemanticAssetType,
)
from smartdata.querying import GroundedPlanValidator, GroundedQueryPlanner, QueryContextBuilder

REGION = GroundedFieldRef(
    datasource_id=DS, data_object_id=ORDERS, field_path="region", field_id=ORDERS_REGION
)
PAY = GroundedFieldRef(
    datasource_id=DS, data_object_id=ORDERS, field_path="pay_amount", field_id=ORDERS_PAY
)
CUSTOMER_ID = GroundedFieldRef(
    datasource_id=DS, data_object_id=CUSTOMERS, field_path="id", field_id="field_customer_pk"
)


def plan_for(context: GroundedQueryContext) -> GroundedQueryPlan:
    planner = GroundedQueryPlanner()
    plan = planner.plan(context)
    GroundedPlanValidator().validate(plan, context)
    return plan


def plan_for_query(query: BusinessQuery, pairs) -> GroundedQueryPlan:
    grounded = grounded_query(query, pairs)
    return plan_for(QueryContextBuilder().build(_result(grounded)))


def test_scenario_1_single_table_metric() -> None:
    query = region_query(dimensions=[])
    context = QueryContextBuilder().build(
        _result(grounded_query(query, [("销售额", metric())]))
    )
    plan = plan_for(context)

    assert plan.datasource_id == DS
    assert plan.data_object_ids == ["obj_orders"]
    assert [item.field.field_path for item in plan.aggregates if item.field] == [
        "pay_amount"
    ]
    assert plan.aggregates[0].function is AggregateFunction.SUM
    assert plan.selected_fields == []
    assert plan.group_by == []
    assert plan.expected_result_type is QueryResultType.SCALAR
    # Nothing else may enter the plan: the region candidate was never bound.
    assert "region" not in plan.model_dump_json()
    assert "gross_amount" not in plan.model_dump_json()


def test_scenario_2_metric_and_dimension_on_one_object() -> None:
    plan = plan_for(sales_context())

    assert plan.aggregates[0].field is not None
    assert plan.aggregates[0].field.identity() == PAY.identity()
    assert plan.aggregates[0].field.field_id == ORDERS_PAY
    assert [item.identity() for item in plan.group_by] == [REGION.identity()]
    assert plan.group_by[0].field_id == ORDERS_REGION
    assert plan.selected_fields == plan.group_by
    assert plan.expected_result_type is QueryResultType.TABULAR
    assert plan.joins == []


def test_scenario_2_unbound_candidates_never_reach_the_plan() -> None:
    """The retrieval recalled gross_amount, but grounding did not bind it, so planning must not."""
    query = region_query(dimensions=[])
    unbound = metric(asset_id="metric_gross_sales", name="成交总额", field_path="gross_amount")
    grounded = grounded_query(
        query, [("销售额", metric())], candidates=[metric(), unbound, dimension()]
    )
    plan = plan_for(QueryContextBuilder().build(_result(grounded)))

    assert "gross_amount" not in plan.model_dump_json()
    assert "成交总额" not in plan.model_dump_json()
    assert "region" not in plan.model_dump_json()


def test_scenario_3_filter_uses_the_grounded_field_and_keeps_the_value_a_value() -> None:
    query = region_query(
        dimensions=[],
        filters=[BusinessFilter(subject="地区", operator="equals", value="东京")],
    )
    plan = plan_for_query(query, [("销售额", metric()), ("地区", dimension())])

    assert len(plan.filters) == 1
    item = plan.filters[0]
    assert item.operator is FilterOperator.EQUALS
    assert item.field.identity() == REGION.identity()
    assert item.value == "东京"
    # The value is business data, never a physical asset: it appears only as a filter value.
    payload = plan.model_dump(mode="json")
    assert payload["filters"][0]["value"] == "东京"
    physical = json.dumps(
        {key: value for key, value in payload.items() if key != "filters"}, ensure_ascii=False
    )
    assert "东京" not in physical


def test_scenario_4_ranking_comes_from_the_intent_layer_verbatim() -> None:
    query = region_query(
        ranking=RankingSpec(direction="top", limit=10, metric="销售额"),
        objective="ranking",
    )
    plan = plan_for_query(query, [("销售额", metric()), ("地区", dimension())])

    assert [item.identity() for item in plan.group_by] == [REGION.identity()]
    assert len(plan.sorts) == 1
    sort = plan.sorts[0]
    assert sort.target is PlanSortTarget.AGGREGATE
    assert sort.aggregate_alias == plan.aggregates[0].alias
    assert sort.direction is SortDirection.DESCENDING
    assert plan.limit == 10

    bottom = region_query(
        ranking=RankingSpec(direction="bottom", limit=3, metric="销售额"),
        objective="ranking",
    )
    bottom_plan = plan_for_query(bottom, [("销售额", metric()), ("地区", dimension())])
    assert bottom_plan.sorts[0].direction is SortDirection.ASCENDING
    assert bottom_plan.limit == 3


def test_scenario_4_the_question_text_is_not_reparsed() -> None:
    """"最高前10" lives in the intent layer: changing the wording must not change the plan."""
    structured = RankingSpec(direction="top", limit=10, metric="销售额")
    first = region_query(ranking=structured, objective="ranking")
    other = region_query(
        question="把金额从高到低排列，取前 10 条记录", ranking=structured, objective="ranking"
    )
    pairs = [("销售额", metric()), ("地区", dimension())]

    assert (
        plan_for_query(first, pairs).model_dump_json(exclude={"plan_id"})
        == plan_for_query(other, pairs).model_dump_json(exclude={"plan_id"})
    )


def test_scenario_5_time_range_is_carried_not_parsed() -> None:
    query = region_query(time_expression="2026年8月")
    range_ = TimeRange(start="2026-08-01", end="2026-08-31")
    grounded = grounded_query(
        query, [("销售额", metric()), ("地区", dimension(data_type="date"))]
    )
    context = QueryContextBuilder().build(
        _result(grounded),
        time_range=range_,
        time_spec=TimeSpec(dimension="地区"),
    )
    plan = plan_for(context)

    assert context.time_expression == "2026年8月"
    assert plan.time_range == range_
    assert plan.time_field is not None and plan.time_field.identity() == REGION.identity()
    # The plan carries the data object locator the generator will compile against, and matches the
    # context so the planner never went back to the graph.
    assert set(plan.data_objects) == set(context.data_objects)
    assert plan.data_objects[ORDERS].data_object_id == ORDERS


def test_scenario_6_confirmed_relationship_becomes_a_direct_join() -> None:
    query = BusinessQuery(
        question="按客户地区看销售额",
        objective="lookup",
        metrics=["销售额"],
        dimensions=["客户地区"],
    )
    customer_region = dimension(
        asset_id="dimension_customer_region",
        name="客户地区",
        data_object_id=CUSTOMERS,
        field_id="field_customer_region",
        field_path="region",
    )
    grounded = grounded_query(
        query,
        [
            ("销售额", metric()),
            ("客户地区", customer_region),
            ("foreign_key", relationship()),
        ],
    )
    context = QueryContextBuilder().build(_result(grounded))
    plan = plan_for(context)

    assert len(plan.joins) == 1
    join = plan.joins[0]
    assert join.relationship_id == EDGE
    assert {join.from_data_object_id, join.to_data_object_id} == {ORDERS, CUSTOMERS}
    assert (join.from_field_path, join.to_field_path) == ("customer_id", "id")
    assert sorted(plan.data_object_ids) == sorted([ORDERS, CUSTOMERS])
    assert [item.relationship_id for item in context.relationships] == [EDGE]


def test_scenario_7_two_objects_without_a_relationship_are_refused() -> None:
    query = BusinessQuery(
        question="按客户地区看销售额",
        objective="lookup",
        metrics=["销售额"],
        dimensions=["客户地区"],
    )
    customer_region = dimension(
        asset_id="dimension_customer_region",
        name="客户地区",
        data_object_id=CUSTOMERS,
        field_id="field_customer_region",
        field_path="region",
    )
    context = QueryContextBuilder().build(
        _result(grounded_query(query, [("销售额", metric()), ("客户地区", customer_region)]))
    )

    with pytest.raises(QueryPlanningError, match="禁止推测连接"):
        GroundedQueryPlanner().plan(context)


def test_scenario_8_allowlist_escape_is_rejected() -> None:
    context = sales_context()
    escape = GroundedFieldRef(
        datasource_id=DS, data_object_id=ORDERS, field_path="gross_amount"
    )
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[ORDERS],
        data_objects=dict(context.data_objects),
        aggregates=[PlanAggregate(function=AggregateFunction.SUM, field=escape, alias="sum_gross")],
        selected_fields=[escape],
        expected_result_type=QueryResultType.TABULAR,
    )

    with pytest.raises(QueryPlanningError, match="不在允许列表中"):
        GroundedPlanValidator().validate(plan, context)


def test_scenario_8_a_tampered_field_identity_is_rejected() -> None:
    context = sales_context()
    tampered = REGION.model_copy(update={"field_id": "field_somewhere_else"})
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[ORDERS],
        data_objects=dict(context.data_objects),
        aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count")],
        selected_fields=[tampered],
        group_by=[tampered],
        expected_result_type=QueryResultType.TABULAR,
    )

    with pytest.raises(QueryPlanningError, match="图字段标识与允许列表不一致"):
        GroundedPlanValidator().validate(plan, context)


def test_scenario_8_an_object_outside_the_allowlist_is_rejected() -> None:
    context = sales_context()
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[CUSTOMERS],
        aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count")],
        expected_result_type=QueryResultType.SCALAR,
    )

    with pytest.raises(QueryPlanningError, match="允许列表之外的数据对象"):
        GroundedPlanValidator().validate(plan, context)


def test_scenario_9_ambiguity_stops_before_planning() -> None:
    grounded = grounded_query(
        region_query(),
        [("销售额", metric()), ("地区", dimension())],
        unresolved=["指标“销售额”对应 2 个不同的物理绑定，未解决歧义"],
    )

    with pytest.raises(QueryContextBuildError, match="未解决歧义"):
        QueryContextBuilder().build(_result(grounded))

    context = sales_context()
    scoped = context.model_copy(update={"unresolved_ambiguities": ["未解决歧义"]})
    with pytest.raises(ValueError, match="unresolved ambiguities"):
        GroundedQueryContext(**scoped.model_dump())


def test_scenario_10_cross_source_planning_is_refused() -> None:
    other = metric(
        asset_id="metric_other",
        name="外部销售额",
        datasource_id=OTHER_DS,
        data_object_id="obj_other",
    )
    query = region_query(metrics=["销售额", "外部销售额"], dimensions=[])
    grounded = grounded_query(query, [("销售额", metric()), ("外部销售额", other)])

    with pytest.raises(QueryContextBuildError, match="跨数据源"):
        QueryContextBuilder().build(_result(grounded))

    # A hand-built multi-source context cannot be planned either.
    sales = QueryContextBinding(
        business_term="销售额",
        asset_type=SemanticAssetType.METRIC,
        asset_id="metric_paid_sales",
        datasource_id=DS,
        data_object_id=ORDERS,
        field_id=ORDERS_PAY,
        field_path="pay_amount",
        score=0.9,
    )
    foreign = sales.model_copy(update={"datasource_id": OTHER_DS, "data_object_id": "obj_other"})
    multi = GroundedQueryContext(
        workspace_id="default",
        objective="lookup",
        business_query=query,
        bindings=[sales, foreign],
        metrics=[sales, foreign],
        allowed_datasource_ids=[DS, OTHER_DS],
        allowed_data_object_ids=[ORDERS, "obj_other"],
        allowed_field_paths=["pay_amount"],
        allowed_field_ids=[ORDERS_PAY],
    )
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id="default",
        datasource_id=DS,
        data_object_ids=[ORDERS],
        aggregates=[PlanAggregate(function=AggregateFunction.SUM, field=PAY, alias="sum_pay_amount")],
        expected_result_type=QueryResultType.SCALAR,
    )
    with pytest.raises(QueryPlanningError, match="跨数据源"):
        GroundedPlanValidator().validate(plan, multi)


def test_scenario_11_planning_is_deterministic() -> None:
    query = region_query(
        ranking=RankingSpec(direction="top", limit=10, metric="销售额"),
        objective="ranking",
        filters=[BusinessFilter(subject="地区", operator="equals", value="东京")],
    )
    grounded = grounded_query(query, [("销售额", metric()), ("地区", dimension())])
    context = QueryContextBuilder().build(_result(grounded))

    first = plan_for(context)
    second = plan_for(context)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.plan_id == second.plan_id
    assert first.plan_id.startswith("plan_")


def test_scenario_12_planning_never_reads_the_database_graph_or_model() -> None:
    """A context built from a hand-written grounding plans without any reader, adapter or model."""
    query = region_query(dimensions=[])
    grounded = grounded_query(query, [("销售额", metric())])
    context = QueryContextBuilder().build(_result(grounded))

    plan = GroundedQueryPlanner().plan(context)

    assert plan.aggregates[0].field is not None
    assert plan.aggregates[0].field.identity() == PAY.identity()


def test_count_is_used_when_no_metric_is_bound() -> None:
    query = BusinessQuery(question="有多少订单", objective="lookup", entities=["订单"])
    orders_object = metric(
        asset_type=SemanticAssetType.DATA_OBJECT,
        asset_id=ORDERS,
        name="订单",
        field_id=None,
        field_path=None,
    )
    context = QueryContextBuilder().build(
        _result(grounded_query(query, [("订单", orders_object)]))
    )
    plan = plan_for(context)

    assert plan.aggregates[0].function is AggregateFunction.COUNT
    assert plan.aggregates[0].field is None
    assert plan.aggregates[0].alias == "count"
    assert plan.expected_result_type is QueryResultType.SCALAR


def test_structured_derivation_overrides_the_default_aggregate() -> None:
    query = region_query(
        dimensions=[], derivations=[DerivationSpec(operation="average", metric="销售额")]
    )
    context = QueryContextBuilder().build(
        _result(grounded_query(query, [("销售额", metric())]))
    )
    plan = plan_for(context)

    assert plan.aggregates[0].function is AggregateFunction.AVERAGE
    assert plan.aggregates[0].alias == "average_pay_amount"


def test_plan_contract_cannot_carry_native_query_text() -> None:
    """A write intent cannot enter the plan: the contract has no field for a command or a language."""
    context = sales_context()
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        GroundedQueryPlan(
            plan_id="plan_manual",
            workspace_id=context.workspace_id,
            datasource_id=DS,
            data_object_ids=[ORDERS],
            aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count")],
            expected_result_type=QueryResultType.SCALAR,
            sql="DELETE FROM orders",
        )
    for forbidden in ("sql", "cypher", "cql", "flux", "command", "query", "query_language"):
        assert forbidden not in GroundedQueryPlan.model_fields


def test_plan_model_requires_a_join_for_several_objects() -> None:
    context = sales_context()
    with pytest.raises(ValueError, match="confirmed join"):
        GroundedQueryPlan(
            plan_id="plan_manual",
            workspace_id=context.workspace_id,
            datasource_id=DS,
            data_object_ids=[ORDERS, CUSTOMERS],
            aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count")],
            expected_result_type=QueryResultType.SCALAR,
        )


def test_join_must_match_the_confirmed_relationship_keys() -> None:
    query = BusinessQuery(
        question="按客户地区看销售额",
        objective="lookup",
        metrics=["销售额"],
        dimensions=["客户地区"],
    )
    customer_region = dimension(
        asset_id="dimension_customer_region",
        name="客户地区",
        data_object_id=CUSTOMERS,
        field_id="field_customer_region",
        field_path="region",
    )
    context = QueryContextBuilder().build(
        _result(
            grounded_query(
                query,
                [
                    ("销售额", metric()),
                    ("客户地区", customer_region),
                    ("foreign_key", relationship()),
                ],
            )
        )
    )
    tampered = PlanJoin(
        datasource_id=DS,
        relationship_id=EDGE,
        from_data_object_id=ORDERS,
        from_field_path="customer_id",
        to_data_object_id=CUSTOMERS,
        to_field_path="region",
    )
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[ORDERS, CUSTOMERS],
        data_objects=dict(context.data_objects),
        aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count")],
        joins=[tampered],
        expected_result_type=QueryResultType.SCALAR,
    )

    with pytest.raises(QueryPlanningError, match="连接的字段必须与已确认关系一致"):
        GroundedPlanValidator().validate(plan, context)


def test_join_relationship_must_be_allowlisted() -> None:
    context = sales_context()
    invented = PlanJoin(
        datasource_id=DS,
        relationship_id="edge_invented",
        from_data_object_id=ORDERS,
        from_field_path="region",
        to_data_object_id=ORDERS,
        to_field_path="region",
    )
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[ORDERS],
        data_objects=dict(context.data_objects),
        aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count")],
        joins=[invented],
        expected_result_type=QueryResultType.SCALAR,
    )

    with pytest.raises(QueryPlanningError, match="允许列表之外的关系"):
        GroundedPlanValidator().validate(plan, context)


def test_validator_rejects_a_plan_from_another_workspace_or_datasource() -> None:
    context = sales_context()
    base = {
        "plan_id": "plan_manual",
        "workspace_id": "other_workspace",
        "datasource_id": DS,
        "data_object_ids": [ORDERS],
        "aggregates": [PlanAggregate(function=AggregateFunction.COUNT, alias="count")],
        "expected_result_type": QueryResultType.SCALAR,
    }

    with pytest.raises(QueryPlanningError, match="工作区"):
        GroundedPlanValidator().validate(GroundedQueryPlan(**base), context)

    wrong_datasource = GroundedQueryPlan(**{**base, "workspace_id": context.workspace_id, "datasource_id": OTHER_DS})
    with pytest.raises(QueryPlanningError, match="不在接地允许列表中"):
        GroundedPlanValidator().validate(wrong_datasource, context)


def test_validator_rejects_a_sort_on_an_unknown_aggregate() -> None:
    context = sales_context()
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[ORDERS],
        data_objects=dict(context.data_objects),
        aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count")],
        group_by=[REGION],
        selected_fields=[REGION],
        sorts=[
            PlanSort(
                target=PlanSortTarget.AGGREGATE,
                aggregate_alias="sum_missing",
                direction=SortDirection.DESCENDING,
            )
        ],
        expected_result_type=QueryResultType.TABULAR,
    )

    with pytest.raises(QueryPlanningError, match="不存在的聚合"):
        GroundedPlanValidator().validate(plan, context)


def test_validator_rejects_duplicate_aliases_and_references() -> None:
    context = sales_context()
    duplicate_alias = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[ORDERS],
        data_objects=dict(context.data_objects),
        aggregates=[
            PlanAggregate(function=AggregateFunction.COUNT, alias="count"),
            PlanAggregate(function=AggregateFunction.COUNT, alias="count"),
        ],
        expected_result_type=QueryResultType.SCALAR,
    )
    with pytest.raises(QueryPlanningError, match="聚合别名必须唯一"):
        GroundedPlanValidator().validate(duplicate_alias, context)

    duplicate_field = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[ORDERS],
        data_objects=dict(context.data_objects),
        aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count")],
        selected_fields=[REGION, REGION],
        expected_result_type=QueryResultType.TABULAR,
    )
    with pytest.raises(QueryPlanningError, match="重复"):
        GroundedPlanValidator().validate(duplicate_field, context)


def test_aggregate_contract_requires_a_field_unless_counting() -> None:
    with pytest.raises(ValueError, match="only count"):
        PlanAggregate(function=AggregateFunction.SUM, alias="sum")
    assert PlanAggregate(function=AggregateFunction.COUNT, alias="count").field is None


def test_filter_contract_requires_a_bound_field_reference() -> None:
    with pytest.raises(ValueError, match="Field required"):
        PlanFilter(operator=FilterOperator.EQUALS, value="东京")
    assert PlanFilter(field=REGION, operator=FilterOperator.EQUALS, value="东京").value == "东京"


# --------------------------------------------------------------------------
# P1-04C1.1 — Execution Readiness Hardening
# --------------------------------------------------------------------------


def metric_with(aggregation: AggregateFunction, asset_id: str = "metric_with_aggr") -> metric:
    return metric(asset_id=asset_id, default_aggregation=aggregation)


def test_plan_uses_metric_default_aggregation_sum() -> None:
    sales = metric_with(AggregateFunction.SUM, asset_id="m_sum")
    context = QueryContextBuilder().build(
        _result(grounded_query(region_query(dimensions=[], metrics=["销售额"]), [("销售额", sales)]))
    )
    plan = plan_for(context)

    assert plan.aggregates[0].function is AggregateFunction.SUM


def test_plan_uses_metric_default_aggregation_average() -> None:
    sales = metric_with(AggregateFunction.AVERAGE, asset_id="m_avg")
    context = QueryContextBuilder().build(
        _result(
            grounded_query(
                region_query(dimensions=[], metrics=["平均订单金额"]),
                [("平均订单金额", sales)],
            )
        )
    )
    plan = plan_for(context)

    assert plan.aggregates[0].function is AggregateFunction.AVERAGE
    assert plan.aggregates[0].alias == "average_pay_amount"


def test_plan_uses_metric_default_aggregation_maximum() -> None:
    sales = metric_with(AggregateFunction.MAXIMUM, asset_id="m_max")
    context = QueryContextBuilder().build(
        _result(
            grounded_query(
                region_query(dimensions=[], metrics=["最大库存"]),
                [("最大库存", sales)],
            )
        )
    )
    plan = plan_for(context)

    assert plan.aggregates[0].function is AggregateFunction.MAXIMUM


def test_planner_fails_closed_when_metric_lacks_default_aggregation() -> None:
    bare = metric(asset_id="metric_bare", default_aggregation=None)
    with pytest.raises(QueryContextBuildError, match="没有受治理聚合语义"):
        QueryContextBuilder().build(
            _result(grounded_query(region_query(dimensions=[]), [("销售额", bare)]))
        )


def test_structured_derivation_can_override_default_aggregation() -> None:
    sales = metric_with(AggregateFunction.SUM, asset_id="m_sum")
    query = region_query(
        dimensions=[],
        derivations=[DerivationSpec(operation="average", metric="销售额")],
    )
    context = QueryContextBuilder().build(_result(grounded_query(query, [("销售额", sales)])))
    plan = plan_for(context)

    assert plan.aggregates[0].function is AggregateFunction.AVERAGE


def test_plan_carries_the_full_physical_object_locator() -> None:
    public_orders = metric(
        asset_id="metric_public_orders_amount",
        data_object_id="obj_public_orders",
        field_path="amount",
        qualified_name="public.orders",
        namespace="public",
        object_kind="table",
    )
    archive_orders = metric(
        asset_id="metric_archive_orders_amount",
        name="归档销售额",
        data_object_id="obj_archive_orders",
        field_path="amount",
        qualified_name="archive.orders",
        namespace="archive",
        object_kind="table",
    )
    public_dim = dimension(
        asset_id="dim_public_orders_date",
        name="订单日期",
        data_object_id="obj_public_orders",
        field_path="order_date",
        qualified_name="public.orders",
        namespace="public",
        object_kind="table",
    )
    edge = relationship(
        asset_id="edge_public_to_archive",
        relationship_id="edge_public_to_archive",
        from_data_object_id="obj_public_orders",
        from_field_path="public_id",
        to_data_object_id="obj_archive_orders",
        to_field_path="archive_id",
    )
    query = BusinessQuery(
        question="同名的 public 与 archive 销售额",
        objective="lookup",
        metrics=["销售额", "归档销售额"],
        dimensions=["订单日期"],
    )
    grounded = grounded_query(
        query,
        [
            ("销售额", public_orders),
            ("归档销售额", archive_orders),
            ("订单日期", public_dim),
            ("public_to_archive", edge),
        ],
    )
    context = QueryContextBuilder().build(_result(grounded))
    plan = plan_for(context)

    assert set(plan.data_objects) == {"obj_public_orders", "obj_archive_orders"}
    assert plan.data_objects["obj_public_orders"].qualified_name == "public.orders"
    assert plan.data_objects["obj_public_orders"].namespace == "public"
    assert plan.data_objects["obj_archive_orders"].qualified_name == "archive.orders"
    assert plan.data_objects["obj_archive_orders"].namespace == "archive"
    # Identities never collapse: the plan distinguishes the two tables by qualified_name.
    qualified_names = sorted(locator.qualified_name for locator in plan.data_objects.values())
    assert qualified_names == ["archive.orders", "public.orders"]


def test_plan_validator_refuses_locator_mismatch_with_plan_objects() -> None:
    public_orders = metric(
        asset_id="metric_public",
        data_object_id="obj_public_orders",
        qualified_name="public.orders",
        namespace="public",
        object_kind="table",
    )
    query = region_query(dimensions=[])
    context = QueryContextBuilder().build(_result(grounded_query(query, [("销售额", public_orders)])))
    orphan = GroundedDataObjectRef(
        datasource_id=DS,
        data_object_id="obj_somewhere_else",
        name="orphan",
        qualified_name="somewhere.else",
        namespace="somewhere",
        object_kind="table",
    )
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[public_orders.data_object_id],
        data_objects={orphan.data_object_id: orphan},
        aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count")],
        expected_result_type=QueryResultType.SCALAR,
    )

    with pytest.raises(QueryPlanningError, match="data_objects contains an object"):
        GroundedPlanValidator().validate(plan, context)


def test_plan_validator_blocks_a_guess_default_aggregation() -> None:
    """A plan that picks an aggregate function not declared by the metric is rejected."""
    sales = metric_with(AggregateFunction.AVERAGE, asset_id="m_avg_blocked")
    context = QueryContextBuilder().build(
        _result(
            grounded_query(
                region_query(dimensions=[], metrics=["平均订单金额"]),
                [("平均订单金额", sales)],
            )
        )
    )
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[ORDERS],
        data_objects=dict(context.data_objects),
        scan_version=context.scan_version,
        aggregates=[
            PlanAggregate(function=AggregateFunction.SUM, field=PAY, alias="sum_pay")
        ],
        expected_result_type=QueryResultType.SCALAR,
    )

    with pytest.raises(QueryPlanningError, match="受治理默认值不一致"):
        GroundedPlanValidator().validate(plan, context)


def test_plan_records_scan_version_from_context() -> None:
    sales = metric(asset_id="metric_for_version")
    grounded = grounded_query(region_query(dimensions=[]), [("销售额", sales)])
    grounded = grounded.model_copy(update={"scan_version": 7})
    context = QueryContextBuilder().build(_result(grounded))
    plan = plan_for(context)

    assert context.scan_version == 7
    assert plan.scan_version == 7


def test_plan_validator_rejects_orphan_scan_version() -> None:
    sales = metric(asset_id="metric_for_version_mismatch")
    grounded = grounded_query(region_query(dimensions=[]), [("销售额", sales)])
    grounded = grounded.model_copy(update={"scan_version": 7})
    context = QueryContextBuilder().build(_result(grounded))
    plan = plan_for(context)
    plan = plan.model_copy(update={"scan_version": 8})

    with pytest.raises(QueryPlanningError, match="扫描版本"):
        GroundedPlanValidator().validate(plan, context)


def test_data_object_locator_carries_native_graph_object_name() -> None:
    """Legacy profile path can no longer fabricate ``name`` from the business term.

    The native name must come from the published ``GraphDataObject.name``. On the legacy
    compatibility path the binding has no ``data_object_name``; the locator's ``name`` is then
    ``None`` so the future executor (P1-04C2) can refuse the plan rather than guess.
    """
    sales = metric(
        asset_id="metric_legacy_path",
        qualified_name=None,
        namespace=None,
        object_kind=None,
    )
    context = QueryContextBuilder().build(
        _result(grounded_query(region_query(dimensions=[]), [("销售额", sales)]))
    )
    plan = plan_for(context)

    locator = plan.data_objects[ORDERS]
    assert locator.data_object_id == ORDERS
    assert locator.namespace is None
    assert locator.qualified_name is None
    assert locator.object_kind is None
    assert locator.name is None


# --------------------------------------------------------------------------
# P1-04C1.2 — Final Execution Boundary Corrections
# --------------------------------------------------------------------------


def test_plan_validator_rejects_tampered_qualified_name() -> None:
    """A plan that keeps the graph identity but rewrites ``qualified_name`` is refused."""
    public_orders = metric(
        asset_id="metric_public_for_tamper",
        data_object_id="obj_public_orders",
        qualified_name="public.orders",
        namespace="public",
        object_kind="table",
    )
    query = region_query(dimensions=[])
    context = QueryContextBuilder().build(_result(grounded_query(query, [("销售额", public_orders)])))
    base = dict(context.data_objects[public_orders.data_object_id])
    base["qualified_name"] = "fake.orders"
    tampered = GroundedDataObjectRef(**base)
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[public_orders.data_object_id],
        data_objects={public_orders.data_object_id: tampered},
        aggregates=[PlanAggregate(function=AggregateFunction.SUM, field=PAY, alias="sum_pay")],
        selected_fields=[PAY],
        expected_result_type=QueryResultType.SCALAR,
    )

    with pytest.raises(QueryPlanningError, match="locator disagrees"):
        GroundedPlanValidator().validate(plan, context)


def test_plan_validator_rejects_tampered_namespace() -> None:
    """A plan that keeps the graph identity but rewrites ``namespace`` is refused."""
    public_orders = metric(
        asset_id="metric_public_for_namespace_tamper",
        data_object_id="obj_public_orders",
        qualified_name="public.orders",
        namespace="public",
        object_kind="table",
    )
    context = QueryContextBuilder().build(
        _result(grounded_query(region_query(dimensions=[]), [("销售额", public_orders)]))
    )
    base = dict(context.data_objects[public_orders.data_object_id])
    base["namespace"] = "archive"
    tampered = GroundedDataObjectRef(**base)
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[public_orders.data_object_id],
        data_objects={public_orders.data_object_id: tampered},
        aggregates=[PlanAggregate(function=AggregateFunction.SUM, field=PAY, alias="sum_pay")],
        selected_fields=[PAY],
        expected_result_type=QueryResultType.SCALAR,
    )

    with pytest.raises(QueryPlanningError, match="locator disagrees"):
        GroundedPlanValidator().validate(plan, context)


def test_plan_validator_rejects_tampered_native_name() -> None:
    """A plan that keeps the graph identity but rewrites ``name`` is refused."""
    public_orders = metric(
        asset_id="metric_public_for_name_tamper",
        data_object_id="obj_public_orders",
        qualified_name="public.orders",
        namespace="public",
        object_kind="table",
        data_object_name="orders",
    )
    context = QueryContextBuilder().build(
        _result(grounded_query(region_query(dimensions=[]), [("销售额", public_orders)]))
    )
    base = dict(context.data_objects[public_orders.data_object_id])
    base["name"] = "fake_orders"
    tampered = GroundedDataObjectRef(**base)
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[public_orders.data_object_id],
        data_objects={public_orders.data_object_id: tampered},
        aggregates=[PlanAggregate(function=AggregateFunction.SUM, field=PAY, alias="sum_pay")],
        selected_fields=[PAY],
        expected_result_type=QueryResultType.SCALAR,
    )

    with pytest.raises(QueryPlanningError, match="locator disagrees"):
        GroundedPlanValidator().validate(plan, context)


def test_plan_validator_rejects_missing_locator_for_plan_object() -> None:
    """A plan must declare a locator for every object it executes against."""
    public_orders = metric(
        asset_id="metric_public_for_missing_locator",
        data_object_id="obj_public_orders",
        qualified_name="public.orders",
        namespace="public",
        object_kind="table",
    )
    context = QueryContextBuilder().build(
        _result(grounded_query(region_query(dimensions=[]), [("销售额", public_orders)]))
    )
    plan = GroundedQueryPlan(
        plan_id="plan_manual",
        workspace_id=context.workspace_id,
        datasource_id=DS,
        data_object_ids=[public_orders.data_object_id],
        data_objects={},  # no locator for the only plan object
        aggregates=[PlanAggregate(function=AggregateFunction.SUM, field=PAY, alias="sum_pay")],
        selected_fields=[PAY],
        expected_result_type=QueryResultType.SCALAR,
    )

    with pytest.raises(QueryPlanningError, match="missing a data_objects locator"):
        GroundedPlanValidator().validate(plan, context)


def test_plan_validator_accepts_matching_scan_version() -> None:
    sales = metric(asset_id="metric_for_version_match")
    grounded = grounded_query(region_query(dimensions=[]), [("销售额", sales)])
    grounded = grounded.model_copy(update={"scan_version": 7})
    context = QueryContextBuilder().build(_result(grounded))
    plan = plan_for(context)

    assert context.scan_version == 7
    assert plan.scan_version == 7


def test_plan_validator_rejects_plan_scan_version_none_when_context_has_one() -> None:
    """A context with a version forces a version on the plan; ``None`` is no longer accepted."""
    sales = metric(asset_id="metric_for_version_drop")
    grounded = grounded_query(region_query(dimensions=[]), [("销售额", sales)])
    grounded = grounded.model_copy(update={"scan_version": 7})
    context = QueryContextBuilder().build(_result(grounded))
    plan = plan_for(context)
    plan = plan.model_copy(update={"scan_version": None})

    with pytest.raises(QueryPlanningError, match="扫描版本"):
        GroundedPlanValidator().validate(plan, context)


def test_plan_validator_accepts_compatibility_context_without_scan_version() -> None:
    """A compatibility context without ``scan_version`` may pair with a version-less plan."""
    sales = metric(asset_id="metric_for_no_version")
    grounded = grounded_query(region_query(dimensions=[]), [("销售额", sales)])
    context = QueryContextBuilder().build(_result(grounded))
    plan = plan_for(context)

    assert context.scan_version is None
    assert plan.scan_version is None
