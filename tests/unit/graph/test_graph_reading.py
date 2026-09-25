from __future__ import annotations

import pytest
from pydantic import ValidationError

from qaneris.common.errors import GraphUnavailableError
from qaneris.graph import (
    GraphDataObject,
    GraphDatasource,
    GraphField,
    GraphRelationship,
    GraphStructure,
    GraphStructureRequest,
    NullGraphReader,
    graph_reader_from_environment,
)
from qaneris.graph.reading import GraphBindingReference


def datasource(datasource_id: str = "ds_sales") -> GraphDatasource:
    return GraphDatasource(
        node_id=f"db-{datasource_id}",
        datasource_id=datasource_id,
        workspace_id="default",
        name=datasource_id,
        kind="relational",
        driver="postgresql",
    )


def data_object(name: str, node_id: str, datasource_id: str = "ds_sales") -> GraphDataObject:
    return GraphDataObject(
        node_id=node_id,
        datasource_id=datasource_id,
        namespace="public",
        name=name,
        qualified_name=f"public.{name}",
        object_kind="table",
    )


def field(name: str, path: str, object_id: str) -> GraphField:
    return GraphField(
        node_id=f"{object_id}:{path}",
        object_id=object_id,
        datasource_id="ds_sales",
        object_name="orders",
        name=name,
        path=path,
        data_type="decimal",
    )


def test_structure_orders_assets_deterministically() -> None:
    structure = GraphStructure(
        datasources=[datasource("ds_b"), datasource("ds_a")],
        data_objects=[
            data_object("orders", "obj_b", "ds_b"),
            data_object("customers", "obj_a", "ds_a"),
        ],
        fields=[field("region", "region", "obj_a")],
    )

    first = structure.sorted()
    second = structure.sorted()

    assert [item.datasource_id for item in first.datasources] == ["ds_a", "ds_b"]
    assert [item.node_id for item in first.data_objects] == ["obj_a", "obj_b"]
    assert first == second


def test_structure_merges_reference_resolution_without_duplicates() -> None:
    bounded = GraphStructure(data_objects=[data_object("orders", "obj_orders")])
    referenced = GraphStructure(
        data_objects=[data_object("orders", "obj_orders"), data_object("customers", "obj_customers")],
        fields=[field("region", "region", "obj_orders")],
        truncated=False,
    )

    merged = bounded.merged(referenced)

    assert [item.node_id for item in merged.data_objects] == ["obj_customers", "obj_orders"]
    assert merged.fields[0].path == "region"
    assert not merged.truncated


def test_structure_looks_up_objects_by_name_or_qualified_name() -> None:
    structure = GraphStructure(data_objects=[data_object("orders", "obj_orders")])

    assert structure.object_by_name("ds_sales", "orders") is not None
    assert structure.object_by_name("ds_sales", "public.orders") is not None
    assert structure.object_by_name("ds_sales", "missing") is None
    assert structure.object_by_name("ds_other", "orders") is None
    assert structure.has_datasource("ds_sales") is False


def test_structure_resolves_field_by_object_name_and_path() -> None:
    structure = GraphStructure(
        data_objects=[data_object("orders", "obj_orders")],
        fields=[field("region", "region", "obj_orders")],
    )

    assert structure.field_by_path("ds_sales", "orders", "region") is not None
    assert structure.field_by_path("ds_sales", "orders", "missing") is None
    assert structure.field_by_path("ds_sales", "public.orders", "region") is not None


def test_read_model_rejects_observed_business_values() -> None:
    with pytest.raises(ValidationError):
        GraphField(
            node_id="f1",
            object_id="obj_orders",
            datasource_id="ds_sales",
            object_name="orders",
            name="pay_amount",
            path="pay_amount",
            sample_values=["sensitive-value-must-not-leak"],
        )


def test_structure_request_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        GraphStructureRequest(max_data_objects=0)
    with pytest.raises(ValidationError):
        GraphStructureRequest(max_fields=100_000)
    with pytest.raises(ValidationError):
        GraphBindingReference(datasource_id="ds_sales", data_object_name="")


def test_binding_reference_requires_an_identity_at_every_level() -> None:
    with pytest.raises(ValidationError, match="requires a datasource or a graph data object id"):
        GraphBindingReference(data_object_name="orders")
    with pytest.raises(ValidationError, match="requires a graph data object id or an object name"):
        GraphBindingReference(datasource_id="ds_sales")
    # A field-level identity still has to name the object that owns the field.
    with pytest.raises(ValidationError, match="requires a datasource or a graph data object id"):
        GraphBindingReference(graph_field_id="field_amount")

    readable = GraphBindingReference(
        datasource_name="sales", namespace="public", data_object_name="orders", field_path="amount"
    )
    exact = GraphBindingReference(
        graph_data_object_id="obj_public_orders", graph_field_id="field_amount"
    )

    assert readable.qualified_name is None
    assert exact.datasource_id is None
    assert exact.field_path is None


