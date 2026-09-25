from __future__ import annotations

from typing import Protocol

from qaneris.common.errors import GraphUnavailableError
from qaneris.graph.reading import GraphStructure, GraphStructureRequest
from qaneris.scan.contracts import ScanGraph


class GraphStore(Protocol):
    """Atomic publication boundary for the current enterprise data graph."""

    def ensure_schema(self) -> None: ...

    def replace_datasource_graph(self, graph: ScanGraph) -> None: ...

    def delete_datasource_graph(self, datasource_id: str) -> None:
        """Remove exactly one datasource's published graph, and nothing else.

        Datasource deletion publishes the removal through the same boundary that published the
        graph, so a deleted datasource cannot leave a ghost behind in the graph. The traversal is
        ownership-based and scoped to the named datasource: a deletion must never be able to take
        another datasource's nodes with it.
        """
        ...


class GraphReader(Protocol):
    """Read side of the published enterprise data graph.

    Consumers such as semantic retrieval read what has already been published. They never reconnect
    to, or rescan, the enterprise database they are asking about.
    """

    def read_structure(self, request: GraphStructureRequest) -> GraphStructure: ...


class NullGraphStore:
    """Compatibility-only store; new deployments should configure Neo4jGraphStore."""

    def ensure_schema(self) -> None:
        return None

    def replace_datasource_graph(self, graph: ScanGraph) -> None:
        return None

    def delete_datasource_graph(self, datasource_id: str) -> None:
        return None


class NullGraphReader:
    """Compatibility-only reader used when no graph backend is configured.

    It fails closed. Returning an empty structure would make "no graph backend" indistinguishable
    from "graph read successfully with no match", so callers that need published structure get an
    explicit :class:`GraphUnavailableError` instead.
    """

    def read_structure(self, request: GraphStructureRequest) -> GraphStructure:
        raise GraphUnavailableError(
            "图后端未配置：语义检索需要已发布的 Neo4j 企业数据图，"
            "请配置 QANERIS_GRAPH_STORE=neo4j 后重试"
        )
