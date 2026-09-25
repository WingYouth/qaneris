"""Planning end to end: real scan -> publication -> retrieval -> grounding -> context -> plan."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from _neo4j_fake import FakeNeo4jDriver, graph_reader, graph_store

from qaneris.adapters import create_adapter
from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.common.errors import QueryContextBuildError
from qaneris.contracts import Datasource
from qaneris.contracts.query import (
    AggregateFunction,
    FilterOperator,
    QueryResultType,
    SortDirection,
    TimeRange,
    TimeSpec,
)
from qaneris.contracts.semantic import BusinessFilter, BusinessQuery, RankingSpec
from qaneris.graph import GraphStructureRequest
from qaneris.scan import ScanService
from qaneris.semantic import (
    GraphSemanticRetriever,
    SemanticAsset,
    SemanticGrounder,
    SQLiteSemanticAssetRegistry,
)

DATASOURCE_ID = "ds_sales"


def create_database(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                region TEXT
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                pay_amount REAL,
                gross_amount REAL,
                region TEXT,
                order_date DATE,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            );
            INSERT INTO customers VALUES (1, 'Acme', '华东');
            INSERT INTO orders VALUES (1, 1, 120.0, 150.0, '华东', '2026-08-12');
            """
        )


def publish(driver: FakeNeo4jDriver, database) -> None:
    adapter = create_adapter(
        DATASOURCE_ID, "relational", {"driver": "sqlite", "path": str(database)}
    )
    datasource = Datasource(
        id=DATASOURCE_ID,
        name="sales",
        kind="relational",
        workspace_id="default",
        driver="sqlite",
    )
    ScanService(graph_store(driver)).scan(datasource, adapter, version=1)


def asset(
    asset_id: str,
    kind: str,
    name: str,
    *,
    object_id: str,
    field_id: str,
    field_path: str,
    data_object_name: str,
    aliases: tuple[str, ...] = (),
    default_aggregation: AggregateFunction | None = None,
    time_axis: bool = False,
) -> SemanticAsset:
    return SemanticAsset(
        asset_id=asset_id,
        workspace_id="default",
        kind=kind,
        name=name,
        aliases=list(aliases),
        source="administrator",
        source_ref=f"seed://retail/{asset_id}",
        status="published",
        confidence=1.0,
        datasource_id=DATASOURCE_ID,
        datasource_name="sales",
        graph_data_object_id=object_id,
        graph_field_id=field_id,
        data_object_name=data_object_name,
        field_path=field_path,
        default_aggregation=default_aggregation,
        time_axis=time_axis,
        updated_at=datetime.now(UTC),
    )


def graph_identity(reader, object_name: str, field_name: str):
    structure = reader.read_structure(GraphStructureRequest(datasource_id=DATASOURCE_ID))
    data_object = structure.object_by_name(DATASOURCE_ID, object_name)
    assert data_object is not None
    field = next(
        item
        for item in structure.fields
        if item.object_id == data_object.node_id and item.path == field_name
    )
    return data_object, field


def governed_assets(reader, *, ambiguous_sales: bool = False) -> list[SemanticAsset]:
    orders, pay_amount = graph_identity(reader, "orders", "pay_amount")
    _, gross_amount = graph_identity(reader, "orders", "gross_amount")
    _, order_region = graph_identity(reader, "orders", "region")
    _, order_date = graph_identity(reader, "orders", "order_date")
    customers, customer_region = graph_identity(reader, "customers", "region")
    gross_aliases = ("销售额", "GMV") if ambiguous_sales else ("GMV",)
    return [
        asset(
            "metric_paid_sales",
            "metric",
            "实收销售额",
            object_id=orders.node_id,
            field_id=pay_amount.node_id,
            field_path="pay_amount",
            data_object_name="orders",
            aliases=("销售额", "实收金额"),
            default_aggregation=AggregateFunction.SUM,
        ),
        asset(
            "metric_gross_sales",
            "metric",
            "成交总额",
            object_id=orders.node_id,
            field_id=gross_amount.node_id,
            field_path="gross_amount",
            data_object_name="orders",
            aliases=gross_aliases,
            default_aggregation=AggregateFunction.SUM,
        ),
        asset(
            "dimension_region",
            "dimension",
            "地区",
            object_id=orders.node_id,
            field_id=order_region.node_id,
            field_path="region",
            data_object_name="orders",
            aliases=("区域",),
        ),
        asset(
            "dimension_order_date",
            "dimension",
            "下单日期",
            object_id=orders.node_id,
            field_id=order_date.node_id,
            field_path="order_date",
            data_object_name="orders",
            # The governed business time axis. A time expression is only executable through an
            # axis an administrator declared; nothing selects the axis by data type or name.
            time_axis=True,
        ),
        asset(
            "dimension_customer_region",
            "dimension",
            "客户地区",
            object_id=customers.node_id,
            field_id=customer_region.node_id,
            field_path="region",
            data_object_name="customers",
        ),
    ]


