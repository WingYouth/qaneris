"""CLI contract tests for ``qaneris source import-excel`` (EXCEL-01B).

These lock the interface contract only: argument wiring, the single Application Service call, the
human and JSON output shapes, the stable Excel error codes and the exit-code boundary. Ingestion
itself - container validation, the type lattice, materialization, policy identity and graph
publication - is already covered by the EXCEL-01A suites and is deliberately not retested here.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest

from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.cli import main as cli
from qaneris.cli import source
from qaneris.ingestion.excel import (
    ExcelColumnSummary,
    ExcelErrorCode,
    ExcelImportRequest,
    ExcelImportResult,
    ExcelIngestionError,
    ExcelSheetSummary,
)

FILE_SHA256 = "1cca1fe203d883bc90b0a414b1c8d73b31dcda85542ade84c0a0b1dbc8b5b1de"
IMPORT_ID = "excel_37a8eec1ce19_f163987ea981_c0eb892c9cea5fd6"
DATASOURCE_ID = "ds_ff606cfffe96"
SNAPSHOT_ID = "snap_814d05c438a6"

# Tokens that must never appear in a CLI error: a formula body or a stored business value.
BUSINESS_TOKENS = ("=SUM(", "华东", "completed")


def imported(tmp_path: Path, **changes: Any) -> ExcelImportResult:
    payload: dict[str, Any] = {
        "import_id": IMPORT_ID,
        "workspace_id": "default",
        "datasource_id": DATASOURCE_ID,
        "snapshot_id": SNAPSHOT_ID,
        "scan_version": 1,
        "status": "READY",
        "original_filename": "orders.xlsx",
        "file_sha256": FILE_SHA256,
        "sqlite_path": str(tmp_path / "data.sqlite3"),
        "artifact_directory": str(tmp_path / "artifacts" / FILE_SHA256),
        "manifest_path": str(tmp_path / "artifacts" / FILE_SHA256 / "manifest.json"),
        "policy_version": "excel-ingestion-v1",
        "neo4j_publication_verified": True,
        "sheets": [
            ExcelSheetSummary(
                original_name="orders",
                table_name="orders",
                row_count=10,
                column_count=5,
                columns=[
                    ExcelColumnSummary(
                        source_header="order_id",
                        column_name="order_id",
                        inferred_type="INTEGER",
                        nullable=False,
                        all_null=False,
                    )
                ],
            )
        ],
        "warnings": [],
    }
    payload.update(changes)
    return ExcelImportResult(**payload)


def installed(monkeypatch: pytest.MonkeyPatch, service: Any) -> Mock:
    """Install the service the CLI will build and return the constructor spy."""
    constructor = Mock(return_value=service)
    monkeypatch.setattr(source, "QanerisService", constructor)
    return constructor


def serving(monkeypatch: pytest.MonkeyPatch, result: ExcelImportResult) -> Mock:
    import_excel = Mock(return_value=result)
    installed(monkeypatch, Mock(spec=QanerisService, import_excel=import_excel))
    return import_excel


def refusing(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    import_excel = Mock(side_effect=error)
    installed(monkeypatch, Mock(spec=QanerisService, import_excel=import_excel))


def command(*extra: str) -> list[str]:
    return ["source", "import-excel", "orders.xlsx", *extra]


# --------------------------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------------------------


def test_source_help_lists_the_import_command(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["source", "--help"])

    assert raised.value.code == 0
    assert "import-excel" in capsys.readouterr().out


def test_import_excel_help_documents_the_options(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["source", "import-excel", "--help"])

    assert raised.value.code == 0
    usage = capsys.readouterr().out
    assert "FILE" in usage
    for option in ("--name", "--workspace", "--json"):
        assert option in usage


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ([], None),
        (["--name", "Sales Workbook"], "Sales Workbook"),
        (["--name", "  Sales Workbook  "], "Sales Workbook"),
    ],
)
def test_name_reaches_the_request(arguments, expected, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", str(tmp_path / "catalog.db"))
    import_excel = serving(monkeypatch, imported(tmp_path))

    assert cli.main(command(*arguments)) == 0
    request = import_excel.call_args.args[0]
    assert request.datasource_name == expected
    assert request.file_path == "orders.xlsx"
    assert request.workspace_id == "default"


def test_workspace_reaches_the_request(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", str(tmp_path / "catalog.db"))
    import_excel = serving(monkeypatch, imported(tmp_path, workspace_id="roadshow"))

    assert cli.main(command("--workspace", "roadshow")) == 0
    request: ExcelImportRequest = import_excel.call_args.args[0]
    assert request.workspace_id == "roadshow"


def test_cli_calls_only_import_excel_on_a_normally_configured_service(
    tmp_path, monkeypatch
) -> None:
    """The CLI orchestrates the Application Service; it never picks a second ingestion path.

    The constructor assertion is the important one: no fake graph, no ``NullGraphReader`` and no
    private pipeline may be injected, and the catalog must follow the same ``QANERIS_CATALOG``
    convention the API, the MCP server and ``doctor`` already use.
    """
    catalog_path = tmp_path / "catalog.db"
    monkeypatch.setenv("QANERIS_CATALOG", str(catalog_path))
    service = Mock(spec=QanerisService, import_excel=Mock(return_value=imported(tmp_path)))
    constructor = installed(monkeypatch, service)

    assert cli.main(command("--name", "Sales Workbook", "--workspace", "default", "--json")) == 0

    args, kwargs = constructor.call_args
    assert kwargs == {}
    assert len(args) == 1 and isinstance(args[0], Catalog)
    assert args[0].path == str(catalog_path)
    assert service.mock_calls == [
        call.import_excel(
            ExcelImportRequest(
                file_path="orders.xlsx",
                datasource_name="Sales Workbook",
                workspace_id="default",
            )
        )
    ]


FORBIDDEN_IN_CLI = (
    "ExcelIngestionService",
    "SQLiteMaterializer",
    "DatabaseInitializer",
    "validate_workbook",
    "iter_table_rows",
    "Neo4jGraphStore",
    "NullGraphReader",
)

#: ``source create`` (RS-CLI-02) legitimately calls this, so the token exists in the module. It
#: must still never appear on the import path: importing a workbook is not creating a datasource.
FORBIDDEN_IN_IMPORT = ("create_secure_datasource", "secure_datasource")


def test_cli_module_does_not_reach_past_the_application_contract() -> None:
    text = inspect.getsource(source)
    assert [token for token in FORBIDDEN_IN_CLI if token in text] == []


def test_import_excel_does_not_reach_past_the_application_contract() -> None:
    text = inspect.getsource(source.run_excel_import)
    assert [token for token in FORBIDDEN_IN_IMPORT if token in text] == []


# --------------------------------------------------------------------------------------------
# success output
# --------------------------------------------------------------------------------------------


def test_human_output_reports_identity_and_artifact(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", str(tmp_path / "catalog.db"))
    serving(monkeypatch, imported(tmp_path, warnings=["工作表 'hidden' 已跳过，原因：hidden"]))

    exit_code = cli.main(command("--name", "Roadshow Excel Orders"))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "[PASS] Excel import ready" in captured.out
    assert "Name: Roadshow Excel Orders" in captured.out
    assert f"Import ID: {IMPORT_ID}" in captured.out
    assert f"Datasource: {DATASOURCE_ID}" in captured.out
    assert f"Snapshot: {SNAPSHOT_ID}" in captured.out
    assert "Scan version: 1" in captured.out
    assert "Sheets: 1" in captured.out
    assert f"Artifact: {tmp_path}/artifacts/{FILE_SHA256}" in captured.out
    assert "Warnings:\n  - 工作表 'hidden' 已跳过，原因：hidden" in captured.out


def test_human_output_omits_the_optional_name_when_not_given(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", str(tmp_path / "catalog.db"))
    serving(monkeypatch, imported(tmp_path))

    assert cli.main(command()) == 0
    assert "Name:" not in capsys.readouterr().out


def test_human_output_hides_internal_file_locations(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", str(tmp_path / "catalog.db"))
    serving(monkeypatch, imported(tmp_path))

    assert cli.main(command()) == 0

    printed = capsys.readouterr().out
    assert str(tmp_path / "data.sqlite3") not in printed
    assert "manifest.json" not in printed


def test_json_output_is_pure_and_stable(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", str(tmp_path / "catalog.db"))
    result = imported(tmp_path, warnings=["工作表 'hidden' 已跳过，原因：hidden"])
    serving(monkeypatch, result)

    exit_code = cli.main(command("--json"))

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert payload == {
        "status": "READY",
        "import_id": IMPORT_ID,
        "workspace_id": "default",
        "datasource_id": DATASOURCE_ID,
        "snapshot_id": SNAPSHOT_ID,
        "scan_version": 1,
        "original_filename": "orders.xlsx",
        "file_sha256": FILE_SHA256,
        "artifact_directory": str(tmp_path / "artifacts" / FILE_SHA256),
        "policy_version": "excel-ingestion-v1",
        "neo4j_publication_verified": True,
        "sheets": result.model_dump(mode="json")["sheets"],
        "warnings": ["工作表 'hidden' 已跳过，原因：hidden"],
    }


def test_json_output_keeps_incidental_stdout_out_of_the_document(
    tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", str(tmp_path / "catalog.db"))
    result = imported(tmp_path)

    def noisy(request: ExcelImportRequest) -> ExcelImportResult:
        print("driver warning")
        return result

    installed(monkeypatch, Mock(spec=QanerisService, import_excel=Mock(side_effect=noisy)))

    assert cli.main(command("--json")) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["import_id"] == IMPORT_ID
    assert captured.err == "driver warning\n"


# --------------------------------------------------------------------------------------------
# failure output
# --------------------------------------------------------------------------------------------


def formula_error() -> ExcelIngestionError:
    return ExcelIngestionError(
        ExcelErrorCode.FORMULA_UNSUPPORTED,
        "工作簿不支持公式，也不接受缓存计算结果；请另存为数值后重新导入",
        sheet="orders",
        coordinate="B3",
    )


@pytest.mark.parametrize(
    "error, code",
    [
        (formula_error(), ExcelErrorCode.FORMULA_UNSUPPORTED.value),
        (
            ExcelIngestionError(ExcelErrorCode.INVALID_CONTAINER, "待导入文件不存在或不是普通文件"),
            ExcelErrorCode.INVALID_CONTAINER.value,
        ),
        (
            ExcelIngestionError(ExcelErrorCode.SCAN_FAILED, "未产生 READY 的活跃 ScanSnapshot"),
            ExcelErrorCode.SCAN_FAILED.value,
        ),
    ],
)
def test_stable_error_code_survives_as_json(error, code, tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", str(tmp_path / "catalog.db"))
    refusing(monkeypatch, error)

    exit_code = cli.main(command("--json"))

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert captured.err == ""
    assert payload["status"] == "operation_failed"
    assert payload["error"]["type"] == "ExcelIngestionError"
    assert payload["error"]["code"] == code
    assert payload["error"]["message"] == error.message


def test_error_json_carries_the_safe_location(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", str(tmp_path / "catalog.db"))
    refusing(monkeypatch, formula_error())

    assert cli.main(command("--json")) == 1

    error = json.loads(capsys.readouterr().out)["error"]
    assert error["sheet"] == "orders"
    assert error["coordinate"] == "B3"
    assert not any(token in json.dumps(error, ensure_ascii=False) for token in BUSINESS_TOKENS)


def test_error_json_has_stable_keys_when_no_location_is_known(
    tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", str(tmp_path / "catalog.db"))
    refusing(monkeypatch, ExcelIngestionError(ExcelErrorCode.SCAN_FAILED, "扫描失败"))

    assert cli.main(command("--json")) == 1

    error = json.loads(capsys.readouterr().out)["error"]
    assert error["sheet"] is None
    assert error["coordinate"] is None


def test_error_human_output_goes_to_stderr(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", str(tmp_path / "catalog.db"))
    refusing(monkeypatch, formula_error())

    assert cli.main(command()) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "Import failed: EXCEL_FORMULA_UNSUPPORTED: 工作簿不支持公式，也不接受缓存计算结果；"
        "请另存为数值后重新导入 (sheet=orders, cell=B3)" in captured.err
    )
    assert not any(token in captured.err for token in BUSINESS_TOKENS)


def test_unexpected_exception_keeps_the_safe_boundary(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", str(tmp_path / "catalog.db"))
    refusing(monkeypatch, RuntimeError("provider rejected unit-super-secret"))

    assert cli.main(command("--json")) == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "operation_failed"
    assert payload["error"] == {"type": "RuntimeError", "message": "RuntimeError"}
    assert "unit-super-secret" not in captured.out
    assert "unit-super-secret" not in captured.err


# --------------------------------------------------------------------------------------------
# usage errors
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        ["source", "import-excel"],
        ["source", "import-excel", "orders.xlsx", "--name", ""],
        ["source", "import-excel", "orders.xlsx", "--name", "   "],
        ["source", "import-excel", "orders.xlsx", "--name", "x" * 101],
        ["source", "import-excel", "orders.xlsx", "--workspace", "  "],
        ["source", "import-excel", "orders.xlsx", "--force"],
    ],
)
def test_invalid_usage_exits_two(arguments, capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(arguments)

    assert raised.value.code == 2
    assert "usage:" in capsys.readouterr().err
