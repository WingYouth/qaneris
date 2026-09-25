from datetime import UTC, datetime

import pytest

from smartdata.contracts import (
    ConstraintInfo,
    DatasetInfo,
    Datasource,
    FieldInfo,
    IndexInfo,
    RelationInfo,
)
from smartdata.scan import ScanEdgeType, ScanGraphBuilder, ScanGraphValidator, ScanNodeKind


def _datasource() -> Datasource:
    return Datasource(
        id="sales", name="Sales", kind="relational", workspace_id="acme", driver="sqlite"
    )


def test_builder_covers_structure_and_only_confirmed_database_relationships() -> None:
    datasets = [
        DatasetInfo(
            datasource_id="sales",
            name="main.orders",
            namespace="main",
            fields=[FieldInfo(name="customer_id", data_type="INTEGER", indexed=True)],
            indexes=[IndexInfo(name="idx_customer", fields=["customer_id"])],
            constraints=[
                ConstraintInfo(
                    name="fk_customer", constraint_type="foreign_key", fields=["customer_id"]
                )
            ],
        ),
        DatasetInfo(
            datasource_id="sales",
            name="main.customers",
            namespace="main",
            fields=[FieldInfo(name="id", data_type="INTEGER", primary_key=True)],
        ),
    ]
    relations = [
        RelationInfo(
            datasource_id="sales",
            from_dataset="main.orders",
            from_field="customer_id",
            to_dataset="main.customers",
            to_field="id",
            relation_type="foreign_key",
            source="scan",
        )
    ]

    graph = ScanGraphBuilder().build(
        _datasource(), datasets, relations, version=2, scanned_at=datetime.now(UTC)
    )
    ScanGraphValidator().validate(graph)

    assert {node.kind for node in graph.nodes} == {
        ScanNodeKind.DATABASE,
        ScanNodeKind.NAMESPACE,
        ScanNodeKind.DATA_OBJECT,
        ScanNodeKind.FIELD,
        ScanNodeKind.INDEX,
        ScanNodeKind.CONSTRAINT,
    }
    relationship = next(edge for edge in graph.edges if edge.type == ScanEdgeType.RELATES_TO)
    assert relationship.properties["source"] == "database"
    assert relationship.properties["confirmed"] is True


def test_builder_rejects_inferred_relationship_source() -> None:
    datasets = [DatasetInfo(datasource_id="sales", name=name) for name in ("a", "b")]
    relation = RelationInfo(
        datasource_id="sales",
        from_dataset="a",
        to_dataset="b",
        relation_type="similar_name",
        source="model",
    )

    with pytest.raises(ValueError, match="untrusted relationship source"):
        ScanGraphBuilder().build(
            _datasource(), datasets, [relation], version=1, scanned_at=datetime.now(UTC)
        )


def test_builder_rejects_relationship_referencing_unknown_object() -> None:
    datasets = [DatasetInfo(datasource_id="sales", name="orders")]
    relation = RelationInfo(
        datasource_id="sales",
        from_dataset="orders",
        to_dataset="missing",
        relation_type="foreign_key",
        source="scan",
    )

    with pytest.raises(ValueError, match="unknown object"):
        ScanGraphBuilder().build(
            _datasource(), datasets, [relation], version=1, scanned_at=datetime.now(UTC)
        )
