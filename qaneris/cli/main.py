from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from contextlib import nullcontext, redirect_stdout
from pathlib import Path

from qaneris.acceptance.phase1 import (
    AcceptanceConfigurationError,
    EnvironmentBlockedError,
    Phase1AcceptanceRunner,
    Phase1RunReport,
)
from qaneris.cli.ask import ASK_COMMAND, add_ask_command, run_ask_command
from qaneris.cli.config import DatasourceConfigError
from qaneris.cli.credentials import (
    CERTIFICATE_COMMAND,
    CREDENTIAL_COMMAND,
    CredentialInputError,
    add_certificate_commands,
    add_credential_commands,
    report_credential_failure,
    run_certificate_command,
    run_credential_command,
)
from qaneris.cli.doctor import doctor_passed, inspect_environment
from qaneris.cli.shell import SHELL_COMMAND, add_shell_command, run_shell_command
from qaneris.cli.source import (
    CATALOG_COMMANDS,
    EXCEL_IMPORT_COMMAND,
    SECURE_COMMANDS,
    add_source_commands,
    print_connection_report,
    print_excel_import_report,
    print_scan_report,
    report_catalog_failure,
    run_catalog_command,
    run_excel_import,
    run_secure_command,
    run_source,
)
from qaneris.common.environment import EnvironmentBootstrapError, load_runtime_environment
from qaneris.common.redaction import SecretRedactor
from qaneris.contracts import ConnectionTestReport
from qaneris.ingestion.excel import ExcelIngestionError
from qaneris.scanning import ScanRunConfigurationError


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _acceptance_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument(
        "--question-class",
        type=str.upper,
        choices=tuple("ABCDEF"),
        help="run only one fixed Phase 1 question class",
    )
    parser.add_argument("--repeat", type=_positive_int, default=1)
    parser.add_argument("--no-trace", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qaneris",
        description="Qaneris command-line interface.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="load this dotenv file (then QANERIS_ENV_FILE, then cwd/.env)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check the Phase 1 runtime environment")
    doctor.add_argument("--json", action="store_true", dest="json_output")

    acceptance = commands.add_parser("acceptance", help="run query acceptance suites")
    acceptance_commands = acceptance.add_subparsers(dest="acceptance_command", required=True)

    phase1 = acceptance_commands.add_parser(
        "phase1", help="run the fixed six-class Phase 1 suite"
    )
    _acceptance_arguments(phase1)

    ask = acceptance_commands.add_parser(
        "ask", help="run a suite through the query acceptance contract"
    )
    ask.add_argument("--suite", required=True, choices=("phase1",))
    _acceptance_arguments(ask)
    add_ask_command(commands)
    add_source_commands(commands)
    add_credential_commands(commands)
    add_certificate_commands(commands)
    add_shell_command(commands)
    return parser


def _print_doctor(checks: dict[str, dict[str, str]], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"checks": checks}, ensure_ascii=False, indent=2))
        return
    labels = {
        "artifact_root": "Artifact Root",
        "model_configuration": "Model configuration",
        "neo4j_configuration": "Neo4j configuration",
        "neo4j_connectivity": "Neo4j connectivity",
        "catalog_configuration": "Catalog configuration",
    }
    for key, item in checks.items():
        print(f"[{item['status'].upper()}] {labels[key]}")


