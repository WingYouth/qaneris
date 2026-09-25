"""Query context construction: grounded in, planner-facing context out, nothing else read."""

from __future__ import annotations

from pathlib import Path

import pytest
from _grounded_fixtures import (
    DS,
    EDGE,
    ORDERS,
    ORDERS_PAY,
    ORDERS_REGION,
    OTHER_DS,
    _result,
    candidate,
    dimension,
    graph_backed_locator,
    grounded_query,
    metric,
    region_query,
    relationship,
    sales_context,
)

import smartdata.querying.context as context_module
import smartdata.querying.planning.grounded_planner as planner_module
from smartdata.common.errors import QueryContextBuildError
from smartdata.contracts.query import GroundedQueryContext, TimeRange, TimeSpec
from smartdata.contracts.semantic import (
    AggregateFunction,
    BusinessFilter,
    BusinessQuery,
    ComparisonSpec,
    DerivationSpec,
    RankingSpec,
    SemanticAssetType,
)
from smartdata.querying import QueryContextBuilder
from smartdata.querying.planning.grounded_planner import GroundedQueryPlanner
from smartdata.semantic.models import ClarificationOption, ClarificationRequest


def test_context_carries_only_grounded_physical_identity() -> None:
    context = sales_context()

    assert context.objective == "lookup"
    assert [item.business_term for item in context.metrics] == ["销售额"]
    assert [item.field_path for item in context.metrics] == ["pay_amount"]
    assert context.metrics[0].field_id == ORDERS_PAY
    assert context.metrics[0].default_aggregation == AggregateFunction.SUM
    assert [item.business_term for item in context.dimensions] == ["地区"]
    assert context.dimensions[0].field_id == ORDERS_REGION
    # Allowlists are exactly the grounding's, and nothing else is in the context.
    grounded = grounded_query(region_query(), [("销售额", metric()), ("地区", dimension())])
    assert context.allowed_datasource_ids == grounded.allowed_datasource_ids == [DS]
    assert context.allowed_data_object_ids == grounded.allowed_data_object_ids == [ORDERS]
    assert context.allowed_field_paths == grounded.allowed_field_paths
    assert context.allowed_field_ids == [ORDERS_PAY, ORDERS_REGION]
    assert context.allowed_relationship_ids == []
    assert context.unresolved_ambiguities == []
    assert context.workspace_id == "default"
    assert context.confidence == pytest.approx(0.9)
    # The context carries the graph-declared data object locator for everything the plan may touch.
    assert set(context.data_objects) == {ORDERS}
    assert context.data_objects[ORDERS].data_object_id == ORDERS


def test_context_copies_structured_intent_fields_verbatim() -> None:
    query = region_query(
        time_expression="2026年8月",
        requested_output=["table"],
        filters=[BusinessFilter(subject="地区", operator="equals", value="东京")],
        ranking=RankingSpec(direction="top", limit=10, metric="销售额"),
    )
    grounded = grounded_query(query, [("销售额", metric()), ("地区", dimension(data_type="date"))])
    context = QueryContextBuilder().build(
        _result(grounded),
        time_range=TimeRange(start="2026-08-01", end="2026-08-31"),
        time_spec=TimeSpec(dimension="地区"),
    )

    assert context.time_expression == "2026年8月"
    assert context.time_range == TimeRange(start="2026-08-01", end="2026-08-31")
    assert context.time_field is not None and context.time_field.field_path == "region"
    assert context.time_spec is not None and context.time_spec.dimension == "地区"
    assert context.time_field.is_date_type() is True
    assert context.requested_output == ["table"]
    assert context.ranking is not None and context.ranking.limit == 10
    assert context.business_query == query


def test_filter_keeps_the_business_value_out_of_the_allowlist() -> None:
    query = region_query(
        metrics=[], dimensions=[], filters=[BusinessFilter(subject="地区", operator="等于", value="东京")]
    )
    grounded = grounded_query(query, [("地区", dimension())])
    context = QueryContextBuilder().build(_result(grounded))

    assert [item.operator.value for item in context.filters] == ["equals"]
    assert context.filters[0].value == "东京"
    assert context.filters[0].field.field_id == ORDERS_REGION
    # The filter value is business data: it is not a physical asset and cannot be allowlisted.
    assert "东京" not in context.allowed_field_paths
    assert "东京" not in context.allowed_field_ids
    assert "东京" not in context.allowed_data_object_ids


