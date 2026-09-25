from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from smartdata.application.service import SmartDataService
from smartdata.catalog import Catalog
from smartdata.cli.config import (
    DatasourceConfigError,
    load_datasource_requests,
    load_single_datasource_request,
)
from smartdata.cli.render import operation_error_detail, render_table
from smartdata.contracts import (
    ConnectionTestReport,
    DatasetInfo,
    Datasource,
    DatasourceDetail,
    ScanRunReport,
)
from smartdata.contracts.connection import SecureDatasourceTest, SecureDatasourceUpdate
from smartdata.ingestion.excel import ExcelImportRequest, ExcelImportResult
from smartdata.scanning import MultiDatabaseScanRunner

EXCEL_IMPORT_COMMAND = "import-excel"
LIST_COMMAND = "list"
SHOW_COMMAND = "show"
SCAN_ONE_COMMAND = "scan-one"
TEST_ONE_COMMAND = "test-one"
CREATE_COMMAND = "create"
UPDATE_COMMAND = "update"
DELETE_COMMAND = "delete"

#: The ``source`` subcommands that read or refresh one catalog entry instead of running a
#: configured batch. They are dispatched separately from ``test`` / ``scan``.
CATALOG_COMMANDS = (LIST_COMMAND, SHOW_COMMAND, SCAN_ONE_COMMAND)

#: The single-datasource secure lifecycle commands. Each addresses one connection from one config
#: file and calls exactly one Application Service method; none of them scans.
SECURE_COMMANDS = (TEST_ONE_COMMAND, CREATE_COMMAND, UPDATE_COMMAND, DELETE_COMMAND)

#: The catalog columns the product publishes. The stored connection document, a secret reference
#: and any credential are never among them, so these rows can be printed as-is.
_LIST_COLUMNS = ("ID", "Name", "Kind", "Driver", "Status", "Workspace")

# Mirrors ``ExcelImportRequest.datasource_name``. The CLI rejects an unusable value before the
# request is built, so bad input is a usage error (exit 2) instead of an ingestion failure.
_DATASOURCE_NAME_LIMIT = 100

#: Exit codes follow the existing CLI convention: 0 for a completed product operation, 1 for an
#: operation that ran and failed. Usage and configuration errors are reported as 2 by ``main``.
_EXIT_OK = 0
_EXIT_FAILED = 1

# The machine projection of one import. Every value comes from ``ExcelImportResult``; it is a
# stable field selection, not a second source of truth. Internal file locations stay out of the
# product output - the managed artifact directory is the location a user is shown.
_JSON_FIELDS = (
    "status",
    "import_id",
    "workspace_id",
    "datasource_id",
    "snapshot_id",
    "scan_version",
    "original_filename",
    "file_sha256",
    "artifact_directory",
    "policy_version",
    "neo4j_publication_verified",
    "sheets",
    "warnings",
)


def _datasource_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise argparse.ArgumentTypeError("must not be empty")
    if len(name) > _DATASOURCE_NAME_LIMIT:
        raise argparse.ArgumentTypeError(
            f"must be at most {_DATASOURCE_NAME_LIMIT} characters"
        )
    return name


def _workspace_id(value: str) -> str:
    workspace = value.strip()
    if not workspace:
        raise argparse.ArgumentTypeError("must not be empty")
    return workspace


def _datasource_id(value: str) -> str:
    datasource = value.strip()
    if not datasource:
        raise argparse.ArgumentTypeError("must not be empty")
    return datasource


