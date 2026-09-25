from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from smartdata.application.service import SmartDataService
from smartdata.cli import main as cli
from smartdata.cli import source as source_cli
from smartdata.contracts import Neo4jValidation
from smartdata.graph.ports import NullGraphReader
from smartdata.scanning import MultiDatabaseScanRunner


class RecordingGraphStore:
    def __init__(self):
        self.graphs = []

    def ensure_schema(self) -> None:
        return None

    def replace_datasource_graph(self, graph) -> None:
        self.graphs.append(graph)


class PassingGraphValidator:
    def validate(self, service, snapshot, datasets, relations):
        return Neo4jValidation(
            status="passed",
            checks={"fixture_graph_readback": True},
            expected={
                "datasource_id": snapshot.datasource_id,
                "scan_version": snapshot.version,
                "objects": len(datasets),
                "fields": sum(len(item.fields) for item in datasets),
                "relationships": len(relations),
            },
            actual={
                "datasource_id": snapshot.datasource_id,
                "scan_version": snapshot.version,
                "objects": len(datasets),
                "fields": sum(len(item.fields) for item in datasets),
                "relationships": len(relations),
            },
        )


def _source(path: Path, table: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT)')
        connection.execute(f'INSERT INTO "{table}" VALUES (1, "one")')


def _config(path: Path, first: Path, second: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "datasources": [
                    {
                        "name": "sales-a",
                        "kind": "relational",
                        "connection_profile": {
                            "driver": "sqlite",
                            "endpoint": {"path": str(first)},
                        },
                    },
                    {
                        "name": "sales-b",
                        "kind": "relational",
                        "connection_profile": {
                            "driver": "sqlite",
                            "endpoint": {"path": str(second)},
                        },
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def test_cli_scans_two_sources_into_distinct_snapshots_in_one_run(
    tmp_path, capsys, monkeypatch
) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    config = tmp_path / "datasources.json"
    _source(first, "orders")
    _source(second, "customers")
    _config(config, first, second)
    graph_store = RecordingGraphStore()

    def service_factory(catalog):
        return SmartDataService(
            catalog,
            model=object(),
            graph_store=graph_store,
            graph_reader=NullGraphReader(),
        )

    runner = MultiDatabaseScanRunner(
        artifact_base=tmp_path / "SmartDataArtifacts" / "scan-runs",
        service_factory=service_factory,
        graph_validator=PassingGraphValidator(),
    )
    monkeypatch.setattr(source_cli, "MultiDatabaseScanRunner", lambda: runner)
    monkeypatch.delenv("SMARTDATA_ENV_FILE", raising=False)
    monkeypatch.delenv("SMARTDATA_PROFILE_DIR", raising=False)

    exit_code = cli.main(["source", "scan", "--config", str(config), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert captured.err == ""
    assert exit_code == 0
    assert report["run"]["status"] == "passed"
    assert report["summary"]["configured_datasources"] == 2
    assert report["summary"]["scan_passed"] == 2
    assert len({item["datasource_id"] for item in report["datasources"]}) == 2
    assert len({item["snapshot_id"] for item in report["datasources"]}) == 2
    assert [item["scan_version"] for item in report["datasources"]] == [1, 1]
    assert len(graph_store.graphs) == 2
    assert len({graph.datasource_id for graph in graph_store.graphs}) == 2
    run_id = report["run"]["run_id"]
    assert all(run_id in path for path in report["artifact_paths"].values())
    assert Path(report["artifact_paths"]["manifest"]).is_file()
    assert Path(report["artifact_paths"]["scan_report_json"]).is_file()
    assert Path(report["artifact_paths"]["scan_report_markdown"]).is_file()
    profile_paths = [Path(item["document_path"]) for item in report["datasources"]]
    assert all(path.is_file() for path in profile_paths)
    assert len({path.parent for path in profile_paths}) == 2
