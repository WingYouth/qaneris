from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _neo4j_fake import FakeNeo4jDriver, graph_reader, graph_store

from smartdata.common.errors import GraphUnavailableError
from smartdata.contracts import DatasetInfo, Datasource, FieldInfo, RelationInfo
from smartdata.graph import GraphBindingReference, GraphStructureRequest
from smartdata.graph.neo4j import unavailable_driver_exceptions
from smartdata.scan import ScanGraphBuilder


def datasource(datasource_id: str = "sales", workspace_id: str = "default") -> Datasource:
    return Datasource(
        id=datasource_id,
        name=datasource_id.title(),
        kind="relational",
        workspace_id=workspace_id,
        driver="sqlite",
    )


def publish(
    driver: FakeNeo4jDriver, datasource_id: str = "sales", workspace_id: str = "default"
) -> None:
    datasets = [
        DatasetInfo(
            datasource_id=datasource_id,
            name="orders",
            fields=[
                FieldInfo(name="id", data_type="integer", primary_key=True),
                FieldInfo(name="customer_id", data_type="integer"),
                FieldInfo(name="pay_amount", data_type="decimal", comment="实收金额"),
                FieldInfo(name="region", data_type="text"),
            ],
        ),
        DatasetInfo(
            datasource_id=datasource_id,
            name="customers",
            fields=[
                FieldInfo(name="id", data_type="integer", primary_key=True),
                FieldInfo(name="name", data_type="text"),
            ],
        ),
    ]
    relations = [
        RelationInfo(
            datasource_id=datasource_id,
            from_dataset="orders",
            from_field="customer_id",
            to_dataset="customers",
            to_field="id",
            relation_type="foreign_key",
            source="scan",
        )
    ]
    graph = ScanGraphBuilder().build(
        datasource(datasource_id, workspace_id),
        datasets,
        relations,
        version=1,
        scanned_at=datetime.now(UTC),
    )
    store = graph_store(driver)
    store.ensure_schema()
    store.replace_datasource_graph(graph)


def test_published_scan_graph_is_readable_through_the_reader() -> None:
    driver = FakeNeo4jDriver()
    publish(driver)

    structure = graph_reader(driver).read_structure(GraphStructureRequest())

    assert [item.datasource_id for item in structure.datasources] == ["sales"]
    assert structure.datasources[0].driver == "sqlite"
    assert structure.datasources[0].scan_version == 1
    assert {item.name for item in structure.data_objects} == {"orders", "customers"}
    orders = structure.object_by_name("sales", "orders")
    assert orders is not None
    assert orders.object_kind == "table"
    assert orders.namespace is None
    fields = [item for item in structure.fields if item.object_id == orders.node_id]
    assert {item.path for item in fields} == {"id", "customer_id", "pay_amount", "region"}
    pay_amount = structure.field_by_path("sales", "orders", "pay_amount")
    assert pay_amount is not None
    assert pay_amount.data_type == "decimal"
    assert pay_amount.comment == "实收金额"


def test_reader_returns_relationships_only_when_the_graph_contains_them() -> None:
    driver = FakeNeo4jDriver()
    publish(driver)
    graph_valid = graph_reader(driver).read_structure(GraphStructureRequest())

    assert len(graph_valid.relationships) == 1
    relationship = graph_valid.relationships[0]
    assert relationship.relationship_type == "foreign_key"
    assert relationship.from_field_path == "customer_id"
    assert relationship.to_field_path == "id"
    assert relationship.source == "database"
    assert relationship.confirmed is True

    empty_driver = FakeNeo4jDriver()
    datasets = [
        DatasetInfo(
            datasource_id="sales",
            name="orders",
            fields=[FieldInfo(name="customer_id", data_type="integer")],
        ),
        DatasetInfo(
            datasource_id="sales",
            name="customers",
            fields=[FieldInfo(name="customer_id", data_type="integer")],
        ),
    ]
    graph = ScanGraphBuilder().build(
        datasource(), datasets, [], version=1, scanned_at=datetime.now(UTC)
    )
    graph_store(empty_driver).replace_datasource_graph(graph)

    no_relationship = graph_reader(empty_driver).read_structure(GraphStructureRequest())

    assert no_relationship.relationships == []
    assert {item.name for item in no_relationship.data_objects} == {"orders", "customers"}