def add_source_commands(commands: argparse._SubParsersAction) -> None:
    source = commands.add_parser("source", help="list, inspect, test, scan and import datasources")
    actions = source.add_subparsers(dest="source_command", required=True)

    listing = actions.add_parser("list", help="list the datasources of one workspace")
    listing.add_argument(
        "--workspace",
        dest="workspace_id",
        type=_workspace_id,
        default="default",
        help="workspace id (default: default)",
    )
    listing.add_argument("--json", action="store_true", dest="json_output")

    show = actions.add_parser("show", help="show one datasource by id")
    show.add_argument(
        "datasource_id",
        metavar="DATASOURCE_ID",
        type=_datasource_id,
        help="the datasource id (an id, never a name)",
    )
    show.add_argument("--json", action="store_true", dest="json_output")

    scan_one = actions.add_parser("scan-one", help="scan one datasource by id")
    scan_one.add_argument(
        "datasource_id",
        metavar="DATASOURCE_ID",
        type=_datasource_id,
        help="the datasource id (an id, never a name)",
    )
    scan_one.add_argument("--json", action="store_true", dest="json_output")

    test_one = actions.add_parser(
        "test-one", help="test one secure datasource connection without saving it"
    )
    test_one.add_argument("--config", required=True, type=Path)
    test_one.add_argument("--json", action="store_true", dest="json_output")

    create = actions.add_parser(
        "create", help="test and save one secure datasource (does not scan)"
    )
    create.add_argument("--config", required=True, type=Path)
    create.add_argument("--json", action="store_true", dest="json_output")

    update = actions.add_parser(
        "update", help="switch one secure datasource onto a new connection profile"
    )
    update.add_argument(
        "datasource_id",
        metavar="DATASOURCE_ID",
        type=_datasource_id,
        help="the datasource id (an id, never a name)",
    )
    update.add_argument("--config", required=True, type=Path)
    update.add_argument("--json", action="store_true", dest="json_output")

    delete = actions.add_parser("delete", help="delete one datasource and its published graph")
    delete.add_argument(
        "datasource_id",
        metavar="DATASOURCE_ID",
        type=_datasource_id,
        help="the datasource id (an id, never a name)",
    )
    delete.add_argument("--json", action="store_true", dest="json_output")

    test = actions.add_parser("test", help="test all configured datasource connections")
    test.add_argument("--config", required=True, type=Path)
    test.add_argument("--json", action="store_true", dest="json_output")

    scan = actions.add_parser(
        "scan", help="scan all configured datasources and create a ScanRun"
    )
    scan.add_argument("--config", required=True, type=Path)
    scan.add_argument("--json", action="store_true", dest="json_output")

    excel = actions.add_parser(
        "import-excel", help="import an .xlsx workbook as a first-class datasource"
    )
    excel.add_argument("file", type=Path, metavar="FILE", help="the .xlsx workbook to import")
    excel.add_argument(
        "--name",
        dest="datasource_name",
        type=_datasource_name,
        help="datasource name (defaults to 'Excel: <filename>')",
    )
    excel.add_argument(
        "--workspace",
        dest="workspace_id",
        type=_workspace_id,
        default="default",
        help="workspace id (default: default)",
    )
    excel.add_argument("--json", action="store_true", dest="json_output")


def run_source(args: argparse.Namespace) -> ConnectionTestReport | ScanRunReport:
    config_path = args.config.expanduser().resolve()
    requests = load_datasource_requests(config_path)
    runner = MultiDatabaseScanRunner()
    if args.source_command == "test":
        return runner.test_connections(requests)
    return runner.scan(requests, config_source=str(config_path))


def application_service() -> SmartDataService:
    """Build the Application Service from the runtime environment.

    The catalog follows the same ``SMARTDATA_CATALOG`` convention that ``doctor``, the API and the
    MCP server already use, so every entry point of one deployment addresses the same catalog
    instead of silently splitting into a cwd-relative one. Everything else - Neo4j store/reader and
    artifact root - is taken from the environment unchanged.
    """
    return SmartDataService(Catalog(os.getenv("SMARTDATA_CATALOG", "smartdata.db")))


def run_excel_import(args: argparse.Namespace) -> ExcelImportResult:
    """Import one workbook through the only Application Service entry point.

    The service is built from the normal runtime environment, and the CLI never injects a fake
    graph, a private pipeline or a Null reader. It also never reaches past ``import_excel`` into the
    validator, the materializer, the catalog APIs or the graph writer.
    """
    request = ExcelImportRequest(
        file_path=str(args.file),
        datasource_name=args.datasource_name,
        workspace_id=args.workspace_id,
    )
    return application_service().import_excel(request)


