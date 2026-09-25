"""CLI contract tests for ``smartdata source list`` / ``show`` / ``scan-one`` (RS-CLI-01B).

These lock the interface contract only: the argument wiring, the single Application Service call
per command, the human and JSON output shapes, stdout purity for JSON reports, the stable product
error codes, exit codes, and the absence of any connection material - a stored connection
document, a secret reference or a credential must never reach these commands' output.

The catalog and the scanner themselves are covered by the Core suites and are deliberately not
retested here.
"""

from __future__ import annotations

import inspect
import json
from typing import Any
from unittest.mock import Mock

import pytest

from smartdata.application.service import SmartDataService
from smartdata.cli import main as cli
from smartdata.cli import source as source_cli
from smartdata.common.errors import DatasourceNotFoundError, SmartDataError
from smartdata.contracts import DatasetInfo, Datasource, DatasourceDetail

DATASOURCE_ID = "ds_ff606cfffe96"
OTHER_ID = "ds_0011223344"

# Connection material that must never reach any output: the catalog holds it, the product does not
# publish it.
SECRET_CONNECTION = '{"driver": "postgresql", "url": "postgresql://ro:s3cret@10.0.0.5/prod"}'
SECRET_PASSWORD = "s3cret"


def datasource(**changes: Any) -> Datasource:
    values: dict[str, Any] = {
        "id": DATASOURCE_ID,
        "name": "sales_2026_07",
        "kind": "relational",
        "workspace_id": "default",
        "driver": "sqlite",
        "status": "ready",
    }
    values.update(changes)
    return Datasource(**values)


def detail(**changes: Any) -> DatasourceDetail:
    values: dict[str, Any] = {
        "id": DATASOURCE_ID,
        "name": "sales_2026_07",
        "kind": "relational",
        "workspace_id": "default",
        "driver": "sqlite",
        "status": "ready",
        "scan_version": 3,
        "last_scan_status": "ready",
        "dataset_count": 4,
    }
    values.update(changes)
    return DatasourceDetail(**values)


def datasets() -> list[DatasetInfo]:
    return [
        DatasetInfo(datasource_id=DATASOURCE_ID, name=name, kind="table")
        for name in ("customers", "order_items", "orders", "products")
    ]


def installed(monkeypatch: pytest.MonkeyPatch, service: Any) -> Mock:
    constructor = Mock(return_value=service)
    monkeypatch.setattr(source_cli, "SmartDataService", constructor)
    return constructor


def serving(monkeypatch: pytest.MonkeyPatch, **methods: Any) -> Any:
    service = Mock(spec=SmartDataService, **methods)
    installed(monkeypatch, service)
    return service