@pytest.mark.parametrize(
    ("operator", "value"),
    [(">=", 100), ("not_equals", "华东"), ("between", [1, 10]), ("contains", "华")],
)
def test_supported_filter_operators_are_mapped(operator: str, value: object) -> None:
    query = region_query(
        metrics=[],
        dimensions=[],
        filters=[BusinessFilter(subject="地区", operator=operator, value=value)],
    )
    context = QueryContextBuilder().build(
        _result(grounded_query(query, [("地区", dimension())]))
    )

    assert len(context.filters) == 1
    assert context.filters[0].value == value


@pytest.mark.parametrize(
    ("operator", "value"),
    [("not_in", ["东京"]), ("matches", "x"), ("in", []), ("between", [1])],
)
def test_unsupported_or_malformed_filters_fail_closed(operator: str, value: object) -> None:
    query = region_query(
        metrics=[],
        dimensions=[],
        filters=[BusinessFilter(subject="地区", operator=operator, value=value)],
    )
    grounded = grounded_query(query, [("地区", dimension())])

    with pytest.raises(QueryContextBuildError):
        QueryContextBuilder().build(_result(grounded))


def test_unresolved_grounding_cannot_become_a_context() -> None:
    grounded = grounded_query(
        region_query(),
        [("销售额", metric()), ("地区", dimension())],
        unresolved=["指标“销售额”对应 2 个不同的物理绑定，未解决歧义"],
    )

    with pytest.raises(QueryContextBuildError, match="未解决歧义"):
        QueryContextBuilder().build(_result(grounded))


def test_pending_clarification_cannot_become_a_context() -> None:
    grounded = grounded_query(region_query(), [("销售额", metric())])
    clarifications = [
        ClarificationRequest(
            clarification_id="grounding_metric_sales",
            field="metric:销售额",
            question="“销售额”有多个企业定义，请确认你指的是：实收销售额、成交总额",
            options=[ClarificationOption(option_id="metric_paid_sales", label="实收销售额")],
        )
    ]

    with pytest.raises(QueryContextBuildError, match="澄清"):
        QueryContextBuilder().build(_result(grounded, clarifications))


def test_grounding_without_bindings_cannot_become_a_context() -> None:
    with pytest.raises(QueryContextBuildError, match="没有产生任何物理绑定"):
        QueryContextBuilder().build(_result(grounded_query(region_query(), [])))


def test_missing_dimension_binding_fails_closed() -> None:
    grounded = grounded_query(region_query(), [("销售额", metric())])

    with pytest.raises(QueryContextBuildError, match="维度“地区”"):
        QueryContextBuilder().build(_result(grounded))


def test_cross_source_binding_fails_closed() -> None:
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


def test_requested_datasource_must_be_grounded() -> None:
    grounded = grounded_query(region_query(), [("销售额", metric()), ("地区", dimension())])
    grounded_scoped = grounded_query(
        region_query(),
        [("销售额", metric()), ("地区", dimension())],
        requested_datasource_id=DS,
    )
    context = QueryContextBuilder().build(_result(grounded_scoped))
    assert context.requested_datasource_id == DS

    with pytest.raises(QueryContextBuildError, match="没有已落地的物理绑定"):
        QueryContextBuilder().build(
            _result(grounded), requested_datasource_id="ds_missing"
        )
    with pytest.raises(QueryContextBuildError, match="不一致"):
        QueryContextBuilder().build(
            _result(grounded_scoped), requested_datasource_id="ds_missing"
        )


def test_workspace_must_match_the_grounding() -> None:
    pairs = [("销售额", metric()), ("地区", dimension())]
    grounded = grounded_query(region_query(), pairs, workspace_id="workspace_a")
    context = QueryContextBuilder().build(_result(grounded))

    assert context.workspace_id == "workspace_a"
    with pytest.raises(QueryContextBuildError, match="工作区"):
        QueryContextBuilder().build(_result(grounded), workspace_id="workspace_b")


def test_comparison_is_out_of_phase_one_scope() -> None:
    query = region_query(
        objective="comparison", comparison=ComparisonSpec(comparison_type="year_over_year")
    )
    grounded = grounded_query(query, [("销售额", metric())])

    with pytest.raises(QueryContextBuildError, match="比较分析"):
        QueryContextBuilder().build(_result(grounded))