def execute_source(args: argparse.Namespace) -> int:
    report = run_source(args)
    if isinstance(report, ConnectionTestReport):
        print_connection_report(report, json_output=args.json_output)
        return 0 if report.status == "passed" else 1
    print_scan_report(report, json_output=args.json_output)
    return 0 if report.passed else 1


def _collect(operation: Callable[[], Any]) -> tuple[Any, Exception | None]:
    """Run one service operation, isolating anything it prints on stdout.

    The CLI emits its own report after this returns, so a JSON report can never be contaminated by
    incidental driver, provider or debug output - and a scan, which really does open a datasource,
    is covered by the same rule as a plain catalog read.
    """
    with redirect_stdout(sys.stderr):
        try:
            return operation(), None
        except Exception as error:  # noqa: BLE001 - safe top-level CLI boundary
            return None, error


def _scan_one(service: SmartDataService, datasource_id: str) -> dict[str, Any]:
    """Scan one datasource through the Application Service and project the result publicly.

    ``scan_datasource`` owns the scan; ``inspect_datasource`` then reports the active snapshot
    version that scan produced. Neither the scanner nor the catalog schema is reimplemented here.
    """
    datasets: list[DatasetInfo] = service.scan_datasource(datasource_id)
    detail = service.inspect_datasource(datasource_id)
    return {
        "status": "completed",
        "datasource_id": datasource_id,
        "scan_version": detail.scan_version,
        "last_scan_status": detail.last_scan_status,
        "dataset_count": len(datasets),
        "datasets": [{"name": item.name, "kind": item.kind} for item in datasets],
    }


def run_catalog_command(args: argparse.Namespace) -> int:
    """Run one catalog-reading ``source`` subcommand and return its exit code.

    Every branch is a thin projection over ``SmartDataService``: the CLI writes no catalog query, so
    it cannot drift from the product rules the Application Service owns.
    """
    command = args.source_command
    json_output = bool(args.json_output)
    service = application_service()

    if command == LIST_COMMAND:
        datasources, failure = _collect(lambda: service.list_datasources(args.workspace_id))
        if failure is not None:
            return report_catalog_failure(args, failure, json_output=json_output)
        print_datasource_list(
            datasources, workspace_id=args.workspace_id, json_output=json_output
        )
        return _EXIT_OK

    if command == SHOW_COMMAND:
        detail, failure = _collect(lambda: service.inspect_datasource(args.datasource_id))
        if failure is not None:
            return report_catalog_failure(args, failure, json_output=json_output)
        print_datasource_detail(detail, json_output=json_output)
        return _EXIT_OK

    scanned, failure = _collect(lambda: _scan_one(service, args.datasource_id))
    if failure is not None:
        return report_catalog_failure(args, failure, json_output=json_output)
    print_scan_one_report(scanned, json_output=json_output)
    return _EXIT_OK


def report_catalog_failure(
    args: argparse.Namespace, error: Exception, *, json_output: bool
) -> int:
    """Report a failed ``source`` operation, keeping its stable code and without a traceback."""
    detail = operation_error_detail(error)
    if json_output:
        print(
            json.dumps(
                {"status": "operation_failed", "error": detail}, ensure_ascii=False, indent=2
            )
        )
        return _EXIT_FAILED
    print(f"Source failed: {detail['message']}", file=sys.stderr)
    return _EXIT_FAILED


def run_secure_command(args: argparse.Namespace) -> int:
    """Run one single-datasource secure lifecycle command and return its exit code.

    Each branch is one Application Service call. The ordering rules - candidate-first update,
    graph-first delete, no implicit scan - belong to the service and are not restated here; the CLI
    only preserves the fact that ``create`` and ``update`` leave the datasource at ``created``.
    """
    command = args.source_command
    json_output = bool(args.json_output)
    service = application_service()

    if command == DELETE_COMMAND:
        _, failure = _collect(lambda: service.delete_datasource(args.datasource_id))
        if failure is not None:
            return report_catalog_failure(args, failure, json_output=json_output)
        print_datasource_deleted(args.datasource_id, json_output=json_output)
        return _EXIT_OK

    config_path = args.config.expanduser().resolve()
    request = load_single_datasource_request(config_path)

    if command == TEST_ONE_COMMAND:
        result, failure = _collect(
            lambda: service.test_secure_datasource(
                SecureDatasourceTest(
                    kind=request.kind, connection_profile=request.connection_profile
                )
            )
        )
        if failure is not None:
            return report_catalog_failure(args, failure, json_output=json_output)
        print_connection_test_one(result, json_output=json_output)
        return _EXIT_OK

    if command == CREATE_COMMAND:
        datasource, failure = _collect(lambda: service.create_secure_datasource(request))
        if failure is not None:
            return report_catalog_failure(args, failure, json_output=json_output)
        print_datasource_created(datasource, json_output=json_output)
        return _EXIT_OK

    return _run_secure_update(service, args, request, json_output=json_output)