def _print_acceptance(report: Phase1RunReport, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
        return
    repeated = report.summary["total"] > len({item.code for item in report.results})
    for item in report.results:
        label = {
            "passed": "PASS",
            "failed": "FAIL",
            "environment_blocked": "BLOCKED",
        }.get(item.status, item.status.upper())
        suffix = f" (repeat {item.repeat})" if repeated else ""
        print(f"[{label}] {item.code} {item.title}{suffix}")
    print()
    print(f"{report.summary['passed']}/{report.summary['total']} passed")
    print(f"Artifacts: {report.artifact_paths['report_json']}")


def _run_excel_import(args: argparse.Namespace) -> int:
    """Run one Excel import and report it.

    Exit code 0 is only reachable for a READY import, and the ingestion contract makes READY mean
    a verified Neo4j publication as well, so a printed success can never overstate the outcome.
    """
    with redirect_stdout(sys.stderr) if args.json_output else nullcontext():
        result = run_excel_import(args)
    print_excel_import_report(
        result, json_output=args.json_output, datasource_name=args.datasource_name
    )
    return 0


def _print_import_error(args: argparse.Namespace, error: ExcelIngestionError) -> None:
    """Report an ingestion refusal while keeping its stable machine code.

    Falling through to the generic boundary would replace ``EXCEL_FORMULA_UNSUPPORTED`` and friends
    with ``operation_failed`` and lose the only stable fact a caller can branch on. The message may
    name a sheet and a cell coordinate; it never carries a formula body or a stored cell value.
    """
    message = SecretRedactor.from_environment().text(error.message)
    if getattr(args, "json_output", False):
        print(
            json.dumps(
                {
                    "status": "operation_failed",
                    "error": {
                        "type": type(error).__name__,
                        "code": error.error_code,
                        "message": message,
                        "sheet": error.sheet,
                        "coordinate": error.coordinate,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    location = ""
    if error.sheet or error.coordinate:
        parts = [f"sheet={error.sheet}"] if error.sheet else []
        if error.coordinate:
            parts.append(f"cell={error.coordinate}")
        location = f" ({', '.join(parts)})"
    print(f"Import failed: {error.error_code}: {message}{location}", file=sys.stderr)


def _run_credential(args: argparse.Namespace) -> int:
    """Run one ``credential`` command and report its refusals.

    Incidental output is isolated *inside* ``run_credential_command`` - around the service call
    only - so the report itself is still written to the real stdout. Redirecting around the whole
    command would send the JSON document to stderr along with the noise.
    """
    json_output = bool(args.json_output)
    try:
        return run_credential_command(args)
    except CredentialInputError as error:
        _print_error(args, "configuration_error", error)
        return 2
    except Exception as error:  # noqa: BLE001 - safe top-level CLI boundary
        report_credential_failure(error, json_output=json_output)
        return 1


def _run_certificate(args: argparse.Namespace) -> int:
    """Run one ``certificate`` command under the same output and error rules."""
    json_output = bool(args.json_output)
    try:
        return run_certificate_command(args)
    except CredentialInputError as error:
        _print_error(args, "configuration_error", error)
        return 2
    except Exception as error:  # noqa: BLE001 - safe top-level CLI boundary
        report_credential_failure(error, json_output=json_output)
        return 1


def _run_secure_source(args: argparse.Namespace) -> int:
    """Run one single-datasource secure command, reporting failures with their stable code."""
    json_output = bool(args.json_output)
    try:
        return run_secure_command(args)
    except DatasourceConfigError:
        # A bad config file is a usage problem, reported as 2 by the shared handler.
        raise
    except Exception as error:  # noqa: BLE001 - safe top-level CLI boundary
        return report_catalog_failure(args, error, json_output=json_output)


def _print_error(args: argparse.Namespace, status: str, error: Exception) -> None:
    message = str(error)
    if getattr(args, "json_output", False):
        print(
            json.dumps(
                {
                    "status": status,
                    "error": {"type": type(error).__name__, "message": message},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print(f"{status.replace('_', ' ').title()}: {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        load_runtime_environment(args.env_file)
        if args.command == "doctor":
            with redirect_stdout(sys.stderr) if args.json_output else nullcontext():
                checks = inspect_environment()
            _print_doctor(checks, json_output=args.json_output)
            return 0 if doctor_passed(checks) else 1

        if args.command == SHELL_COMMAND:
            # The session runs commands through this same entry point, so it inherits the
            # one-shot parsing, exit codes and error rules instead of restating them.
            return run_shell_command(args)

        if args.command == ASK_COMMAND:
            return run_ask_command(args)

        if args.command == CREDENTIAL_COMMAND:
            return _run_credential(args)

        if args.command == CERTIFICATE_COMMAND:
            return _run_certificate(args)

        if args.command == "source":
            if args.source_command == EXCEL_IMPORT_COMMAND:
                return _run_excel_import(args)
            if args.source_command in CATALOG_COMMANDS:
                # ``list`` / ``show`` / ``scan-one`` project the catalog; they isolate their own
                # incidental output and own their error and exit-code reporting.
                return run_catalog_command(args)
            if args.source_command in SECURE_COMMANDS:
                return _run_secure_source(args)
            with redirect_stdout(sys.stderr) if args.json_output else nullcontext():
                # Capture incidental driver output; the structured report is emitted below.
                source_report = run_source(args)
            if isinstance(source_report, ConnectionTestReport):
                print_connection_report(source_report, json_output=args.json_output)
                return 0 if source_report.status == "passed" else 1
            print_scan_report(source_report, json_output=args.json_output)
            return 0 if source_report.passed else 1

        # ``ask --suite phase1`` and ``phase1`` deliberately converge here.
        with redirect_stdout(sys.stderr) if args.json_output else nullcontext():
            report = Phase1AcceptanceRunner().run(
                question_class=args.question_class,
                repeat=args.repeat,
                trace=not args.no_trace,
            )
        _print_acceptance(report, json_output=args.json_output)
        return 0 if report.status == "passed" else 1
    except (
        EnvironmentBootstrapError,
        AcceptanceConfigurationError,
        DatasourceConfigError,
        ScanRunConfigurationError,
    ) as error:
        _print_error(args, "configuration_error", error)
        return 2
    except EnvironmentBlockedError as error:
        _print_error(args, "environment_blocked", error)
        return 1
    except ExcelIngestionError as error:
        # An Excel refusal is an operation that ran and failed, not a usage or configuration error:
        # it exits 1 like any other failed operation, but keeps its own stable code and shape.
        _print_import_error(args, error)
        return 1
    except Exception as error:  # noqa: BLE001 - safe top-level CLI boundary
        # Unexpected exceptions expose only their type, never an arbitrary message that
        # could contain a provider credential.
        _print_error(args, "operation_failed", RuntimeError(type(error).__name__))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