def registry_with(assets: list[SemanticAsset], path) -> SQLiteSemanticAssetRegistry:
    registry = SQLiteSemanticAssetRegistry(path)
    for item in assets:
        registry.save(item)
    return registry


def scanned(tmp_path):
    driver = FakeNeo4jDriver()
    database = tmp_path / "sales.db"
    create_database(database)
    publish(driver, database)
    reader = graph_reader(driver)
    return driver, reader, tmp_path / "catalog.db"


def retrieve(reader, catalog_path, query: BusinessQuery):
    registry = registry_with(governed_assets(reader), catalog_path)
    return GraphSemanticRetriever(reader, registry).retrieve(query, limit=50)


def plan_of(driver, reader, catalog_path, query: BusinessQuery, **build):
    service = QanerisService(Catalog(catalog_path), graph_reader=reader)
    retrieval = retrieve(reader, catalog_path, query)
    grounding = SemanticGrounder().ground(query, retrieval)
    context = service.build_query_context(grounding, **build)
    reads_before = len(driver.reads)
    plan = service.plan_grounded_query(context)
    # Context building and planning must not read the graph, catalog, profile or a model again.
    assert len(driver.reads) == reads_before
    return retrieval, grounding, context, plan


def sales_query(**overrides) -> BusinessQuery:
    values = {
        "question": "按地区看销售额",
        "objective": "lookup",
        "metrics": ["销售额"],
        "dimensions": ["地区"],
    }
    return BusinessQuery(**{**values, **overrides})


def test_metric_and_dimension_plan_from_the_published_graph(tmp_path) -> None:
    _driver, reader, catalog_path = scanned(tmp_path)
    orders, pay_amount = graph_identity(reader, "orders", "pay_amount")
    _, region = graph_identity(reader, "orders", "region")

    _retrieval, grounding, context, plan = plan_of(reader=reader, driver=_driver, catalog_path=catalog_path, query=sales_query())

    assert grounding.is_executable is True
    assert context.allowed_data_object_ids == [orders.node_id]
    assert plan.datasource_id == DATASOURCE_ID
    assert plan.data_object_ids == [orders.node_id]
    assert [item.field.field_id for item in plan.aggregates if item.field] == [pay_amount.node_id]
    assert [item.field.field_path for item in plan.aggregates if item.field] == ["pay_amount"]
    assert plan.aggregates[0].function is AggregateFunction.SUM
    assert [item.field_id for item in plan.group_by] == [region.node_id]
    assert [item.field_path for item in plan.group_by] == ["region"]
    assert plan.expected_result_type is QueryResultType.TABULAR
    # The unbound 成交总额 candidate never reaches the plan.
    assert "gross_amount" not in plan.model_dump_json()
    assert plan.joins == []


def test_filter_ranking_and_time_come_from_structured_inputs(tmp_path) -> None:
    _driver, reader, catalog_path = scanned(tmp_path)
    _, region = graph_identity(reader, "orders", "region")
    _, order_date = graph_identity(reader, "orders", "order_date")
    query = sales_query(
        dimensions=["下单日期"],
        objective="ranking",
        ranking=RankingSpec(direction="top", limit=10, metric="销售额"),
        filters=[BusinessFilter(subject="地区", operator="=", value="华东")],
        time_expression="2026年8月",
    )

    _retrieval, _grounding, context, plan = plan_of(
        driver=_driver,
        reader=reader,
        catalog_path=catalog_path,
        query=query,
        time_range=TimeRange(start="2026-08-01", end="2026-08-31"),
        time_spec=TimeSpec(dimension="下单日期"),
    )

    assert context.time_expression == "2026年8月"
    assert context.time_spec is not None and context.time_spec.dimension == "下单日期"
    assert plan.time_range == TimeRange(start="2026-08-01", end="2026-08-31")
    assert plan.time_field is not None and plan.time_field.field_id == order_date.node_id
    assert plan.time_field.is_date_type() is True
    assert plan.limit == 10
    assert [item.field_id for item in plan.group_by] == [order_date.node_id]
    assert plan.sorts[0].direction is SortDirection.DESCENDING
    assert plan.filters[0].operator is FilterOperator.EQUALS
    assert plan.filters[0].value == "华东"
    assert plan.filters[0].field.field_id == region.node_id
    assert plan.joins == []