def _run_secure_update(
    service: SmartDataService,
    args: argparse.Namespace,
    request: Any,
    *,
    json_output: bool,
) -> int:
    """Update one secure datasource after proving the config still describes the same datasource.

    ``name``, ``kind`` and ``workspace_id`` are immutable in this contract, and an update carries a
    ``connection_profile`` only. A config that names a different datasource is therefore a mistake
    about which datasource is being changed, not a rename request, and it is refused rather than
    partially applied.
    """
    current, failure = _collect(lambda: service.inspect_datasource(args.datasource_id))
    if failure is not None:
        return report_catalog_failure(args, failure, json_output=json_output)
    mismatched = [
        field
        for field, configured, existing in (
            ("name", request.name, current.name),
            ("kind", request.kind, current.kind),
            ("workspace_id", request.workspace_id, current.workspace_id),
        )
        if configured != existing
    ]
    if mismatched:
        raise DatasourceConfigError(
            "source update may change connection_profile only; "
            f"{', '.join(mismatched)} must match the existing datasource"
        )

    updated, failure = _collect(
        lambda: service.update_secure_datasource(
            args.datasource_id,
            SecureDatasourceUpdate(connection_profile=request.connection_profile),
        )
    )
    if failure is not None:
        return report_catalog_failure(args, failure, json_output=json_output)
    print_datasource_updated(updated, json_output=json_output)
    return _EXIT_OK


def datasource_payload(datasource: Datasource) -> dict[str, Any]:
    """The public projection of one datasource: identity and status, never connection material."""
    return {
        "id": datasource.id,
        "name": datasource.name,
        "kind": datasource.kind.value,
        "driver": datasource.driver,
        "status": datasource.status,
        "workspace_id": datasource.workspace_id,
    }


