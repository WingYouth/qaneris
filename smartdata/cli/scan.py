"""Compatibility CLI delegating to the canonical multi-database scan runner."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from contextlib import nullcontext, redirect_stdout
from pathlib import Path

from smartdata.cli.config import DatasourceConfigError, load_datasource_requests
from smartdata.cli.source import print_connection_report, print_scan_report
from smartdata.common.environment import EnvironmentBootstrapError, load_runtime_environment
from smartdata.common.redaction import safe_error
from smartdata.scanning import MultiDatabaseScanRunner, ScanRunConfigurationError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartdata-scan",
        description="Compatibility entry point; prefer 'smartdata source'.",
    )
    parser.add_argument("--env-file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("test", "test datasource connections only"),
        ("run", "scan and publish configured datasources"),
    ):
        child = commands.add_parser(command, help=help_text)
        child.add_argument("--config", required=True, type=Path)
        child.add_argument("--env-file", type=Path, default=argparse.SUPPRESS)
        child.add_argument("--json", action="store_true", dest="json_output")
        if command == "run":
            child.add_argument("--catalog", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        load_runtime_environment(args.env_file)
        requests = load_datasource_requests(args.config.expanduser().resolve())
        runner = MultiDatabaseScanRunner()
        with redirect_stdout(sys.stderr) if args.json_output else nullcontext():
            if args.command == "test":
                report = runner.test_connections(requests)
            else:
                report = runner.scan(
                    requests,
                    config_source=str(args.config.expanduser().resolve()),
                    catalog_path=args.catalog,
                )
        if args.command == "test":
            print_connection_report(report, json_output=args.json_output)
            return 0 if report.status == "passed" else 1
        print_scan_report(report, json_output=args.json_output)
        return 0 if report.passed else 1
    except (
        EnvironmentBootstrapError,
        DatasourceConfigError,
        ScanRunConfigurationError,
    ) as error:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "status": "configuration_error",
                        "error": {"type": type(error).__name__, "message": str(error)},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - credential-safe compatibility boundary
        message = safe_error(error)
        if args.json_output:
            print(
                json.dumps(
                    {"status": "operation_failed", "error": message},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"Operation failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
