from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from smartdata.scan import (
    ScanEdgeType,
    ScanGraph,
    ScanGraphEdge,
    ScanGraphNode,
    ScanGraphValidator,
    ScanNodeKind,
)


def _node(identifier: str) -> ScanGraphNode:
    return ScanGraphNode(id=identifier, kind=ScanNodeKind.DATA_OBJECT)


def _graph(nodes: list[ScanGraphNode], edges: list[ScanGraphEdge]) -> ScanGraph:
    return ScanGraph(
        workspace_id="w",
        datasource_id="d",
        scan_version=1,
        scanned_at=datetime.now(UTC),
        nodes=nodes,
        edges=edges,
    )


def _relationship(source: str = "database", confirmed: bool = True) -> ScanGraphEdge:
    return ScanGraphEdge(
        id="rel",
        type=ScanEdgeType.RELATES_TO,
        from_node_id="a",
        to_node_id="b",
        properties={"source": source, "confirmed": confirmed},
    )


def test_duplicate_node_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate node ids"):
        _graph([_node("same"), _node("same")], [])


def test_duplicate_edge_id_is_rejected() -> None:
    edge = ScanGraphEdge(id="same", type=ScanEdgeType.CONTAINS, from_node_id="a", to_node_id="b")
    with pytest.raises(ValidationError, match="duplicate edge ids"):
        _graph([_node("a"), _node("b")], [edge, edge])


def test_dangling_edge_is_rejected() -> None:
    edge = ScanGraphEdge(
        id="dangling", type=ScanEdgeType.CONTAINS, from_node_id="a", to_node_id="missing"
    )
    with pytest.raises(ValidationError, match="dangling edges"):
        _graph([_node("a")], [edge])


@pytest.mark.parametrize("source", ["database", "configuration"])
def test_confirmed_relationship_with_trusted_source_is_allowed(source: str) -> None:
    graph = _graph([_node("a"), _node("b")], [_relationship(source=source)])

    ScanGraphValidator().validate(graph)


def test_unconfirmed_relationship_is_rejected() -> None:
    graph = _graph([_node("a"), _node("b")], [_relationship(confirmed=False)])

    with pytest.raises(ValueError, match="must be confirmed"):
        ScanGraphValidator().validate(graph)


def test_relationship_with_unknown_source_is_rejected() -> None:
    graph = _graph([_node("a"), _node("b")], [_relationship(source="model")])

    with pytest.raises(ValueError, match="trusted source"):
        ScanGraphValidator().validate(graph)
