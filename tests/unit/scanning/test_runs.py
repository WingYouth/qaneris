from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from qaneris.catalog import Catalog
from qaneris.contracts import (
    ConnectionEndpoint,
    ConnectionProfile,
    DatasetInfo,
    DatasourceKind,
    DataSourceProfile,
    FieldInfo,
    NamespaceProfile,
    ScanSnapshot,
    ScanStatus,
    SecureDatasourceCreate,
)
from qaneris.graph.reading import GraphDataObject, GraphDatasource, GraphField, GraphStructure
from qaneris.scanning import MultiDatabaseScanRunner, SnapshotGraphValidator


def request(name, path, *, workspace_id="default") -> SecureDatasourceCreate:
    return SecureDatasourceCreate(
        name=name,
        kind="relational",
        workspace_id=workspace_id,
        connection_profile=ConnectionProfile(
            driver="sqlite", endpoint=ConnectionEndpoint(path=str(path))
        ),
    )


def sqlite_source(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL)")


def test_connection_batch_continues_after_failure_and_redacts(tmp_path, monkeypatch) -> None:
    valid = tmp_path / "valid.db"
    sqlite_source(valid)
    secret = "connection-secret"
    monkeypatch.setenv("BROKEN_DB_PASSWORD", secret)
    missing = tmp_path / secret / "missing.db"

    report = MultiDatabaseScanRunner().test_connections(
        [request("valid", valid), request("broken", missing)]
    )

    assert report.status == "failed"
    assert report.connection_passed == 1
    assert report.connection_failed == 1
    assert [item.status for item in report.results] == ["passed", "failed"]
    assert secret not in (report.results[1].error or "")
    assert "<redacted>" in (report.results[1].error or "")


def test_scan_run_ids_and_manifests_are_unique(tmp_path) -> None:
    source = tmp_path / "source.db"
    sqlite_source(source)
    runner = MultiDatabaseScanRunner(
        artifact_base=tmp_path / "scan-runs",
        service_factory=lambda catalog: (_ for _ in ()).throw(RuntimeError("graph offline")),
    )

    first = runner.scan([request("sales", source)], config_source="test-config")
    second = runner.scan([request("sales", source)], config_source="test-config")

    assert first.run.run_id != second.run.run_id
    assert first.datasources[0].snapshot_id is None
    for report in (first, second):
        manifest = json.loads(
            Path(report.artifact_paths["manifest"]).read_text(encoding="utf-8")
        )
        assert manifest["run_id"] == report.run.run_id
        assert manifest["datasources"][0]["snapshot_id"] is None


def test_one_scan_run_cannot_merge_workspaces(tmp_path) -> None:
    source = tmp_path / "source.db"
    sqlite_source(source)

    try:
        MultiDatabaseScanRunner().test_connections(
            [request("one", source, workspace_id="a"), request("two", source, workspace_id="b")]
        )
    except ValueError as error:
        assert "cannot span multiple workspaces" in str(error)
    else:  # pragma: no cover - explicit contract guard
        raise AssertionError("mixed workspaces must be rejected")


def test_graph_validator_matches_active_snapshot_counts(tmp_path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    datasource = catalog.create_secure_datasource(request("sales", tmp_path / "source.db"))
    profile = DataSourceProfile(
        datasource_id=datasource.id,
        name=datasource.name,
        kind=DatasourceKind.RELATIONAL,
        driver="sqlite",
        namespaces=[NamespaceProfile(name="default")],
    )
    snapshot = ScanSnapshot(
        id="snap-one",
        datasource_id=datasource.id,
        version=3,
        status=ScanStatus.READY,
        profile=profile,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        active=True,
    )
    datasets = [
        DatasetInfo(
            datasource_id=datasource.id,
            name="orders",
            kind="table",
            fields=[FieldInfo(name="id", data_type="INTEGER")],
        )
    ]
    structure = GraphStructure(
        datasources=[
            GraphDatasource(
                node_id="db-one",
                datasource_id=datasource.id,
                workspace_id="default",
                name="sales",
                scan_version=3,
            )
        ],
        data_objects=[
            GraphDataObject(
                node_id="obj-one",
                datasource_id=datasource.id,
                name="orders",
            )
        ],
        fields=[
            GraphField(
                node_id="field-one",
                object_id="obj-one",
                datasource_id=datasource.id,
                object_name="orders",
                name="id",
                path="id",
            )
        ],
    )
    service = SimpleNamespace(
        catalog=catalog,
        graph_reader=SimpleNamespace(read_structure=lambda request: structure),
    )

    validation = SnapshotGraphValidator().validate(service, snapshot, datasets, [])

    assert validation.status == "passed"
    assert all(validation.checks.values())


def test_graph_version_mismatch_is_not_a_pass(tmp_path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    datasource = catalog.create_secure_datasource(request("sales", tmp_path / "source.db"))
    snapshot = ScanSnapshot(
        id="snap-one",
        datasource_id=datasource.id,
        version=2,
        status="ready",
        profile=DataSourceProfile(
            datasource_id=datasource.id,
            name="sales",
            kind="relational",
            driver="sqlite",
        ),
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        active=True,
    )
    structure = GraphStructure(
        datasources=[
            GraphDatasource(
                node_id="db-one",
                datasource_id=datasource.id,
                workspace_id="default",
                name="sales",
                scan_version=1,
            )
        ]
    )
    service = SimpleNamespace(
        catalog=catalog,
        graph_reader=SimpleNamespace(read_structure=lambda request: structure),
    )

    validation = SnapshotGraphValidator().validate(service, snapshot, [], [])

    assert validation.status == "failed"
    assert validation.checks["scan_version_matches"] is False
    assert validation.checks["active_snapshot_matches"] is False