def print_connection_test_one(result: Any, *, json_output: bool) -> None:
    """Report one successful candidate test. The endpoint, username and references stay inside."""
    if json_output:
        print(
            json.dumps(
                {
                    "status": "completed",
                    "driver": result.driver,
                    "tls_enabled": result.tls_enabled,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print("[PASS] Datasource connection test passed")
    print(f"Driver: {result.driver}")
    print(f"TLS: {'enabled' if result.tls_enabled else 'disabled'}")


def print_datasource_created(datasource: Datasource, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {"status": "completed", "datasource": datasource_payload(datasource)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print("[PASS] Datasource created")
    print(f"Datasource ID: {datasource.id}")
    print(f"Name: {datasource.name}")
    print(f"Driver: {datasource.driver}")
    print(f"Status: {datasource.status}")
    print(f"Next: smartdata source scan-one {datasource.id}")


def print_datasource_updated(datasource: Datasource, *, json_output: bool) -> None:
    """Report one profile switch. The new profile is not scanned, so ``created`` is the outcome."""
    if json_output:
        print(
            json.dumps(
                {
                    "status": "completed",
                    "datasource": datasource_payload(datasource),
                    "scan_required": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print("[PASS] Datasource updated")
    print(f"Datasource ID: {datasource.id}")
    print(f"Status: {datasource.status}")
    # The old active scan is invalidated by the update itself; the CLI does not read the catalog to
    # discover this, it reports the Application Contract's fixed semantics.
    print("Previous scan invalidated: yes")
    print(f"Next: smartdata source scan-one {datasource.id}")


def print_datasource_deleted(datasource_id: str, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {"status": "completed", "datasource_id": datasource_id},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print("[PASS] Datasource deleted")
    print(f"Datasource ID: {datasource_id}")


def datasource_list_payload(datasources: Sequence[Datasource]) -> list[dict[str, Any]]:
    """The public projection of ``source list``: identity and status, never connection material."""
    return [
        {
            "id": item.id,
            "name": item.name,
            "kind": item.kind.value,
            "driver": item.driver,
            "status": item.status,
            "workspace_id": item.workspace_id,
        }
        for item in datasources
    ]


def print_datasource_list(
    datasources: Sequence[Datasource], *, workspace_id: str, json_output: bool
) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "workspace_id": workspace_id,
                    "datasource_count": len(datasources),
                    "datasources": datasource_list_payload(datasources),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not datasources:
        print(f"No datasources in workspace {workspace_id}.")
        return
    rows = [
        {
            "ID": item.id,
            "Name": item.name,
            "Kind": item.kind.value,
            "Driver": item.driver,
            "Status": item.status,
            "Workspace": item.workspace_id,
        }
        for item in datasources
    ]
    for line in render_table(_LIST_COLUMNS, rows):
        print(line)


def print_datasource_detail(detail: DatasourceDetail, *, json_output: bool) -> None:
    """Report one datasource: public identity plus the scan facts the catalog actually holds."""
    if json_output:
        print(json.dumps(detail.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    print(f"Datasource ID: {detail.id}")
    print(f"Name: {detail.name}")
    print(f"Kind: {detail.kind.value}")
    print(f"Driver: {detail.driver}")
    print(f"Status: {detail.status}")
    print(f"Workspace: {detail.workspace_id}")
    if detail.scan_version is None:
        # No active snapshot means no successful scan; a version is never invented for display.
        print("Scan version: not scanned")
        return
    print(f"Scan version: {detail.scan_version}")
    print(f"Last scan status: {detail.last_scan_status}")
    print(f"Dataset count: {detail.dataset_count}")


def print_scan_one_report(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print("[PASS] Scan completed")
    print(f"Datasource: {payload['datasource_id']}")
    version = payload.get("scan_version")
    print(f"Scan version: {version if version is not None else 'unknown'}")
    print(f"Datasets: {payload['dataset_count']}")
    for item in payload.get("datasets") or []:
        print(f"  - {item['name']} ({item['kind']})")


def excel_import_payload(result: ExcelImportResult) -> dict[str, Any]:
    dumped = result.model_dump(mode="json")
    return {name: dumped[name] for name in _JSON_FIELDS}


def print_connection_report(report: ConnectionTestReport, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    for item in report.results:
        print(f"[{'PASS' if item.status == 'passed' else 'FAIL'}] {item.name}")
        if item.error:
            print(f"  Error: {item.error}")
    print()
    print(f"{report.connection_passed}/{report.configured_datasources} passed")


def print_scan_report(report: ScanRunReport, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    for item in report.datasources:
        print(f"[{'PASS' if item.status == 'passed' else 'FAIL'}] {item.name}")
        if item.error:
            print(f"  Error: {item.error}")
    print()
    print(f"{report.summary.scan_passed}/{report.summary.configured_datasources} passed")
    print(f"Run: {report.run.run_id}")
    print(f"Artifacts: {report.artifact_paths['scan_report_json']}")


def print_excel_import_report(
    result: ExcelImportResult, *, json_output: bool, datasource_name: str | None = None
) -> None:
    """Report one READY import: identity and provenance only, never workbook content."""
    if json_output:
        print(json.dumps(excel_import_payload(result), ensure_ascii=False, indent=2))
        return
    print("[PASS] Excel import ready")
    if datasource_name:
        print(f"Name: {datasource_name}")
    print(f"Import ID: {result.import_id}")
    print(f"Datasource: {result.datasource_id}")
    print(f"Snapshot: {result.snapshot_id}")
    print(f"Scan version: {result.scan_version}")
    print(f"Sheets: {len(result.sheets)}")
    print(f"Artifact: {result.artifact_directory}")
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"  - {warning}")