def test_supported_derivation_is_accepted_and_unsupported_one_is_refused() -> None:
    supported = region_query(
        dimensions=[], derivations=[DerivationSpec(operation="average", metric="销售额")]
    )
    grounded = grounded_query(supported, [("销售额", metric())])
    context = QueryContextBuilder().build(_result(grounded))
    assert [item.operation for item in context.derivations] == ["average"]

    unsupported = region_query(
        dimensions=[], derivations=[DerivationSpec(operation="year_over_year", metric="销售额")]
    )
    with pytest.raises(QueryContextBuildError, match="第一阶段"):
        QueryContextBuilder().build(
            _result(grounded_query(unsupported, [("销售额", metric())]))
        )


def test_ranking_requires_a_grounded_metric() -> None:
    grounded = grounded_query(
        region_query(dimensions=[], ranking=RankingSpec(direction="top", limit=10, metric="销售额")),
        [("销售额", metric())],
    )
    context = QueryContextBuilder().build(_result(grounded))
    assert context.ranking is not None and context.ranking.limit == 10

    unbound = region_query(
        dimensions=[], ranking=RankingSpec(direction="top", limit=10, metric="成交总额")
    )
    with pytest.raises(QueryContextBuildError, match="排名指标"):
        QueryContextBuilder().build(
            _result(grounded_query(unbound, [("销售额", metric())]))
        )

    no_metric = region_query(
        dimensions=[], metrics=[], ranking=RankingSpec(direction="top", limit=5)
    )
    with pytest.raises(QueryContextBuildError, match="排名缺少指标"):
        QueryContextBuilder().build(
            _result(grounded_query(no_metric, [("地区", dimension())]))
        )


def test_time_range_requires_a_grounded_time_field() -> None:
    grounded = grounded_query(region_query(), [("销售额", metric()), ("地区", dimension(data_type="date"))])
    range_ = TimeRange(start="2026-08-01", end="2026-08-31")

    with pytest.raises(QueryContextBuildError, match="时间范围"):
        QueryContextBuilder().build(_result(grounded), time_range=range_)
    with pytest.raises(QueryContextBuildError, match="时间维度"):
        QueryContextBuilder().build(
            _result(grounded),
            time_range=range_,
            time_spec=TimeSpec(dimension="下单日期"),
        )
    with pytest.raises(QueryContextBuildError, match="时间范围"):
        QueryContextBuilder().build(
            _result(grounded), time_spec=TimeSpec(dimension="地区")
        )


def test_time_field_must_be_a_date_column() -> None:
    """A bound dimension that is not a date/datetime column cannot be the time axis."""
    grounded = grounded_query(
        region_query(),
        [
            ("销售额", metric()),
            ("地区", dimension(data_type="text")),
        ],
    )
    range_ = TimeRange(start="2026-08-01", end="2026-08-31")

    with pytest.raises(QueryContextBuildError, match="日期"):
        QueryContextBuilder().build(
            _result(grounded),
            time_range=range_,
            time_spec=TimeSpec(dimension="地区"),
        )


def test_time_field_bare_time_type_cannot_be_time_range_axis() -> None:
    """A bare ``time`` column (within-day) is not a Phase 1 ``TimeRange`` axis.

    ``TimeRange`` carries date / datetime boundaries, so binding the time axis to a ``time``-typed
    column must fail closed — the query context refuses to be built.
    """
    grounded = grounded_query(
        region_query(),
        [
            ("销售额", metric()),
            ("地区", dimension(data_type="time")),
        ],
    )
    range_ = TimeRange(start="2026-08-01", end="2026-08-31")

    with pytest.raises(QueryContextBuildError, match="非日期字段"):
        QueryContextBuilder().build(
            _result(grounded),
            time_range=range_,
            time_spec=TimeSpec(dimension="地区"),
        )


def test_time_spec_requires_dimension_field() -> None:
    """``TimeSpec`` is converged: only the bound business dimension is allowed."""
    with pytest.raises(ValueError, match="dimension"):
        TimeSpec()
    # ``expression`` is no longer a ``TimeSpec`` slot; the natural-language time phrase lives on
    # ``BusinessQuery.time_expression`` and the normalised range lives on ``TimeRange``.
    with pytest.raises(ValueError, match="expression"):
        TimeSpec(expression="2026-08", dimension="地区")