def test_cross_object_planning_uses_the_published_relationship(tmp_path) -> None:
    _driver, reader, catalog_path = scanned(tmp_path)
    orders, pay_amount = graph_identity(reader, "orders", "pay_amount")
    customers, customer_region = graph_identity(reader, "customers", "region")
    query = sales_query(
        question="按客户地区看销售额", metrics=["销售额"], dimensions=["客户地区"]
    )

    _retrieval, _grounding, context, plan = plan_of(
        driver=_driver, reader=reader, catalog_path=catalog_path, query=query
    )

    assert len(context.relationships) == 1
    relationship = context.relationships[0]
    assert relationship.from_field_path == "customer_id"
    assert relationship.to_field_path == "id"
    assert len(plan.joins) == 1
    join = plan.joins[0]
    assert join.relationship_id == relationship.relationship_id
    assert {join.from_data_object_id, join.to_data_object_id} == {
        orders.node_id,
        customers.node_id,
    }
    assert sorted(plan.data_object_ids) == sorted([orders.node_id, customers.node_id])
    assert [item.field.field_id for item in plan.aggregates if item.field] == [pay_amount.node_id]
    assert [item.field_id for item in plan.group_by] == [customer_region.node_id]
    assert "customer_id" in plan.model_dump_json()


def test_ambiguity_stops_before_the_context(tmp_path) -> None:
    _driver, reader, catalog_path = scanned(tmp_path)
    registry = registry_with(governed_assets(reader, ambiguous_sales=True), catalog_path)
    query = sales_query(dimensions=[], metrics=["销售额"])
    retrieval = GraphSemanticRetriever(reader, registry).retrieve(query, limit=50)
    grounding = SemanticGrounder().ground(query, retrieval)

    assert grounding.is_executable is False
    assert grounding.clarifications
    with pytest.raises(QueryContextBuildError, match="未解决歧义"):
        QanerisService(Catalog(catalog_path), graph_reader=reader).build_query_context(grounding)


def test_planning_is_deterministic_over_the_published_graph(tmp_path) -> None:
    driver, reader, catalog_path = scanned(tmp_path)
    query = sales_query(
        objective="ranking",
        ranking=RankingSpec(direction="top", limit=5, metric="销售额"),
    )
    first = plan_of(driver=driver, reader=reader, catalog_path=catalog_path, query=query)[3]
    second = plan_of(driver=driver, reader=reader, catalog_path=catalog_path, query=query)[3]

    assert first.model_dump_json() == second.model_dump_json()
    assert first.plan_id == second.plan_id


def test_service_retrieve_ground_build_plan_chain(tmp_path) -> None:
    _driver, reader, catalog_path = scanned(tmp_path)
    registry_with(governed_assets(reader), catalog_path)
    service = QanerisService(Catalog(catalog_path), graph_reader=reader)
    query = sales_query()

    retrieval = service.retrieve_semantics(query, requested_datasource_id=DATASOURCE_ID, limit=50)
    grounding = service.ground_semantics(query, retrieval)
    context = service.build_query_context(grounding)
    plan = service.plan_grounded_query(context)

    assert retrieval.workspace_id == "default"
    assert grounding.grounded_query.requested_datasource_id == DATASOURCE_ID
    assert context.requested_datasource_id == DATASOURCE_ID
    assert context.workspace_id == "default"
    assert plan.datasource_id == DATASOURCE_ID
    assert plan.workspace_id == "default"
    assert plan.data_object_ids == context.allowed_data_object_ids


def test_planning_never_reads_the_profile(tmp_path, monkeypatch) -> None:
    """The new main chain must not touch CompanyDataProfile physical structure."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the planning chain must not read CompanyDataProfile")

    _driver, reader, catalog_path = scanned(tmp_path)
    monkeypatch.setattr(Catalog, "get_company_data_profile", forbidden)
    _retrieval, _grounding, context, plan = plan_of(
        driver=_driver, reader=reader, catalog_path=catalog_path, query=sales_query()
    )

    assert plan.data_object_ids == context.allowed_data_object_ids
