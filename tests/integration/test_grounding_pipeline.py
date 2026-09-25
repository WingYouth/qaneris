"""Grounding end to end: real scan -> publication -> retrieval -> grounded query."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from _neo4j_fake import FakeNeo4jDriver, graph_reader, graph_store

from qaneris.adapters import create_adapter
from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.contracts import DatasetInfo, Datasource, FieldInfo
from qaneris.contracts.semantic import BusinessFilter, BusinessQuery
from qaneris.graph import GraphStructureRequest
from qaneris.scan import ScanGraphBuilder, ScanService
from qaneris.semantic import (
    GraphSemanticRetriever,
    SemanticAsset,
    SemanticGrounder,
    SQLiteSemanticAssetRegistry,
)

DATASOURCE_ID = "ds_sales"


def create_database(path, columns: tuple[str, ...] = ("pay_amount", "gross_amount", "region")) -> None:
    fields = ",\n                ".join(
        f"{name} {'REAL' if 'amount' in name else 'TEXT'}" for name in columns
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                {fields},
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            );
            INSERT INTO customers VALUES (1, 'Acme');
            INSERT INTO orders (id, customer_id, region) VALUES (1, 1, '华东');
            """
        )


def publish(driver: FakeNeo4jDriver, database, *, version: int = 1) -> None:
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
    ScanService(graph_store(driver)).scan(datasource, adapter, version=version)


def publish_graph(
    driver: FakeNeo4jDriver,
    datasets: list[DatasetInfo],
    relations=None,
    *,
    datasource_id: str = DATASOURCE_ID,
) -> None:
    graph = ScanGraphBuilder().build(
        Datasource(
            id=datasource_id,
            name=datasource_id,
            kind="relational",
            workspace_id="default",
            driver="sqlite",
        ),
        datasets,
        relations or [],
        version=1,
        scanned_at=datetime.now(UTC),
    )
    graph_store(driver).replace_datasource_graph(graph)


def asset(
    asset_id: str,
    kind: str,
    name: str,
    *,
    object_id: str,
    field_id: str,
    field_path: str,
    aliases: tuple[str, ...] = (),
    datasource_id: str = DATASOURCE_ID,
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
        datasource_id=datasource_id,
        datasource_name="sales",
        graph_data_object_id=object_id,
        graph_field_id=field_id,
        data_object_name="orders",
        field_path=field_path,
        updated_at=datetime.now(UTC),
    )


def registry_with(assets: list[SemanticAsset], path) -> SQLiteSemanticAssetRegistry:
    registry = SQLiteSemanticAssetRegistry(path)
    for item in assets:
        registry.save(item)
    return registry


def graph_ids(reader, datasource_id: str, object_name: str, field_name: str):
    structure = reader.read_structure(GraphStructureRequest(datasource_id=datasource_id))
    data_object = structure.object_by_name(datasource_id, object_name)
    assert data_object is not None
    field = next(
        item
        for item in structure.fields
        if item.object_id == data_object.node_id and item.path == field_name
    )
    return data_object, field


def sales_assets(reader, *, ambiguous_sales: bool = False) -> list[SemanticAsset]:
    """Governed assets for the scanned ``orders`` object.

    With ``ambiguous_sales`` the shared alias 销售额 maps to two different physical fields, which is
    exactly the ambiguity grounding must refuse to resolve on its own.
    """
    orders, pay_amount = graph_ids(reader, DATASOURCE_ID, "orders", "pay_amount")
    _, gross_amount = graph_ids(reader, DATASOURCE_ID, "orders", "gross_amount")
    _, region = graph_ids(reader, DATASOURCE_ID, "orders", "region")
    gross_aliases = ("销售额", "GMV") if ambiguous_sales else ("GMV",)
    return [
        asset(
            "metric_paid_sales",
            "metric",
            "实收销售额",
            object_id=orders.node_id,
            field_id=pay_amount.node_id,
            field_path="pay_amount",
            aliases=("销售额", "实收金额"),
        ),
        asset(
            "metric_gross_sales",
            "metric",
            "成交总额",
            object_id=orders.node_id,
            field_id=gross_amount.node_id,
            field_path="gross_amount",
            aliases=gross_aliases,
        ),
        asset(
            "dimension_region",
            "dimension",
            "地区",
            object_id=orders.node_id,
            field_id=region.node_id,
            field_path="region",
            aliases=("区域",),
        ),
    ]