def test_relationship_binding_requires_confirmed_join_keys() -> None:
    query = region_query(dimensions=["地区"])
    with_keys = grounded_query(
        query, [("销售额", metric()), ("地区", dimension()), ("foreign_key", relationship())]
    )
    context = QueryContextBuilder().build(_result(with_keys))

    assert len(context.relationships) == 1
    relation = context.relationships[0]
    assert relation.relationship_id == EDGE
    assert relation.from_field_path == "customer_id"
    assert relation.to_field_path == "id"
    assert relation.relationship_type == "foreign_key"
    assert context.allowed_relationship_ids == [EDGE]

    without_keys = relationship(from_field_path=None, to_field_path=None)
    keyless = grounded_query(
        query, [("销售额", metric()), ("地区", dimension()), ("foreign_key", without_keys)]
    )
    with pytest.raises(QueryContextBuildError, match="连接字段"):
        QueryContextBuilder().build(_result(keyless))


def test_binding_without_a_datasource_fails_closed() -> None:
    incomplete = metric(datasource_id=None)
    grounded = grounded_query(region_query(dimensions=[]), [("销售额", incomplete)])

    with pytest.raises(QueryContextBuildError, match="缺少数据源身份"):
        QueryContextBuilder().build(_result(grounded))


def test_conflicting_graph_field_identity_fails_closed() -> None:
    # Two candidates for one physical field with different graph identities contradict each other.
    query = region_query(dimensions=[])
    conflicting = metric(asset_id="metric_other_paid", field_id="field_other")
    grounded = grounded_query(
        query,
        [("销售额", metric())],
        candidates=[metric(), conflicting],
    )

    with pytest.raises(QueryContextBuildError, match="多个字段标识"):
        QueryContextBuilder().build(_result(grounded))


def test_context_is_deterministic() -> None:
    grounded = grounded_query(
        region_query(filters=[BusinessFilter(subject="地区", operator="equals", value="东京")]),
        [("销售额", metric()), ("地区", dimension())],
    )

    first = QueryContextBuilder().build(_result(grounded))
    second = QueryContextBuilder().build(_result(grounded))

    assert first.model_dump_json() == second.model_dump_json()


def test_context_model_refuses_unresolved_ambiguity_directly() -> None:
    """Even a hand-built context cannot smuggle an unresolved grounding into planning."""
    with pytest.raises(ValueError, match="unresolved ambiguities"):
        GroundedQueryContext(
            workspace_id="default",
            objective="lookup",
            business_query=region_query(),
            bindings=[
                {
                    "business_term": "销售额",
                    "asset_type": SemanticAssetType.METRIC,
                    "asset_id": "metric_paid_sales",
                    "datasource_id": DS,
                    "data_object_id": ORDERS,
                    "field_path": "pay_amount",
                    "score": 0.9,
                }
            ],
            allowed_datasource_ids=[DS],
            allowed_data_object_ids=[ORDERS],
            allowed_field_paths=["pay_amount"],
            unresolved_ambiguities=["未解决歧义"],
        )