def test_reader_scopes_to_one_datasource_and_reports_unknown_sources() -> None:
    driver = FakeNeo4jDriver()
    publish(driver, "sales")
    publish(driver, "crm")
    reader = graph_reader(driver)

    scoped = reader.read_structure(GraphStructureRequest(datasource_id="sales"))

    assert [item.datasource_id for item in scoped.datasources] == ["sales"]
    assert {item.datasource_id for item in scoped.data_objects} == {"sales"}
    assert {item.datasource_id for item in scoped.fields} == {"sales"}
    assert {item.from_datasource_id for item in scoped.relationships} == {"sales"}

    unknown = reader.read_structure(GraphStructureRequest(datasource_id="ds_missing"))

    assert unknown.datasources == []
    assert unknown.data_objects == []
    assert unknown.fields == []


def test_reader_resolves_exact_bindings_and_rejects_missing_ones() -> None:
    driver = FakeNeo4jDriver()
    publish(driver)

    structure = graph_reader(driver).read_structure(
        GraphStructureRequest(
            datasource_id="sales",
            references=[
                GraphBindingReference(
                    datasource_id="sales", data_object_name="orders", field_path="pay_amount"
                ),
                GraphBindingReference(
                    datasource_id="sales", data_object_name="orders", field_path="missing_amount"
                ),
                GraphBindingReference(datasource_id="sales", data_object_name="orders"),
            ],
        )
    )

    assert structure.field_by_path("sales", "orders", "pay_amount") is not None
    assert structure.field_by_path("sales", "orders", "missing_amount") is None
    assert len([item for item in structure.fields if item.path == "missing_amount"]) == 0


def test_reader_resolves_bindings_outside_the_bounded_context() -> None:
    driver = FakeNeo4jDriver()
    publish(driver)

    structure = graph_reader(driver).read_structure(
        GraphStructureRequest(
            datasource_id="sales",
            max_data_objects=1,
            references=[
                GraphBindingReference(
                    datasource_id="sales", data_object_name="orders", field_path="region"
                )
            ],
        )
    )

    assert structure.truncated is True
    assert len([item for item in structure.data_objects if item.name == "orders"]) == 1
    assert structure.field_by_path("sales", "orders", "region") is not None


def test_reader_never_projects_observed_business_values() -> None:
    driver = FakeNeo4jDriver()
    publish(driver)
    driver.inject_field_property("pay_amount", "sample_values", ["sensitive-value-must-not-leak"])
    driver.inject_field_property("pay_amount", "preview_rows", ["sensitive-value-must-not-leak"])

    structure = graph_reader(driver).read_structure(GraphStructureRequest())

    serialized = structure.model_dump_json()
    assert "sensitive-value-must-not-leak" not in serialized
    assert "sample_values" not in serialized
    assert "preview_rows" not in serialized
    pay_amount = structure.field_by_path("sales", "orders", "pay_amount")
    assert pay_amount is not None
    assert pay_amount.data_type == "decimal"


def test_repeated_reads_are_deterministic() -> None:
    driver = FakeNeo4jDriver()
    publish(driver)
    publish(driver, "crm")
    reader = graph_reader(driver)

    first = reader.read_structure(GraphStructureRequest())
    second = reader.read_structure(GraphStructureRequest())

    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_requested_datasource_is_constrained_in_the_database_query() -> None:
    driver = FakeNeo4jDriver()
    publish(driver, "sales")
    publish(driver, "crm")

    structure = graph_reader(driver).read_structure(
        GraphStructureRequest(datasource_id="sales")
    )

    assert driver.read_parameters("(d:Database)")["datasource_id"] == "sales"
    assert driver.read_parameters("ORDER BY datasource_id, name, node_id")["datasource_ids"] == [
        "sales"
    ]
    assert driver.read_parameters("ORDER BY from_object_id, relationship_id")[
        "datasource_ids"
    ] == ["sales"]

    sales_object_ids = {
        node["id"]
        for node in driver.nodes.values()
        if node.get("label") == "DataObject" and node.get("datasource_id") == "sales"
    }
    # The field window is requested for the selected object window only.
    assert set(driver.read_parameters("MATCH (o:DataObject)-[:HAS_FIELD]->")["object_ids"]) == (
        sales_object_ids
    )
    assert {item.node_id for item in structure.data_objects} == sales_object_ids