def region_query() -> BusinessQuery:
    return BusinessQuery(
        question="按地区看销售额",
        objective="lookup",
        metrics=["销售额"],
        dimensions=["地区"],
    )


def scanned(tmp_path, *, columns: tuple[str, ...] = ("pay_amount", "gross_amount", "region")):
    driver = FakeNeo4jDriver()
    database = tmp_path / "sales.db"
    create_database(database, columns)
    publish(driver, database)
    return driver, graph_reader(driver)


def test_grounded_query_keeps_only_the_bound_physical_assets(tmp_path) -> None:
    driver, reader = scanned(tmp_path)
    registry = registry_with(sales_assets(reader), tmp_path / "catalog.db")
    query = region_query()

    retrieval = GraphSemanticRetriever(reader, registry).retrieve(
        query, requested_datasource_id=DATASOURCE_ID, limit=50
    )
    result = SemanticGrounder().ground(query, retrieval)

    bindings = {
        binding.business_term: (
            binding.asset_type.value,
            binding.asset_id,
            binding.datasource_id,
            binding.data_object_id,
            binding.field_path,
        )
        for binding in result.grounded_query.bindings
    }
    orders, _ = graph_ids(reader, DATASOURCE_ID, "orders", "pay_amount")
    assert bindings["销售额"] == (
        "metric",
        "metric_paid_sales",
        DATASOURCE_ID,
        orders.node_id,
        "pay_amount",
    )
    assert bindings["地区"] == (
        "dimension",
        "dimension_region",
        DATASOURCE_ID,
        orders.node_id,
        "region",
    )
    # Minimal allowlist: the retrieval candidate bound to gross_amount was never bound.
    assert result.grounded_query.allowed_field_paths == ["pay_amount", "region"]
    assert result.grounded_query.allowed_datasource_ids == [DATASOURCE_ID]
    assert result.grounded_query.allowed_data_object_ids == [orders.node_id]
    assert result.grounded_query.allowed_relationship_ids == []
    assert result.grounded_query.unresolved_ambiguities == []
    assert result.is_executable is True
    assert driver.nodes  # the graph was really published into, not simulated in grounding


def test_ambiguous_sales_definition_stops_at_clarification(tmp_path) -> None:
    _driver, reader = scanned(tmp_path)
    registry = registry_with(
        sales_assets(reader, ambiguous_sales=True), tmp_path / "catalog.db"
    )
    query = BusinessQuery(question="销售额", objective="lookup", metrics=["销售额"])

    retrieval = GraphSemanticRetriever(reader, registry).retrieve(
        query, requested_datasource_id=DATASOURCE_ID, limit=50
    )
    result = SemanticGrounder().ground(query, retrieval)

    assert result.grounded_query.bindings == []
    assert result.grounded_query.allowed_field_paths == []
    assert result.is_executable is False
    assert result.needs_clarification is True
    option_labels = {option.label for option in result.clarifications[0].options}
    assert option_labels == {"成交总额", "实收销售额"}


def test_cross_object_binding_uses_the_published_relates_to_edge(tmp_path) -> None:
    _driver, reader = scanned(tmp_path)
    orders, _ = graph_ids(reader, DATASOURCE_ID, "orders", "pay_amount")
    customers, customer_name = graph_ids(reader, DATASOURCE_ID, "customers", "name")
    registry = registry_with(
        [
            *sales_assets(reader),
            asset(
                "dimension_customer_name",
                "dimension",
                "客户名称",
                object_id=customers.node_id,
                field_id=customer_name.node_id,
                field_path="name",
            ),
        ],
        tmp_path / "catalog.db",
    )
    query = BusinessQuery(
        question="按客户名称看销售额",
        objective="lookup",
        metrics=["销售额"],
        dimensions=["客户名称"],
    )

    retrieval = GraphSemanticRetriever(reader, registry).retrieve(
        query, requested_datasource_id=DATASOURCE_ID, limit=50
    )
    result = SemanticGrounder().ground(query, retrieval)

    relationship_ids = result.grounded_query.allowed_relationship_ids
    assert len(relationship_ids) == 1
    assert any(
        binding.relationship_id == relationship_ids[0]
        for binding in result.grounded_query.bindings
    )
    assert sorted(result.grounded_query.allowed_data_object_ids) == sorted(
        [orders.node_id, customers.node_id]
    )
    assert result.is_executable is True