@pytest.mark.parametrize(
    "module",
    [context_module, planner_module],
)
def test_context_and_planner_modules_cannot_reach_io(module) -> None:
    """The modules must not be able to read a database, the graph, a profile or a model."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "smartdata.adapters",
        "smartdata.graph",
        "smartdata.catalog",
        "smartdata.profiling",
        "smartdata.llm",
        "smartdata.scan",
        "sqlite3",
        "neo4j",
        "GraphReader",
        "SemanticRetriever",
    ):
        assert forbidden not in source, f"{module.__name__} must not reference {forbidden}"


def test_metric_without_default_aggregation_cannot_become_a_context() -> None:
    bare = metric(asset_id="metric_bare", default_aggregation=None)
    grounded = grounded_query(region_query(dimensions=[]), [("销售额", bare)])

    with pytest.raises(QueryContextBuildError, match="没有受治理聚合语义"):
        QueryContextBuilder().build(_result(grounded))


def test_plan_carries_distinct_locators_for_colliding_object_names() -> None:
    """``public.orders`` and ``archive.orders`` keep distinct ``qualified_name`` and ``namespace``."""
    public_orders = dimension(
        asset_id="dim_public_orders",
        data_object_id="obj_public_orders",
        qualified_name="public.orders",
        namespace="public",
        object_kind="table",
    )
    archive_orders = dimension(
        asset_id="dim_archive_orders",
        name="归档订单日期",
        data_object_id="obj_archive_orders",
        qualified_name="archive.orders",
        namespace="archive",
        object_kind="table",
    )
    customer = metric(
        asset_id="metric_orders_amount",
        name="订单数",
        data_object_id=public_orders.data_object_id,
        field_path="amount",
        qualified_name="public.orders",
        namespace="public",
        object_kind="table",
    )
    query = BusinessQuery(
        question="按订单日期看订单数",
        objective="lookup",
        metrics=["订单数"],
        dimensions=["订单日期"],
    )
    grounded = grounded_query(
        query,
        [
            ("订单数", customer),
            ("订单日期", public_orders),
            ("归档订单日期", archive_orders),
        ],
    )
    context = QueryContextBuilder().build(_result(grounded))

    public_id = public_orders.data_object_id
    assert public_id == "obj_public_orders"
    assert public_id in context.data_objects
    locator = context.data_objects[public_id]
    assert locator.qualified_name == "public.orders"
    assert locator.namespace == "public"
    assert locator.object_kind == "table"


# --------------------------------------------------------------------------
# P1-04C1.2 — Final Execution Boundary Corrections
# --------------------------------------------------------------------------


def test_locator_name_is_graph_data_object_name_not_business_term() -> None:
    """``name`` is the native ``GraphDataObject.name`` (e.g. ``orders``), never the business term."""
    sales = metric(asset_id="metric_native_name", **graph_backed_locator())
    grounded = grounded_query(region_query(dimensions=[]), [("销售额", sales)])
    context = QueryContextBuilder().build(_result(grounded))

    locator = context.data_objects[ORDERS]
    assert locator.name == "orders"
    assert locator.name != "销售额"
    assert locator.qualified_name == "public.orders"
    assert locator.namespace == "public"


def test_raw_field_candidate_carries_owning_data_object_locator() -> None:
    """A FIELD candidate bound by a filter slot keeps the owning ``GraphDataObject`` locator.

    Without this, a raw-field binding could only see ``field_path`` / ``data_type`` and the
    query context would have to go back to the graph to learn the parent table's native
    locator. Retrieval looks up the owning object inside the same structure read and stamps
    it onto every field candidate, so the context already knows ``public.orders`` / ``orders``
    / ``public`` / ``table`` when only the field was bound.
    """
    field_candidate = candidate(
        SemanticAssetType.FIELD,
        "field_orders_region",
        "地区",
        data_object_id=ORDERS,
        field_id=ORDERS_REGION,
        field_path="region",
        **graph_backed_locator(),
    )
    query = region_query(
        metrics=[], dimensions=[], filters=[BusinessFilter(subject="地区", operator="equals", value="东京")]
    )
    grounded = grounded_query(query, [("地区", field_candidate)])
    context = QueryContextBuilder().build(_result(grounded))

    assert ORDERS in context.data_objects
    locator = context.data_objects[ORDERS]
    assert locator.name == "orders"
    assert locator.namespace == "public"
    assert locator.qualified_name == "public.orders"
    assert locator.object_kind == "table"
    # The filter value is business data: the native locator never participates in the value path.
    assert "东京" not in context.allowed_field_paths


def test_candidate_object_locator_ignores_empty_candidates() -> None:
    """A candidate with no locator never disqualifies the data object a richer candidate described."""
    from smartdata.querying.context import _candidate_object_locator

    rich = candidate(
        SemanticAssetType.DATA_OBJECT,
        ORDERS,
        "orders",
        data_object_id=ORDERS,
        **graph_backed_locator(),
    )
    bare = candidate(
        SemanticAssetType.METRIC,
        "metric_bare",
        "销售额",
        data_object_id=ORDERS,
        field_path="pay_amount",
    )
    resolved = _candidate_object_locator([rich, bare])
    assert resolved[(DS, ORDERS)] == (
        "orders",
        "public",
        "public.orders",
        "table",
    )


def test_candidate_object_locator_rejects_multiple_different_populated_locators() -> None:
    """Two different populated locators for one data object fail closed."""
    from smartdata.querying.context import _candidate_object_locator

    public_orders = candidate(
        SemanticAssetType.DATA_OBJECT,
        ORDERS,
        "orders",
        data_object_id=ORDERS,
        **graph_backed_locator(),
    )
    archive_orders = candidate(
        SemanticAssetType.DATA_OBJECT,
        ORDERS,
        "orders",
        data_object_id=ORDERS,
        namespace="archive",
        qualified_name="archive.orders",
        object_kind="table",
        data_object_name="orders",
    )
    with pytest.raises(QueryContextBuildError, match="多种定位"):
        _candidate_object_locator([public_orders, archive_orders])


def test_candidate_object_locator_all_empty_returns_empty() -> None:
    """When every candidate is empty, the resolution records the compatibility state."""
    from smartdata.querying.context import _candidate_object_locator

    bare_a = candidate(
        SemanticAssetType.METRIC, "metric_bare_a", "A", data_object_id=ORDERS, field_path="a"
    )
    bare_b = candidate(
        SemanticAssetType.METRIC, "metric_bare_b", "B", data_object_id=ORDERS, field_path="b"
    )
    resolved = _candidate_object_locator([bare_a, bare_b])
    assert resolved[(DS, ORDERS)] == (None, None, None, None)


def test_candidate_object_locator_is_order_independent() -> None:
    """Reversing the candidate list does not change the resolved locator."""
    from smartdata.querying.context import _candidate_object_locator

    rich = candidate(
        SemanticAssetType.DATA_OBJECT,
        ORDERS,
        "orders",
        data_object_id=ORDERS,
        **graph_backed_locator(),
    )
    bare = candidate(
        SemanticAssetType.METRIC,
        "metric_bare",
        "销售额",
        data_object_id=ORDERS,
        field_path="pay_amount",
    )

    assert _candidate_object_locator([rich, bare]) == _candidate_object_locator([bare, rich])


def test_context_accepts_metric_with_no_default_when_derivation_overrides() -> None:
    """Default=None + explicit derivation => Context OK, Planner uses derivation's aggregate."""
    bare = metric(asset_id="metric_derived_only", default_aggregation=None)
    query = region_query(
        dimensions=[],
        metrics=["平均金额"],
        derivations=[DerivationSpec(operation="average", metric="平均金额")],
    )
    grounded = grounded_query(query, [("平均金额", bare)])
    context = QueryContextBuilder().build(_result(grounded))
    plan = GroundedQueryPlanner().plan(context)

    assert plan.aggregates[0].function is AggregateFunction.AVERAGE