def test_reference_resolution_is_never_truncated_by_the_bounded_context() -> None:
    driver = FakeNeo4jDriver()
    publish(driver)
    reader = graph_reader(driver)

    structure = reader.read_structure(
        GraphStructureRequest(
            datasource_id="sales",
            max_data_objects=1,
            max_fields=1,
            max_relationships=1,
            references=[
                GraphBindingReference(
                    datasource_id="sales", qualified_name="orders", field_path="pay_amount"
                ),
                GraphBindingReference(
                    datasource_id="sales", data_object_name="customers", field_path="name"
                ),
            ],
        )
    )

    assert structure.truncated is True
    assert structure.field_by_path("sales", "orders", "pay_amount") is not None
    assert structure.field_by_path("sales", "customers", "name") is not None
    assert structure.object_by_name("sales", "customers") is not None


def test_reference_resolution_prefers_the_graph_identity_over_readable_names() -> None:
    driver = FakeNeo4jDriver()
    publish(driver)
    structure = graph_reader(driver).read_structure(GraphStructureRequest(datasource_id="sales"))
    customers = structure.object_by_name("sales", "customers")
    assert customers is not None

    stale_name = GraphBindingReference(
        datasource_id="sales",
        graph_data_object_id=customers.node_id,
        qualified_name="orders",
        data_object_name="orders",
    )
    referenced = structure.match_object(stale_name, "sales")

    # The physical identity decides; the conflicting readable name is ignored.
    assert [item.node_id for item in referenced] == [customers.node_id]
    assert referenced[0].name == "customers"


def publish_colliding_namespaces(driver: FakeNeo4jDriver) -> None:
    """One datasource holding public.orders and archive.orders, both with an ``amount`` field."""
    datasets = [
        DatasetInfo(
            datasource_id="sales",
            name="public.orders",
            fields=[FieldInfo(name="amount", data_type="decimal")],
        ),
        DatasetInfo(
            datasource_id="sales",
            name="archive.orders",
            fields=[FieldInfo(name="amount", data_type="decimal")],
        ),
    ]
    graph = ScanGraphBuilder().build(
        datasource("sales"), datasets, [], version=1, scanned_at=datetime.now(UTC)
    )
    graph_store(driver).replace_datasource_graph(graph)


def test_identical_object_names_in_different_namespaces_stay_distinct() -> None:
    driver = FakeNeo4jDriver()
    publish_colliding_namespaces(driver)
    reader = graph_reader(driver)

    structure = reader.read_structure(
        GraphStructureRequest(
            datasource_id="sales",
            references=[
                GraphBindingReference(
                    datasource_id="sales",
                    namespace="public",
                    qualified_name="public.orders",
                    data_object_name="orders",
                    field_path="amount",
                ),
                GraphBindingReference(
                    datasource_id="sales",
                    namespace="archive",
                    qualified_name="archive.orders",
                    data_object_name="orders",
                    field_path="amount",
                ),
            ],
        )
    )

    public_reference = GraphBindingReference(
        datasource_id="sales",
        namespace="public",
        qualified_name="public.orders",
        data_object_name="orders",
        field_path="amount",
    )
    archive_reference = GraphBindingReference(
        datasource_id="sales",
        namespace="archive",
        qualified_name="archive.orders",
        data_object_name="orders",
        field_path="amount",
    )

    public = structure.match_object(public_reference, "sales")
    archive = structure.match_object(archive_reference, "sales")
    ambiguous = structure.match_object(
        GraphBindingReference(datasource_id="sales", data_object_name="orders"), "sales"
    )

    assert len(public) == 1 and public[0].qualified_name == "public.orders"
    assert len(archive) == 1 and archive[0].qualified_name == "archive.orders"
    assert public[0].node_id != archive[0].node_id
    assert {item.node_id for item in ambiguous} == {public[0].node_id, archive[0].node_id}

    public_fields = structure.match_field(public_reference, public[0])
    archive_fields = structure.match_field(archive_reference, archive[0])

    assert [item.object_id for item in public_fields] == [public[0].node_id]
    assert [item.object_id for item in archive_fields] == [archive[0].node_id]
    assert public_fields[0].node_id != archive_fields[0].node_id