def test_cross_object_binding_without_a_relationship_is_not_executable(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    publish_graph(
        driver,
        [
            DatasetInfo(
                datasource_id=DATASOURCE_ID,
                name="orders",
                fields=[
                    FieldInfo(name="pay_amount", data_type="decimal"),
                    FieldInfo(name="customer_ref", data_type="integer"),
                ],
            ),
            DatasetInfo(
                datasource_id=DATASOURCE_ID,
                name="customers",
                fields=[
                    FieldInfo(name="id", data_type="integer"),
                    FieldInfo(name="customer_ref", data_type="integer"),
                ],
            ),
        ],
        [],  # same-named column, but no confirmed relationship
    )
    reader = graph_reader(driver)
    orders, pay_amount = graph_ids(reader, DATASOURCE_ID, "orders", "pay_amount")
    customers, customer_ref = graph_ids(reader, DATASOURCE_ID, "customers", "customer_ref")
    registry = registry_with(
        [
            asset(
                "metric_paid_sales",
                "metric",
                "实收销售额",
                object_id=orders.node_id,
                field_id=pay_amount.node_id,
                field_path="pay_amount",
                aliases=("销售额",),
            ),
            asset(
                "dimension_customer_ref",
                "dimension",
                "客户编号",
                object_id=customers.node_id,
                field_id=customer_ref.node_id,
                field_path="customer_ref",
            ),
        ],
        tmp_path / "catalog.db",
    )
    query = BusinessQuery(
        question="按客户编号看销售额",
        objective="lookup",
        metrics=["销售额"],
        dimensions=["客户编号"],
    )

    retrieval = GraphSemanticRetriever(reader, registry).retrieve(
        query, requested_datasource_id=DATASOURCE_ID, limit=50
    )
    result = SemanticGrounder().ground(query, retrieval)

    assert result.grounded_query.allowed_relationship_ids == []
    assert any(
        "没有已确认的 RELATES_TO" in problem
        for problem in result.grounded_query.unresolved_ambiguities
    )
    assert result.is_executable is False


def test_cross_datasource_binding_is_not_executable(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    database = tmp_path / "sales.db"
    create_database(database)
    publish(driver, database)
    publish_graph(
        driver,
        [
            DatasetInfo(
                datasource_id="ds_other",
                name="other_orders",
                fields=[FieldInfo(name="amount", data_type="decimal")],
            )
        ],
        datasource_id="ds_other",
    )
    reader = graph_reader(driver)
    _, other_amount = graph_ids(reader, "ds_other", "other_orders", "amount")
    registry = registry_with(
        [
            *sales_assets(reader),
            asset(
                "metric_port_amount",
                "metric",
                "口岸金额",
                object_id=other_amount.object_id,
                field_id=other_amount.node_id,
                field_path="amount",
                datasource_id="ds_other",
            ),
        ],
        tmp_path / "catalog.db",
    )
    query = BusinessQuery(
        question="按地区看口岸金额",
        objective="lookup",
        metrics=["口岸金额"],
        dimensions=["地区"],
    )

    retrieval = GraphSemanticRetriever(reader, registry).retrieve(query, limit=50)
    result = SemanticGrounder().ground(query, retrieval)

    assert any(
        "跨数据源" in problem for problem in result.grounded_query.unresolved_ambiguities
    )
    assert result.is_executable is False


def test_grounding_does_not_recover_a_field_removed_by_rescan(tmp_path) -> None:
    driver, reader = scanned(tmp_path)
    registry = registry_with(sales_assets(reader), tmp_path / "catalog.db")
    query = BusinessQuery(question="销售额", objective="lookup", metrics=["销售额"])
    retriever = GraphSemanticRetriever(reader, registry)

    before = SemanticGrounder().ground(
        query, retriever.retrieve(query, requested_datasource_id=DATASOURCE_ID, limit=50)
    )
    assert any(
        binding.field_path == "pay_amount" for binding in before.grounded_query.bindings
    )

    rescan = tmp_path / "sales_v2.db"
    create_database(rescan, ("gross_amount", "region"))
    publish(driver, rescan, version=2)

    after = SemanticGrounder().ground(
        query, retriever.retrieve(query, requested_datasource_id=DATASOURCE_ID, limit=50)
    )

    assert after.grounded_query.bindings == []
    assert after.grounded_query.allowed_field_paths == []
    assert "pay_amount" not in after.grounded_query.model_dump_json()
    assert after.is_executable is False


def test_service_retrieve_then_ground_chain(tmp_path) -> None:
    _driver, reader = scanned(tmp_path)
    database = tmp_path / "sales.db"
    catalog_path = tmp_path / "catalog.db"
    registry_with(sales_assets(reader), catalog_path)
    service = QanerisService(Catalog(catalog_path), graph_reader=reader)
    query = BusinessQuery(
        question="地区=华东的销售额",
        objective="lookup",
        metrics=["销售额"],
        dimensions=["地区"],
        filters=[BusinessFilter(subject="地区", operator="=", value="华东")],
    )

    retrieval = service.retrieve_semantics(
        query, requested_datasource_id=DATASOURCE_ID, limit=50
    )
    result = service.ground_semantics(query, retrieval)

    orders, _ = graph_ids(reader, DATASOURCE_ID, "orders", "pay_amount")
    assert result.is_executable is True
    assert result.grounded_query.allowed_data_object_ids == [orders.node_id]
    assert result.grounded_query.allowed_field_paths == ["pay_amount", "region"]
    assert "华东" not in str([binding.model_dump() for binding in result.grounded_query.bindings])
    assert database.exists()


def test_shared_generic_suffix_does_not_claim_an_expression(tmp_path) -> None:
    """Recall may offer an asset that merely shares a suffix; grounding must not treat it as a match.

    实收销售额 carries the alias 实收金额 and 口岸金额 shares only the suffix 金额 with it, so the
    expression 口岸金额 addresses exactly one asset and must bind without a clarification.
    """
    _driver, reader = scanned(tmp_path)
    orders, _ = graph_ids(reader, DATASOURCE_ID, "orders", "pay_amount")
    _, gross_amount = graph_ids(reader, DATASOURCE_ID, "orders", "gross_amount")
    registry = registry_with(
        [
            *sales_assets(reader),
            asset(
                "metric_port_amount",
                "metric",
                "口岸金额",
                object_id=orders.node_id,
                field_id=gross_amount.node_id,
                field_path="gross_amount",
            ),
        ],
        tmp_path / "catalog.db",
    )
    query = BusinessQuery(
        question="按地区看口岸金额",
        objective="lookup",
        metrics=["口岸金额"],
        dimensions=["地区"],
    )

    retrieval = GraphSemanticRetriever(reader, registry).retrieve(
        query, requested_datasource_id=DATASOURCE_ID, limit=50
    )
    result = SemanticGrounder().ground(query, retrieval)

    assert any(
        candidate.name == "实收销售额" for candidate in retrieval.candidates
    )  # offered by recall
    bindings = {
        binding.business_term: binding.asset_id for binding in result.grounded_query.bindings
    }
    assert bindings == {"口岸金额": "metric_port_amount", "地区": "dimension_region"}
    assert result.grounded_query.unresolved_ambiguities == []
    assert result.is_executable is True


def test_grounding_is_deterministic_over_the_published_graph(tmp_path) -> None:
    _driver, reader = scanned(tmp_path)
    registry = registry_with(sales_assets(reader), tmp_path / "catalog.db")
    query = region_query()
    retriever = GraphSemanticRetriever(reader, registry)

    first = SemanticGrounder().ground(
        query, retriever.retrieve(query, requested_datasource_id=DATASOURCE_ID, limit=50)
    )
    second = SemanticGrounder().ground(
        query, retriever.retrieve(query, requested_datasource_id=DATASOURCE_ID, limit=50)
    )

    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.parametrize("field_path", ["pay_amount", "region"])
def test_allowlist_field_paths_are_real_graph_fields(tmp_path, field_path: str) -> None:
    _driver, reader = scanned(tmp_path)
    registry = registry_with(sales_assets(reader), tmp_path / "catalog.db")
    query = region_query()

    retrieval = GraphSemanticRetriever(reader, registry).retrieve(
        query, requested_datasource_id=DATASOURCE_ID, limit=50
    )
    result = SemanticGrounder().ground(query, retrieval)
    structure = reader.read_structure(GraphStructureRequest(datasource_id=DATASOURCE_ID))

    assert field_path in result.grounded_query.allowed_field_paths
    assert any(item.path == field_path for item in structure.fields)