def test_null_graph_reader_fails_closed_instead_of_reporting_an_empty_graph() -> None:
    with pytest.raises(GraphUnavailableError, match="图后端未配置"):
        NullGraphReader().read_structure(
            GraphStructureRequest(datasource_id="ds_sales")
        )


def test_reader_factory_follows_the_graph_store_switch() -> None:
    reader = graph_reader_from_environment({"QANERIS_GRAPH_STORE": "null"})

    assert isinstance(reader, NullGraphReader)
    with pytest.raises(GraphUnavailableError):
        reader.read_structure(GraphStructureRequest())
    with pytest.raises(ValueError, match="Unsupported QANERIS_GRAPH_STORE"):
        graph_reader_from_environment({"QANERIS_GRAPH_STORE": "oracle"})


def test_relationship_read_model_has_no_inferred_semantics() -> None:
    relationship = GraphRelationship(
        relationship_id="edge_1",
        relationship_type="foreign_key",
        from_object_id="obj_orders",
        from_datasource_id="ds_sales",
        from_object_name="orders",
        from_field_path="customer_id",
        to_object_id="obj_customers",
        to_datasource_id="ds_sales",
        to_object_name="customers",
        to_field_path="id",
        source="database",
    )

    assert relationship.confirmed is True
    assert relationship.source == "database"


def colliding_structure() -> GraphStructure:
    """One datasource holding public.orders and archive.orders, both with an ``amount`` field."""
    return GraphStructure(
        datasources=[datasource("ds_sales")],
        data_objects=[
            GraphDataObject(
                node_id="obj_public_orders",
                datasource_id="ds_sales",
                namespace="public",
                name="orders",
                qualified_name="public.orders",
                object_kind="table",
            ),
            GraphDataObject(
                node_id="obj_archive_orders",
                datasource_id="ds_sales",
                namespace="archive",
                name="orders",
                qualified_name="archive.orders",
                object_kind="table",
            ),
        ],
        fields=[
            GraphField(
                node_id="field_public_amount",
                object_id="obj_public_orders",
                datasource_id="ds_sales",
                object_name="orders",
                name="amount",
                path="amount",
                data_type="decimal",
            ),
            GraphField(
                node_id="field_archive_amount",
                object_id="obj_archive_orders",
                datasource_id="ds_sales",
                object_name="orders",
                name="amount",
                path="amount",
                data_type="decimal",
            ),
        ],
    )


def test_namespace_lookup_separates_identical_object_names() -> None:
    structure = colliding_structure()

    public = structure.match_object(
        GraphBindingReference(
            datasource_id="ds_sales", namespace="public", data_object_name="orders"
        )
    )
    archive = structure.match_object(
        GraphBindingReference(
            datasource_id="ds_sales", qualified_name="archive.orders"
        )
    )

    assert [item.node_id for item in public] == ["obj_public_orders"]
    assert [item.node_id for item in archive] == ["obj_archive_orders"]


def test_ambiguous_name_lookup_returns_every_candidate_instead_of_picking_one() -> None:
    structure = colliding_structure()

    matches = structure.match_object(
        GraphBindingReference(datasource_id="ds_sales", data_object_name="orders")
    )

    assert [item.node_id for item in matches] == ["obj_public_orders", "obj_archive_orders"]


def test_exact_graph_id_lookup_never_falls_back_to_names() -> None:
    structure = colliding_structure()

    matched = structure.match_object(
        GraphBindingReference(
            graph_data_object_id="obj_archive_orders", data_object_name="intruder"
        )
    )
    missing = structure.match_object(
        GraphBindingReference(graph_data_object_id="obj_gone")
    )

    assert [item.node_id for item in matched] == ["obj_archive_orders"]
    assert missing == []


def test_field_resolution_binds_to_the_resolved_object_and_prefers_graph_field_id() -> None:
    structure = colliding_structure()
    public_orders = structure.match_object(
        GraphBindingReference(graph_data_object_id="obj_public_orders")
    )[0]

    by_path = structure.match_field(
        GraphBindingReference(
            datasource_id="ds_sales", namespace="public", data_object_name="orders", field_path="amount"
        ),
        public_orders,
    )
    by_field_id = structure.match_field(
        GraphBindingReference(
            graph_data_object_id="obj_public_orders", graph_field_id="field_public_amount"
        ),
        public_orders,
    )
    foreign_field_id = structure.match_field(
        GraphBindingReference(
            graph_data_object_id="obj_public_orders", graph_field_id="field_archive_amount"
        ),
        public_orders,
    )

    assert [item.node_id for item in by_path] == ["field_public_amount"]
    assert [item.node_id for item in by_field_id] == ["field_public_amount"]
    # A graph field id belonging to another object must not be accepted for this object.
    assert foreign_field_id == []


def test_datasource_match_by_name_is_unambiguous_only_when_unique() -> None:
    structure = GraphStructure(datasources=[datasource("ds_sales"), datasource("ds_crm")])

    assert [item.datasource_id for item in structure.match_datasource(datasource_name="ds_crm")] == [
        "ds_crm"
    ]
    assert structure.match_datasource(datasource_name="ds_missing") == []