def test_reference_resolution_does_not_cross_workspaces() -> None:
    driver = FakeNeo4jDriver()
    publish(driver, "sales", "workspace_a")
    publish(driver, "crm", "default")
    reader = graph_reader(driver)

    foreign = reader.read_structure(GraphStructureRequest(workspace_id="default"))
    foreign_orders = foreign.object_by_name("crm", "orders")
    foreign_amount = foreign.field_by_path("crm", "orders", "pay_amount")
    assert foreign_orders is not None and foreign_amount is not None

    structure = reader.read_structure(
        GraphStructureRequest(
            workspace_id="workspace_a",
            references=[
                # A graph id owned by another workspace must not resolve.
                GraphBindingReference(
                    graph_data_object_id=foreign_orders.node_id,
                    graph_field_id=foreign_amount.node_id,
                ),
                # Neither may a readable datasource from another workspace.
                GraphBindingReference(datasource_id="crm", data_object_name="orders"),
                # The same lookup inside the workspace does resolve.
                GraphBindingReference(datasource_id="sales", data_object_name="orders"),
            ],
        )
    )

    # Nothing from the other workspace reaches the structure, by graph id or by name.
    assert {item.datasource_id for item in structure.data_objects} == {"sales"}
    assert {item.datasource_id for item in structure.fields} == {"sales"}
    assert structure.object_by_name("crm", "orders") is None
    assert structure.object_by_name("sales", "orders") is not None
    assert (
        structure.match_object(
            GraphBindingReference(graph_data_object_id=foreign_orders.node_id), None
        )
        == []
    )
    assert (
        structure.match_object(
            GraphBindingReference(datasource_id="crm", data_object_name="orders"), None
        )
        == []
    )
    assert driver.read_parameters("*1..2]->(o:DataObject)")["workspace_id"] == "workspace_a"


def test_requested_datasource_also_bounds_reference_resolution() -> None:
    driver = FakeNeo4jDriver()
    publish(driver, "sales", "workspace_a")
    publish(driver, "crm", "workspace_a")
    reader = graph_reader(driver)
    everything = reader.read_structure(GraphStructureRequest(workspace_id="workspace_a"))
    crm_orders = everything.object_by_name("crm", "orders")
    assert crm_orders is not None

    structure = reader.read_structure(
        GraphStructureRequest(
            workspace_id="workspace_a",
            datasource_id="sales",
            references=[
                GraphBindingReference(graph_data_object_id=crm_orders.node_id),
                GraphBindingReference(datasource_id="crm", data_object_name="orders"),
            ],
        )
    )

    assert structure.object_by_name("crm", "orders") is None
    assert structure.object_by_name("sales", "orders") is not None
    assert {item.datasource_id for item in structure.data_objects} == {"sales"}
    assert {item.datasource_id for item in structure.fields} == {"sales"}
    assert (
        structure.match_object(
            GraphBindingReference(graph_data_object_id=crm_orders.node_id), None
        )
        == []
    )
    assert driver.read_parameters("*1..2]->(o:DataObject)")["scoped_datasource_ids"] == ["sales"]


