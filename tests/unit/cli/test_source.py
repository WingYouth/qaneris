from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from qaneris.cli import main as cli
from qaneris.contracts import (
    ConnectionTestReport,
    ConnectionTestResult,
    ScanRunInfo,
    ScanRunMember,
    ScanRunReport,
    ScanRunSummary,
)


@pytest.mark.parametrize(
    "arguments",
    [
        ["source", "--help"],
        ["source", "test", "--help"],
        ["source", "scan", "--help"],
    ],
)
def test_source_help(arguments, capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(arguments)

    assert raised.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_connection_json_stdout_is_pure_and_mixed_result_exits_one(
    tmp_path, capsys, monkeypatch
) -> None:
    report = ConnectionTestReport(
        status="failed",
        configured_datasources=2,
        connection_passed=1,
        connection_failed=1,
        results=[
            ConnectionTestResult(
                name="one", driver="sqlite", workspace_id="default", status="passed"
            ),
            ConnectionTestResult(
                name="two",
                driver="sqlite",
                workspace_id="default",
                status="failed",
                error="not available",
            ),
        ],
    )

    def noisy_run(args):
        print("driver warning")
        return report

    monkeypatch.setattr(cli, "run_source", noisy_run)

    exit_code = cli.main(
        ["source", "test", "--config", str(tmp_path / "config.json"), "--json"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out)["connection_failed"] == 1
    assert captured.err == "driver warning\n"


def test_failed_scan_report_exits_one(tmp_path, capsys, monkeypatch) -> None:
    report = ScanRunReport(
        run=ScanRunInfo(
            run_id="run-one",
            workspace_id="default",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            status="failed",
            config_source="config.json",
        ),
        summary=ScanRunSummary(
            configured_datasources=1,
            connection_passed=1,
            scan_passed=0,
            scan_failed=1,
            total_data_objects=1,
            total_fields=2,
            total_indexes=0,
            total_constraints=0,
            total_relationships=0,
        ),
        datasources=[
            ScanRunMember(
                name="sales",
                driver="sqlite",
                kind="relational",
                workspace_id="default",
                status="failed",
                connection_status="passed",
                scan_status="passed",
                neo4j_status="failed",
                error="Neo4j validation failed",
            )
        ],
        artifact_paths={"scan_report_json": str(tmp_path / "report.json")},
    )
    monkeypatch.setattr(cli, "run_source", lambda args: report)

    assert cli.main(["source", "scan", "--config", "config.json"]) == 1
    assert "[FAIL] sales" in capsys.readouterr().out


def test_invalid_config_is_exit_two_and_structured_json(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "invalid.json"
    config.write_text("not-json", encoding="utf-8")

    exit_code = cli.main(["source", "test", "--config", str(config), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "configuration_error"
