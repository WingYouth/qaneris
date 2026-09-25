from __future__ import annotations

import contextlib
import sqlite3
from copy import deepcopy

import pytest

from qaneris.adapters.base import DataSourceAdapter
from qaneris.catalog import Catalog
from qaneris.contracts import DatasetInfo, Datasource, DatasourceCreate, NormalizedResult
from qaneris.contracts.profile import ScanStatus
from qaneris.initialization import DatabaseInitializer
from qaneris.scan import ScanGraph, ScanService


class FakeGraphStore:
    def __init__(self):
        self.graph: ScanGraph | None = None
        self.fail = False

    def ensure_schema(self) -> None:
        return None

    def replace_datasource_graph(self, graph: ScanGraph) -> None:
        if self.fail:
            raise RuntimeError("graph publication failed")
        self.graph = deepcopy(graph)


class StubAdapter(DataSourceAdapter):
    def __init__(self, datasets: list[DatasetInfo], *, fail_scan: bool = False):
        super().__init__("sales", {})
        self.datasets = datasets
        self.fail_scan = fail_scan

    def test_connection(self) -> None:
        return None

    def scan_metadata(self) -> list[DatasetInfo]:
        if self.fail_scan:
            raise RuntimeError("adapter scan failed")
        return self.datasets

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        raise NotImplementedError


def _datasource() -> Datasource:
    return Datasource(
        id="sales", name="Sales", kind="relational", workspace_id="default", driver="sqlite"
    )


def test_adapter_scan_failure_preserves_published_graph() -> None:
    store = FakeGraphStore()
    service = ScanService(store)
    service.scan(
        _datasource(), StubAdapter([DatasetInfo(datasource_id="sales", name="orders")]), version=1
    )
    before = deepcopy(store.graph)

    with pytest.raises(RuntimeError, match="adapter scan failed"):
        service.scan(_datasource(), StubAdapter([], fail_scan=True), version=2)

    assert store.graph == before


def test_graph_validation_failure_preserves_published_graph() -> None:
    class FailingValidator:
        def validate(self, graph: ScanGraph) -> None:
            raise ValueError("graph validation failed")

    store = FakeGraphStore()
    valid = ScanService(store)
    valid.scan(
        _datasource(), StubAdapter([DatasetInfo(datasource_id="sales", name="orders")]), version=1
    )
    before = deepcopy(store.graph)
    failing = ScanService(store, validator=FailingValidator())

    with pytest.raises(ValueError, match="graph validation failed"):
        failing.scan(
            _datasource(), StubAdapter([DatasetInfo(datasource_id="sales", name="new")]), version=2
        )

    assert store.graph == before


def test_failed_graph_publication_does_not_activate_catalog_snapshot(tmp_path) -> None:
    source_path = tmp_path / "source.db"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
    catalog = Catalog(tmp_path / "catalog.db")
    datasource = catalog.create_datasource(
        DatasourceCreate(
            name="sales",
            kind="relational",
            connection={"driver": "sqlite", "path": str(source_path)},
        )
    )
    store = FakeGraphStore()
    initializer = DatabaseInitializer(catalog, graph_store=store, generate_profile_documents=False)
    first_job = initializer.initialize(datasource.id)
    first_snapshot = catalog.get_active_snapshot(datasource.id)
    assert first_job.status == ScanStatus.READY
    assert first_snapshot is not None

    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE products (id INTEGER PRIMARY KEY)")
    store.fail = True
    second_job = initializer.initialize(datasource.id)

    assert second_job.status == ScanStatus.PROFILE_FAILED
    assert catalog.get_active_snapshot(datasource.id).id == first_snapshot.id


def test_catalog_commit_failure_never_marks_job_ready(tmp_path, monkeypatch) -> None:
    source_path = tmp_path / "source.db"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
    catalog = Catalog(tmp_path / "catalog.db")
    datasource = catalog.create_datasource(
        DatasourceCreate(
            name="sales",
            kind="relational",
            connection={"driver": "sqlite", "path": str(source_path)},
        )
    )
    store = FakeGraphStore()
    initializer = DatabaseInitializer(catalog, graph_store=store, generate_profile_documents=False)

    def fail_commit(*args, **kwargs):
        raise sqlite3.OperationalError("catalog commit failed")

    monkeypatch.setattr(catalog, "commit_scan_snapshot", fail_commit)
    job = initializer.initialize(datasource.id)

    assert store.graph is not None
    assert job.status == ScanStatus.PROFILE_FAILED
    assert job.snapshot_id is None
    assert catalog.get_active_snapshot(datasource.id) is None


def test_initialization_job_redacts_materialized_secret_errors(tmp_path, monkeypatch) -> None:
    secret = "materialized-database-secret"
    catalog = Catalog(tmp_path / "catalog.db")
    datasource = catalog.create_datasource(
        DatasourceCreate(
            name="sales",
            kind="relational",
            connection={"driver": "sqlite", "path": str(tmp_path / "source.db")},
        )
    )

    class Provider:
        # The runtime boundary is context-managed, so a stub provider mirrors ``open()``; the
        # connection it yields is the same materialized dict the redaction assertion is about.
        def open(self, datasource_id):
            return contextlib.nullcontext({"driver": "sqlite", "password": secret})

    class FailingAdapter(StubAdapter):
        def __init__(self):
            super().__init__([])

        def scan_metadata(self):
            raise RuntimeError(f"scan failed with {secret}")

    monkeypatch.setattr(
        "qaneris.initialization.service.create_adapter",
        lambda datasource_id, kind, connection: FailingAdapter(),
    )
    initializer = DatabaseInitializer(
        catalog,
        connection_provider=Provider(),
        graph_store=FakeGraphStore(),
        generate_profile_documents=False,
    )

    job = initializer.initialize(datasource.id)

    assert job.status == ScanStatus.SCAN_FAILED
    assert secret not in (job.error_message or "")
    assert "<redacted>" in (job.error_message or "")