def test_context_rejects_metric_with_no_default_and_no_derivation() -> None:
    """Default=None and no derivation => Context fails closed before planning."""
    bare = metric(asset_id="metric_bare_no_derivation", default_aggregation=None)
    query = region_query(dimensions=[], metrics=["销售额"])
    grounded = grounded_query(query, [("销售额", bare)])

    with pytest.raises(QueryContextBuildError, match="没有受治理聚合语义"):
        QueryContextBuilder().build(_result(grounded))


def test_context_rejects_metric_with_unsupported_derivation() -> None:
    """Default=None plus an unsupported derivation is still a fail-closed plan."""
    bare = metric(asset_id="metric_unsupported_derivation", default_aggregation=None)
    query = region_query(
        dimensions=[],
        metrics=["销售额"],
        derivations=[DerivationSpec(operation="year_over_year", metric="销售额")],
    )
    grounded = grounded_query(query, [("销售额", bare)])

    with pytest.raises(QueryContextBuildError, match="第一阶段"):
        QueryContextBuilder().build(_result(grounded))


def test_derivation_overrides_default_aggregation_in_context_and_plan() -> None:
    """Default=SUM + derivation=AVERAGE => Context OK, Planner uses AVERAGE."""
    sales = metric(asset_id="metric_default_sum", default_aggregation=AggregateFunction.SUM)
    query = region_query(
        dimensions=[],
        metrics=["销售额"],
        derivations=[DerivationSpec(operation="average", metric="销售额")],
    )
    context = QueryContextBuilder().build(_result(grounded_query(query, [("销售额", sales)])))
    plan = GroundedQueryPlanner().plan(context)

    assert plan.aggregates[0].function is AggregateFunction.AVERAGE