def run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    try:
        code = cli.main(argv)
    except SystemExit as error:  # argparse usage errors
        code = error.code if isinstance(error.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------------------------
# source list
# --------------------------------------------------------------------------------------------


def test_source_list_human_reports_the_public_columns(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = Mock(return_value=[datasource(), datasource(id=OTHER_ID, name="warehouse")])
    serving(monkeypatch, list_datasources=listing)

    code, stdout, stderr = run(capsys, ["source", "list"])

    assert code == 0
    assert stderr == ""
    header = stdout.splitlines()[0]
    for column in ("ID", "Name", "Kind", "Driver", "Status", "Workspace"):
        assert column in header
    assert DATASOURCE_ID in stdout and OTHER_ID in stdout
    assert "sales_2026_07" in stdout and "warehouse" in stdout
    assert "relational" in stdout and "sqlite" in stdout and "ready" in stdout


def test_source_list_defaults_to_the_default_workspace(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = Mock(return_value=[])
    service = serving(monkeypatch, list_datasources=listing)

    code, stdout, _ = run(capsys, ["source", "list"])

    assert code == 0
    assert listing.call_args.args == ("default",)
    assert "No datasources in workspace default." in stdout
    assert service.ask.call_count == 0


def test_source_list_workspace_reaches_the_application_service(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = Mock(return_value=[])
    serving(monkeypatch, list_datasources=listing)

    assert run(capsys, ["source", "list", "--workspace", "ws1"])[0] == 0
    assert listing.call_args.args == ("ws1",)


def test_source_list_json_is_the_public_projection(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(monkeypatch, list_datasources=Mock(return_value=[datasource()]))

    code, stdout, stderr = run(capsys, ["source", "list", "--json"])

    assert code == 0
    assert stderr == ""
    document = json.loads(stdout)
    assert document["workspace_id"] == "default"
    assert document["datasource_count"] == 1
    assert document["datasources"] == [
        {
            "id": DATASOURCE_ID,
            "name": "sales_2026_07",
            "kind": "relational",
            "driver": "sqlite",
            "status": "ready",
            "workspace_id": "default",
        }
    ]


def test_source_list_never_publishes_connection_material(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The catalog row that produced this projection also holds a connection document; the
    # projection is an explicit field selection, so no connection material can reach the output.
    serving(monkeypatch, list_datasources=Mock(return_value=[datasource()]))

    _, stdout, _ = run(capsys, ["source", "list", "--json"])

    document = json.loads(stdout)
    assert set(document["datasources"][0]) == {
        "id",
        "name",
        "kind",
        "driver",
        "status",
        "workspace_id",
    }
    assert SECRET_PASSWORD not in stdout


# --------------------------------------------------------------------------------------------
# source show
# --------------------------------------------------------------------------------------------


def test_source_show_human_reports_identity_and_scan_facts(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    show = Mock(return_value=detail())
    serving(monkeypatch, inspect_datasource=show)

    code, stdout, stderr = run(capsys, ["source", "show", DATASOURCE_ID])

    assert code == 0
    assert stderr == ""
    assert f"Datasource ID: {DATASOURCE_ID}" in stdout
    assert "Name: sales_2026_07" in stdout
    assert "Kind: relational" in stdout
    assert "Driver: sqlite" in stdout
    assert "Status: ready" in stdout
    assert "Workspace: default" in stdout
    assert "Scan version: 3" in stdout
    assert "Last scan status: ready" in stdout
    assert "Dataset count: 4" in stdout
    assert show.call_args.args == (DATASOURCE_ID,)


def test_source_show_reports_an_unscanned_datasource_without_inventing_a_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(
        monkeypatch,
        inspect_datasource=Mock(
            return_value=detail(
                status="created", scan_version=None, last_scan_status=None, dataset_count=0
            )
        ),
    )

    code, stdout, _ = run(capsys, ["source", "show", DATASOURCE_ID])

    assert code == 0
    assert "Scan version: not scanned" in stdout
    assert "Last scan status" not in stdout


def test_source_show_json_is_the_contract_projection(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(monkeypatch, inspect_datasource=Mock(return_value=detail()))

    code, stdout, stderr = run(capsys, ["source", "show", DATASOURCE_ID, "--json"])

    assert code == 0
    assert stderr == ""
    document = json.loads(stdout)
    assert set(document) == {
        "id",
        "name",
        "kind",
        "driver",
        "status",
        "workspace_id",
        "scan_version",
        "last_scan_status",
        "dataset_count",
    }
    assert document["scan_version"] == 3
    assert SECRET_CONNECTION not in stdout


def test_source_show_unknown_id_keeps_the_stable_code_and_exits_one(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(
        monkeypatch,
        inspect_datasource=Mock(side_effect=DatasourceNotFoundError("数据源不存在：ds_missing")),
    )

    code, stdout, stderr = run(capsys, ["source", "show", "ds_missing", "--json"])

    assert code == 1
    document = json.loads(stdout)
    assert document["status"] == "operation_failed"
    assert document["error"]["code"] == "datasource_not_found"
    assert "Traceback" not in stdout + stderr


def test_source_show_human_failure_stays_on_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(
        monkeypatch,
        inspect_datasource=Mock(side_effect=DatasourceNotFoundError("数据源不存在：ds_missing")),
    )

    code, stdout, stderr = run(capsys, ["source", "show", "ds_missing"])

    assert code == 1
    assert stdout == ""
    assert "Source failed: 数据源不存在：ds_missing" in stderr
    assert "Traceback" not in stderr


def test_an_unexpected_error_exposes_only_its_type(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(
        monkeypatch,
        inspect_datasource=Mock(side_effect=RuntimeError(f"driver said {SECRET_CONNECTION}")),
    )

    code, stdout, stderr = run(capsys, ["source", "show", DATASOURCE_ID, "--json"])

    assert code == 1
    document = json.loads(stdout)
    assert document["error"]["type"] == "RuntimeError"
    assert document["error"]["message"] == "RuntimeError"
    assert SECRET_PASSWORD not in stdout + stderr


# --------------------------------------------------------------------------------------------
# source scan-one
# --------------------------------------------------------------------------------------------


def test_source_scan_one_uses_the_application_service_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scan = Mock(return_value=datasets())
    serving(monkeypatch, scan_datasource=scan, inspect_datasource=Mock(return_value=detail()))

    assert run(capsys, ["source", "scan-one", DATASOURCE_ID])[0] == 0
    assert scan.call_args.args == (DATASOURCE_ID,)
    assert scan.call_count == 1


def test_source_scan_one_human_reports_the_scan_result(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(
        monkeypatch,
        scan_datasource=Mock(return_value=datasets()),
        inspect_datasource=Mock(return_value=detail()),
    )

    code, stdout, stderr = run(capsys, ["source", "scan-one", DATASOURCE_ID])

    assert code == 0
    assert stderr == ""
    assert stdout.startswith("[PASS] Scan completed")
    assert f"Datasource: {DATASOURCE_ID}" in stdout
    assert "Scan version: 3" in stdout
    assert "Datasets: 4" in stdout
    assert "  - orders (table)" in stdout


def test_source_scan_one_json_is_a_stable_projection(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(
        monkeypatch,
        scan_datasource=Mock(return_value=datasets()),
        inspect_datasource=Mock(return_value=detail()),
    )

    code, stdout, stderr = run(capsys, ["source", "scan-one", DATASOURCE_ID, "--json"])

    assert code == 0
    assert stderr == ""
    document = json.loads(stdout)
    assert set(document) == {
        "status",
        "datasource_id",
        "scan_version",
        "last_scan_status",
        "dataset_count",
        "datasets",
    }
    assert document["status"] == "completed"
    assert document["scan_version"] == 3
    assert document["dataset_count"] == 4


def test_source_scan_one_json_stdout_stays_pure_when_a_scan_is_noisy(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def noisy_scan(datasource_id: str) -> list[DatasetInfo]:
        print("driver warning noise")
        return datasets()

    serving(
        monkeypatch,
        scan_datasource=Mock(side_effect=noisy_scan),
        inspect_datasource=Mock(return_value=detail()),
    )

    code, stdout, stderr = run(capsys, ["source", "scan-one", DATASOURCE_ID, "--json"])

    assert code == 0
    assert json.loads(stdout)["status"] == "completed"
    assert "driver warning noise" in stderr


def test_source_scan_one_unknown_id_exits_one_with_the_stable_code(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(
        monkeypatch,
        scan_datasource=Mock(side_effect=DatasourceNotFoundError("数据源不存在：ds_missing")),
        inspect_datasource=Mock(),
    )

    code, stdout, _ = run(capsys, ["source", "scan-one", "ds_missing", "--json"])

    assert code == 1
    assert json.loads(stdout)["error"]["code"] == "datasource_not_found"


# --------------------------------------------------------------------------------------------
# boundary hygiene
# --------------------------------------------------------------------------------------------


def test_catalog_commands_never_run_the_configured_batch_runner(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Mock(side_effect=AssertionError("source list must not run the batch scanner"))
    monkeypatch.setattr(source_cli, "MultiDatabaseScanRunner", runner)
    serving(monkeypatch, list_datasources=Mock(return_value=[]))

    assert run(capsys, ["source", "list"])[0] == 0
    runner.assert_not_called()


def test_catalog_commands_never_query_the_catalog_directly(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every catalog read must go through the Application Service, not through its catalog port."""
    service = Mock()
    service.list_datasources.return_value = [datasource()]
    service.inspect_datasource.return_value = detail()
    service.scan_datasource.return_value = datasets()
    installed(monkeypatch, service)

    assert run(capsys, ["source", "list"])[0] == 0
    assert run(capsys, ["source", "show", DATASOURCE_ID])[0] == 0
    assert run(capsys, ["source", "scan-one", DATASOURCE_ID])[0] == 0

    assert [item[0] for item in service.mock_calls] == [
        "list_datasources",
        "inspect_datasource",
        "scan_datasource",
        "inspect_datasource",
    ]
    # ``catalog`` is the Application Service's own port; the CLI never acquires it.
    assert service.catalog.mock_calls == []


def test_the_source_cli_keeps_its_datasource_id_contract() -> None:
    """The CLI addresses a datasource by id and never acquires the catalog port itself."""
    module_source = inspect.getsource(source_cli)
    for forbidden in ("get_datasource", "get_active_snapshot", "list_datasets", "get_connection"):
        assert forbidden not in module_source, forbidden
    # No secret may be passed on the command line: the credential lifecycle is not a CLI feature.
    for option in ("--password", "--token", "--secret", "--api-key"):
        assert option not in module_source, option


@pytest.mark.parametrize(
    "arguments",
    [
        ["source", "show"],
        ["source", "show", ""],
        ["source", "scan-one"],
        ["source", "scan-one", ""],
        ["source", "list", "--workspace", "   "],
    ],
)
def test_usage_errors_exit_two(
    arguments: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(monkeypatch)
    code, _, _ = run(capsys, arguments)
    assert code == 2


def test_a_smartdata_error_exposes_its_code_through_the_shared_helper() -> None:
    """The failure projection is shared, so a product code is never replaced by a bare type name."""
    error = DatasourceNotFoundError("数据源不存在：ds_missing")
    service_error = SmartDataError("boom")

    assert source_cli.operation_error_detail(error)["code"] == "datasource_not_found"
    assert source_cli.operation_error_detail(error)["message"] == "数据源不存在：ds_missing"
    assert source_cli.operation_error_detail(service_error)["message"] == "boom"
    # An arbitrary exception exposes only its type: its message could carry a credential.
    anonymous = source_cli.operation_error_detail(ValueError(SECRET_CONNECTION))
    assert anonymous["type"] == "ValueError"
    assert anonymous["message"] == "ValueError"
    assert SECRET_PASSWORD not in json.dumps(anonymous)
