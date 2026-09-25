from __future__ import annotations

from datetime import UTC, datetime

from qaneris.adapters.base import DataSourceAdapter
from qaneris.contracts.datasource import Datasource
from qaneris.graph.ports import GraphStore
from qaneris.scan.builder import ScanGraphBuilder
from qaneris.scan.contracts import ScanGraph
from qaneris.scan.validation import ScanGraphValidator


class ScanService:
    """Primary scan use case: source adapter -> validated graph -> graph store."""

    def __init__(
        self,
        graph_store: GraphStore,
        builder: ScanGraphBuilder | None = None,
        validator: ScanGraphValidator | None = None,
    ):
        self.graph_store = graph_store
        self.builder = builder or ScanGraphBuilder()
        self.validator = validator or ScanGraphValidator()

    def scan(
        self, datasource: Datasource, adapter: DataSourceAdapter, *, version: int
    ) -> ScanGraph:
        adapter.test_connection()
        datasets = adapter.scan_metadata()
        relations = adapter.scan_relations()
        graph = self.builder.build(
            datasource,
            datasets,
            relations,
            version=version,
            scanned_at=datetime.now(UTC),
        )
        self.validator.validate(graph)
        self.graph_store.ensure_schema()
        self.graph_store.replace_datasource_graph(graph)
        return graph
