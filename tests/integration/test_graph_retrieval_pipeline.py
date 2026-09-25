from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _neo4j_fake import FakeNeo4jDriver, graph_reader, graph_store

from qaneris.adapters import create_adapter
from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.common.errors import GraphUnavailableError
from qaneris.contracts import (
    CompanyDataProfile,
    DataObjectProfile,
    DatasetInfo,
    Datasource,
    DataSourceProfile,
    FieldInfo,
    FieldProfile,
    NamespaceProfile,
    SemanticAssetType,
)
from qaneris.contracts.semantic import BusinessQuery
from qaneris.graph import GraphStructureRequest, NullGraphReader
from qaneris.scan import ScanGraphBuilder, ScanService
from qaneris.semantic import (
    GraphSemanticRetriever,
    SemanticAsset,
    SemanticAssetBootstrap,
    SemanticAssetSeed,
    SQLiteSemanticAssetRegistry,
)

DATASOURCE_ID = "ds_sales"


def create_sqlite_database(path: Path, columns: tuple[str, ...]) -> None:
    field_sql = ", ".join(
        f"{column} {'REAL' if 'amount' in column else 'TEXT'}" for column in columns
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                {field_sql},
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            );
            INSERT INTO customers VALUES (1, 'Acme');
            INSERT INTO orders (id, customer_id, region) VALUES (1, 1, '华东');
            """
        )


def scan_into_graph(database: Path, driver: FakeNeo4jDriver, *, version: int) -> None:
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


def publish_sales_graph(path: Path, driver: FakeNeo4jDriver, *, version: int = 1) -> Path:
    database = path / f"sales_v{version}.db"
    create_sqlite_database(database, ("pay_amount", "region"))
    scan_into_graph(database, driver, version=version)
    return database


def semantic_asset(
    asset_id: str,
    kind: str,
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    data_object_name: str | None = None,
    field_path: str | None = None,
    target_asset_id: str | None = None,
    source: str = "administrator",
    status: str = "published",
) -> SemanticAsset:
    return SemanticAsset(
        asset_id=asset_id,
        workspace_id="default",
        kind=kind,
        name=name,
        aliases=list(aliases),
        source=source,
        source_ref=f"seed://retail/{asset_id}",
        status=status,
        confidence=1.0,
        datasource_id=DATASOURCE_ID if data_object_name else None,
        datasource_name="sales" if data_object_name else None,
        data_object_id=f"profile_{data_object_name}" if data_object_name else None,
        data_object_name=data_object_name,
        field_path=field_path,
        target_asset_id=target_asset_id,
        updated_at=datetime.now(UTC),
    )


def governed_assets() -> list[SemanticAsset]:
    """Assets are written straight to the registry: no CompanyDataProfile is involved anywhere."""
    return [
        semantic_asset(
            "metric_paid_sales",
            "metric",
            "实收销售额",
            aliases=("销售额", "实收金额"),
            data_object_name="orders",
            field_path="pay_amount",
        ),
        semantic_asset(
            "dimension_region",
            "dimension",
            "地区",
            aliases=("区域",),
            data_object_name="orders",
            field_path="region",
        ),
        semantic_asset(
            "alias_order_revenue",
            "alias",
            "订单收入",
            target_asset_id="metric_paid_sales",
            source="enterprise_definition",
            status="approved",
        ),
    ]


def registry_with(assets: list[SemanticAsset], path: Path) -> SQLiteSemanticAssetRegistry:
    registry = SQLiteSemanticAssetRegistry(path)
    for asset in assets:
        registry.save(asset)
    return registry


def sales_query(metrics: list[str] | None = None) -> BusinessQuery:
    return BusinessQuery(
        question="按地区查看销售额",
        objective="lookup",
        metrics=["销售额"] if metrics is None else metrics,
        dimensions=["地区"],
    )


def test_scanned_datasource_is_retrievable_from_the_published_graph(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    publish_sales_graph(tmp_path, driver)
    reader = graph_reader(driver)
    retriever = GraphSemanticRetriever(
        reader, registry_with(governed_assets(), tmp_path / "catalog.db")
    )

    result = retriever.retrieve(sales_query(), requested_datasource_id=DATASOURCE_ID)

    structure = reader.read_structure(GraphStructureRequest())
    orders = structure.object_by_name(DATASOURCE_ID, "orders")
    assert orders is not None
    metric = next(
        item for item in result.candidates if item.asset_type == SemanticAssetType.METRIC
    )
    assert metric.name == "实收销售额"
    assert metric.datasource_id == DATASOURCE_ID
    assert metric.data_object_id == orders.node_id
    assert metric.field_path == "pay_amount"
    dimension = next(
        item for item in result.candidates if item.asset_type == SemanticAssetType.DIMENSION
    )
    assert dimension.field_path == "region"
    assert dimension.data_object_id == orders.node_id
    assert result.retrieval_path == "trusted"
    assert all(item.trusted for item in result.candidates)
    assert not [item for item in result.warnings if "不存在" in item]


def test_scanned_foreign_key_is_the_only_relationship_source(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    publish_sales_graph(tmp_path, driver)
    retriever = GraphSemanticRetriever(
        graph_reader(driver), registry_with(governed_assets(), tmp_path / "catalog.db")
    )

    matched = retriever.retrieve(
        BusinessQuery(
            question="orders customers foreign_key customer_id",
            objective="lookup",
            entities=["orders", "customers", "foreign_key", "customer_id"],
        ),
        requested_datasource_id=DATASOURCE_ID,
        limit=100,
    )
    unrelated = retriever.retrieve(
        BusinessQuery(question="pay_amount", objective="lookup", metrics=["pay_amount"]),
        requested_datasource_id=DATASOURCE_ID,
        limit=100,
    )

    relationships = matched.candidates_for(SemanticAssetType.RELATIONSHIP)
    assert len(relationships) == 1
    relationship = relationships[0]
    assert relationship.relationship_id is not None
    assert "foreign_key" == relationship.name
    # The published edge is also offered structurally for joining, but never inferred: a query that
    # only mentions a column name can never produce a candidate for an edge that does not exist.
    assert {
        item.relationship_id for item in unrelated.candidates_for(SemanticAssetType.RELATIONSHIP)
    } <= {relationship.relationship_id}


def test_registry_binding_absent_from_published_graph_is_rejected(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    publish_sales_graph(tmp_path, driver)
    assets = [
        semantic_asset(
            "metric_phantom",
            "metric",
            "幽灵销售额",
            aliases=("幽灵",),
            data_object_name="orders",
            field_path="net_amount",
        )
    ]
    retriever = GraphSemanticRetriever(
        graph_reader(driver), registry_with(assets, tmp_path / "catalog.db")
    )

    result = retriever.retrieve(
        BusinessQuery(question="幽灵销售额", objective="lookup", metrics=["幽灵销售额"]),
        requested_datasource_id=DATASOURCE_ID,
        limit=100,
    )

    assert all(item.asset_id != "metric_phantom" for item in result.candidates)
    assert all(item.field_path != "net_amount" for item in result.candidates)
    assert any("metric_phantom" in warning for warning in result.warnings)


def test_retrieval_evidence_never_contains_observed_values(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    publish_sales_graph(tmp_path, driver)
    driver.inject_field_property("pay_amount", "sample_values", ["sensitive-value-must-not-leak"])
    driver.inject_field_property("region", "preview_rows", [{"region": "sensitive-value-must-not-leak"}])
    retriever = GraphSemanticRetriever(
        graph_reader(driver), registry_with(governed_assets(), tmp_path / "catalog.db")
    )

    result = retriever.retrieve(sales_query(), requested_datasource_id=DATASOURCE_ID, limit=100)

    serialized = result.model_dump_json()
    assert result.candidates
    assert "sensitive-value-must-not-leak" not in serialized
    assert "sample_values" not in serialized
    assert "preview_rows" not in serialized


def test_rescan_makes_the_graph_the_source_of_physical_truth(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    publish_sales_graph(tmp_path, driver, version=1)
    retriever = GraphSemanticRetriever(
        graph_reader(driver), registry_with(governed_assets(), tmp_path / "catalog.db")
    )
    before = retriever.retrieve(sales_query(), requested_datasource_id=DATASOURCE_ID)
    assert any(item.field_path == "pay_amount" for item in before.candidates)

    # The source drops pay_amount; a rescan must publish the narrower structure.
    recreated = tmp_path / "sales_v2.db"
    create_sqlite_database(recreated, ("gross_amount", "region"))
    scan_into_graph(recreated, driver, version=2)

    after = retriever.retrieve(sales_query(), requested_datasource_id=DATASOURCE_ID, limit=100)

    assert all(item.field_path != "pay_amount" for item in after.candidates)
    assert all(item.asset_id != "metric_paid_sales" for item in after.candidates)
    assert any("metric_paid_sales" in warning for warning in after.warnings)
    assert any(item.field_path == "region" for item in after.candidates)


def test_unknown_requested_datasource_returns_no_candidates(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    publish_sales_graph(tmp_path, driver)
    retriever = GraphSemanticRetriever(
        graph_reader(driver), registry_with(governed_assets(), tmp_path / "catalog.db")
    )

    result = retriever.retrieve(sales_query(), requested_datasource_id="ds_missing")

    assert result.candidates == []
    assert len(result.warnings) == 1
    assert "ds_missing" in result.warnings[0]


def test_service_retrieves_semantics_from_the_reader_it_is_given(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    publish_sales_graph(tmp_path, driver)
    catalog_path = tmp_path / "catalog.db"
    registry_with(governed_assets(), catalog_path)
    reader = graph_reader(driver)
    service = QanerisService(Catalog(catalog_path), graph_reader=reader)

    result = service.retrieve_semantics(
        sales_query(), requested_datasource_id=DATASOURCE_ID, limit=50
    )

    assert service.graph_reader is reader
    assert any(
        item.asset_type == SemanticAssetType.METRIC and item.field_path == "pay_amount"
        for item in result.candidates
    )
    assert any(
        item.asset_type == SemanticAssetType.DIMENSION and item.field_path == "region"
        for item in result.candidates
    )


def test_service_fails_closed_when_the_graph_backend_is_unavailable(tmp_path) -> None:
    """No configured graph backend must raise, not report "no candidates"."""
    catalog_path = tmp_path / "catalog.db"
    registry_with(governed_assets(), catalog_path)
    service = QanerisService(Catalog(catalog_path))

    assert isinstance(service.graph_reader, NullGraphReader)
    with pytest.raises(GraphUnavailableError) as error:
        service.retrieve_semantics(sales_query(), requested_datasource_id=DATASOURCE_ID)

    assert error.value.code == "graph_unavailable"
    assert "图后端未配置" in error.value.message


def publish_colliding_namespaces(
    driver: FakeNeo4jDriver, workspace_id: str = "default"
) -> None:
    """One scanned datasource holding both ``public.orders`` and ``archive.orders``."""
    datasets = [
        DatasetInfo(
            datasource_id=DATASOURCE_ID,
            name="public.orders",
            fields=[FieldInfo(name="amount", data_type="decimal")],
        ),
        DatasetInfo(
            datasource_id=DATASOURCE_ID,
            name="archive.orders",
            fields=[FieldInfo(name="amount", data_type="decimal")],
        ),
    ]
    graph = ScanGraphBuilder().build(
        Datasource(
            id=DATASOURCE_ID,
            name="sales",
            kind="relational",
            workspace_id=workspace_id,
            driver="sqlite",
        ),
        datasets,
        [],
        version=1,
        scanned_at=datetime.now(UTC),
    )
    graph_store(driver).replace_datasource_graph(graph)


def collision_profile(
    extra_fields: tuple[str, ...] = (), workspace_id: str = "default"
) -> CompanyDataProfile:
    def orders(data_object_id: str, namespace: str) -> DataObjectProfile:
        return DataObjectProfile(
            id=data_object_id,
            datasource_id=DATASOURCE_ID,
            namespace=namespace,
            name="orders",
            object_kind="table",
            fields=[
                FieldProfile(name=name, path=name, data_type="decimal")
                for name in ("amount", *extra_fields)
            ],
        )

    return CompanyDataProfile(
        workspace_id=workspace_id,
        data_sources=[
            DataSourceProfile(
                datasource_id=DATASOURCE_ID,
                name="sales",
                kind="relational",
                driver="sqlite",
                namespaces=[
                    NamespaceProfile(name="public", data_objects=[orders("profile_public", "public")]),
                    NamespaceProfile(
                        name="archive", data_objects=[orders("profile_archive", "archive")]
                    ),
                ],
            )
        ],
        generated_at=datetime.now(UTC),
    )


def amount_seed(
    asset_id: str,
    name: str,
    alias: str,
    namespace: str,
    workspace_id: str = "default",
) -> SemanticAssetSeed:
    return SemanticAssetSeed(
        asset_id=asset_id,
        workspace_id=workspace_id,
        kind="metric",
        name=name,
        aliases=[alias],
        source="administrator",
        source_ref=f"seed://retail/{asset_id}",
        status="published",
        datasource_name="sales",
        namespace=namespace,
        qualified_name=f"{namespace}.orders",
        data_object_name="orders",
        field_path="amount",
    )


def test_bootstrap_persists_graph_physical_identity_for_colliding_names(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    publish_colliding_namespaces(driver)
    reader = graph_reader(driver)
    registry = SQLiteSemanticAssetRegistry(tmp_path / "catalog.db")

    public, archive = SemanticAssetBootstrap(registry, graph_reader=reader).load(
        collision_profile(),
        [
            amount_seed("metric_public_amount", "对公销售额", "对公口径", "public"),
            amount_seed("metric_archive_amount", "归档销售额", "归档口径", "archive"),
        ],
    )

    assert public.has_graph_identity is True
    assert public.datasource_id == DATASOURCE_ID
    assert public.graph_data_object_id
    assert public.graph_field_id
    assert public.graph_data_object_id != archive.graph_data_object_id
    assert public.graph_field_id != archive.graph_field_id
    assert public.physical_identity == (DATASOURCE_ID, public.graph_data_object_id, "amount")
    # Readable names stay as display and migration metadata.
    assert (public.qualified_name, public.data_object_name) == ("public.orders", "orders")
    assert public.namespace == "public"

    reloaded = registry.get("default", "metric_public_amount")
    assert reloaded is not None
    assert reloaded.graph_data_object_id == public.graph_data_object_id
    assert reloaded.graph_field_id == public.graph_field_id

    result = GraphSemanticRetriever(reader, registry).retrieve(
        BusinessQuery(question="对公销售额", objective="lookup", metrics=["对公销售额"]),
        requested_datasource_id=DATASOURCE_ID,
        limit=100,
    )
    candidate = next(
        item for item in result.candidates if item.asset_id == "metric_public_amount"
    )
    assert candidate.data_object_id == public.graph_data_object_id
    assert candidate.field_id == public.graph_field_id
    assert candidate.migration_lookup is False
    archived = next(
        item for item in result.candidates if item.asset_id == "metric_archive_amount"
    )
    assert archived.data_object_id == archive.graph_data_object_id
    assert candidate.data_object_id != archived.data_object_id


def test_bootstrap_rejects_a_binding_the_graph_does_not_contain(tmp_path) -> None:
    """The profile may be optimistic; the published graph decides."""
    driver = FakeNeo4jDriver()
    publish_colliding_namespaces(driver)
    registry = SQLiteSemanticAssetRegistry(tmp_path / "catalog.db")
    bootstrap = SemanticAssetBootstrap(registry, graph_reader=graph_reader(driver))
    seed = amount_seed("metric_public_amount", "对公销售额", "对公口径", "public").model_copy(
        update={"field_path": "net_amount"}
    )

    with pytest.raises(ValueError, match="does not exist in the enterprise data graph"):
        bootstrap.load(collision_profile(extra_fields=("net_amount",)), [seed])

    assert registry.get("default", "metric_public_amount") is None


def test_bootstrap_refuses_an_ambiguous_readable_lookup(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    publish_colliding_namespaces(driver)
    registry = SQLiteSemanticAssetRegistry(tmp_path / "catalog.db")
    bootstrap = SemanticAssetBootstrap(registry, graph_reader=graph_reader(driver))
    seed = amount_seed("metric_ambiguous", "歧义销售额", "歧义口径", "public").model_copy(
        update={"namespace": None, "qualified_name": None}
    )

    with pytest.raises(ValueError, match="is not unique"):
        bootstrap.load(collision_profile(), [seed])


def test_bootstrap_resolves_graph_identity_in_a_non_default_workspace(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    publish_colliding_namespaces(driver, workspace_id="workspace_a")
    reader = graph_reader(driver)
    registry = SQLiteSemanticAssetRegistry(tmp_path / "catalog.db")

    public, archive = SemanticAssetBootstrap(registry, graph_reader=reader).load(
        collision_profile(workspace_id="workspace_a"),
        [
            amount_seed(
                "metric_public_amount", "对公销售额", "对公口径", "public", "workspace_a"
            ),
            amount_seed(
                "metric_archive_amount", "归档销售额", "归档口径", "archive", "workspace_a"
            ),
        ],
    )

    assert public.workspace_id == "workspace_a"
    assert public.has_graph_identity is True
    assert public.graph_data_object_id
    assert public.graph_field_id
    assert public.graph_data_object_id != archive.graph_data_object_id
    assert (
        registry.get("workspace_a", "metric_public_amount").graph_field_id
        == public.graph_field_id
    )

    # The bootstrap must read the seed's own workspace, never the default one.
    assert driver.read_parameters("(d:Database)")["workspace_id"] == "workspace_a"
    assert (
        driver.read_parameters("*1..2]->(o:DataObject)")["workspace_id"] == "workspace_a"
    )
    default_read = reader.read_structure(
        GraphStructureRequest(workspace_id="default")
    )
    assert default_read.datasources == []
    assert default_read.data_objects == []


def test_bootstrap_cannot_resolve_a_binding_from_another_workspace(tmp_path) -> None:
    driver = FakeNeo4jDriver()
    publish_colliding_namespaces(driver, workspace_id="default")
    registry = SQLiteSemanticAssetRegistry(tmp_path / "catalog.db")
    # The graph only holds the datasource under "default"; the seed claims "workspace_a".
    bootstrap = SemanticAssetBootstrap(registry, graph_reader=graph_reader(driver))
    seed = amount_seed(
        "metric_public_amount", "对公销售额", "对公口径", "public", "workspace_a"
    )

    with pytest.raises(ValueError, match="does not exist in the enterprise data graph"):
        bootstrap.load(collision_profile(workspace_id="workspace_a"), [seed])

    assert registry.get("workspace_a", "metric_public_amount") is None
