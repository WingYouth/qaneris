import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from smartdata.cli import scan
from smartdata.contracts import (
    ConnectionTestReport,
    ConnectionTestResult,
    ScanRunInfo,
    ScanRunReport,
    ScanRunSummary,
)


def sqlite_config(source_path) -> dict:
    return {
        "name": "sales",
        "kind": "relational",
        "connection_profile": {
            "driver": "sqlite",
            "endpoint": {"path": str(source_path)},
        },
    }


def create_sqlite_source(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)")


def test_connection_test_command_delegates_to_scan_runner(tmp_path, capsys, monkeypatch) -> None:
    source = tmp_path / "source.db"
    create_sqlite_source(source)
    config = tmp_path / "datasource.json"
    config.write_text(json.dumps(sqlite_config(source)), encoding="utf-8")
    report = ConnectionTestReport(
        status="passed",
        configured_datasources=1,
        connection_passed=1,
        connection_failed=0,
        results=[
            ConnectionTestResult(
                name="sales", driver="sqlite", workspace_id="default", status="passed"
            )
        ],
    )
    test_connections = Mock(return_value=report)
    monkeypatch.setattr(
        scan,
        "MultiDatabaseScanRunner",
        lambda: SimpleNamespace(test_connections=test_connections),
    )

    exit_code = scan.main(["test", "--config", str(config)])

    assert exit_code == 0
    assert "[PASS] sales" in capsys.readouterr().out
    assert len(test_connections.call_args.args[0]) == 1


def test_run_command_maps_to_canonical_scan_runner(tmp_path, capsys, monkeypatch) -> None:
    source = tmp_path / "source.db"
    create_sqlite_source(source)
    config = tmp_path / "datasource.json"
    config.write_text(json.dumps(sqlite_config(source)), encoding="utf-8")
    catalog = tmp_path / "catalog.db"
    report = ScanRunReport(
        run=ScanRunInfo(
            run_id="run-one",
            workspace_id="default",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            status="passed",
            config_source=str(config),
        ),
        summary=ScanRunSummary(
            configured_datasources=1,
            connection_passed=1,
            scan_passed=1,
            scan_failed=0,
            total_data_objects=1,
            total_fields=2,
            total_indexes=0,
            total_constraints=1,
            total_relationships=0,
        ),
        artifact_paths={"scan_report_json": str(tmp_path / "scan_report.json")},
    )
    run = Mock(return_value=report)
    monkeypatch.setattr(
        scan,
        "MultiDatabaseScanRunner",
        lambda: SimpleNamespace(scan=run),
    )

    exit_code = scan.main(
        ["run", "--config", str(config), "--catalog", str(catalog), "--json"]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["run"]["run_id"] == "run-one"
    assert run.call_args.kwargs["catalog_path"] == catalog


def test_top_level_failure_redacts_secret(tmp_path, capsys, monkeypatch) -> None:
    source = tmp_path / "source.db"
    create_sqlite_source(source)
    config = tmp_path / "datasource.json"
    config.write_text(json.dumps(sqlite_config(source)), encoding="utf-8")
    monkeypatch.setenv("SMARTDATA_NEO4J_PASSWORD", "never-print-this")
    monkeypatch.setattr(
        scan,
        "MultiDatabaseScanRunner",
        lambda: (_ for _ in ()).throw(
            RuntimeError("authentication failed for never-print-this")
        ),
    )

    exit_code = scan.main(["run", "--config", str(config)])

    output = capsys.readouterr().err
    assert exit_code == 1
    assert "never-print-this" not in output
    assert "<redacted>" in output