def test_field_window_follows_the_selected_object_window() -> None:
    driver = FakeNeo4jDriver()
    publish(driver)
    reader = graph_reader(driver)

    # "customers" sorts first, so the object window holds only that object.
    structure = reader.read_structure(
        GraphStructureRequest(datasource_id="sales", max_data_objects=1, max_fields=10)
    )

    assert structure.truncated is True
    assert [item.name for item in structure.data_objects] == ["customers"]
    selected = {item.node_id for item in structure.data_objects}
    assert {item.object_id for item in structure.fields} == selected
    assert {item.path for item in structure.fields} == {"id", "name"}
    assert set(driver.read_parameters("MATCH (o:DataObject)-[:HAS_FIELD]->")["object_ids"]) == selected


def test_configured_but_unreachable_backend_is_reported_as_unavailable() -> None:
    driver = FakeNeo4jDriver()
    publish(driver)
    reader = graph_reader(driver)
    kinds = unavailable_driver_exceptions()
    assert kinds, "the neo4j driver extra should be installed in the dev environment"
    driver.fail_read_with = kinds[0]("connection failed for bolt://neo4j:secret-password@host")

    with pytest.raises(GraphUnavailableError) as error:
        reader.read_structure(GraphStructureRequest(datasource_id="sales"))

    assert error.value.code == "graph_unavailable"
    assert error.value.status_code == 503
    assert "secret-password" not in error.value.message
    assert "bolt://" not in error.value.message


def test_readable_graph_without_matching_structure_is_not_an_error() -> None:
    driver = FakeNeo4jDriver()
    driver.state["nodes"]["db_empty"] = {
        "label": "Database",
        "id": "db_empty",
        "datasource_id": "sales",
        "workspace_id": "default",
        "name": "sales",
    }

    structure = graph_reader(driver).read_structure(GraphStructureRequest())

    assert structure.datasources
    assert structure.data_objects == []
    assert structure.fields == []
    assert structure.relationships == []


def driver_exception(name: str) -> type[BaseException]:
    exceptions = pytest.importorskip("neo4j.exceptions")
    error_type = getattr(exceptions, name, None)
    if error_type is None:
        pytest.skip(f"{name} is not part of this driver version")
    return error_type


ACCESS_FAILURES = (
    "ServiceUnavailable",
    "SessionExpired",
    "ConnectionPoolError",
    "ConnectionAcquisitionTimeoutError",
    "ConfigurationError",
    "AuthError",
    "DatabaseUnavailable",
)

USAGE_OR_QUERY_ERRORS = (
    "ResultError",
    "TransactionError",
    "BrokenRecordError",
    "CypherSyntaxError",
    "DriverError",
)


@pytest.mark.parametrize("name", ACCESS_FAILURES)
def test_access_failures_map_to_graph_unavailable(name: str) -> None:
    driver = FakeNeo4jDriver()
    publish(driver)
    driver.fail_read_with = driver_exception(name)(
        "connection failed for bolt://neo4j:secret-password@host"
    )

    with pytest.raises(GraphUnavailableError) as error:
        graph_reader(driver).read_structure(GraphStructureRequest(datasource_id="sales"))

    assert error.value.code == "graph_unavailable"
    assert error.value.status_code == 503
    assert name in error.value.message
    assert "secret-password" not in error.value.message
    assert "bolt://" not in error.value.message


@pytest.mark.parametrize("name", USAGE_OR_QUERY_ERRORS)
def test_usage_and_query_errors_are_not_masked_as_unavailable(name: str) -> None:
    error_type = driver_exception(name)
    driver = FakeNeo4jDriver()
    publish(driver)
    driver.fail_read_with = error_type("simulated failure")

    with pytest.raises(error_type):
        graph_reader(driver).read_structure(GraphStructureRequest(datasource_id="sales"))


def test_unavailable_exception_mapping_stays_narrow() -> None:
    mapped = {item.__name__ for item in unavailable_driver_exceptions()}

    assert mapped <= set(ACCESS_FAILURES)
    assert {"ServiceUnavailable", "SessionExpired", "AuthError", "DatabaseUnavailable"} <= mapped
    assert "DriverError" not in mapped
    assert not mapped & set(USAGE_OR_QUERY_ERRORS)
