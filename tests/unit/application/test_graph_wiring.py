from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog


class FakeGraphStore:
    def ensure_schema(self) -> None:
        return None

    def replace_datasource_graph(self, graph) -> None:
        return None


def test_service_passes_explicit_graph_store_to_initializer(tmp_path) -> None:
    graph_store = FakeGraphStore()

    service = QanerisService(Catalog(tmp_path / "catalog.db"), graph_store=graph_store)

    assert service.graph_store is graph_store
    assert service.initializer.graph_store is graph_store
