"""Compatibility entry point for the canonical Phase 1 acceptance runner.

Prefer ``qaneris acceptance phase1``. This module remains executable for existing
automation, but owns no scenarios, verdicts, setup, or reporting logic.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from contextlib import nullcontext, redirect_stdout
from pathlib import Path

from qaneris.acceptance.phase1 import Phase1AcceptanceRunner
from qaneris.common.environment import load_runtime_environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question-class", type=str.upper, choices=tuple("ABCDEF"))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--no-trace", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--report", type=Path, help="also copy report.json to this path")
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="deprecated; canonical run artifacts are always retained",
    )
    args = parser.parse_args()
    load_runtime_environment()
    with redirect_stdout(sys.stderr) if args.json_output else nullcontext():
        report = Phase1AcceptanceRunner().run(
            question_class=args.question_class,
            repeat=args.repeat,
            trace=not args.no_trace,
        )
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(report.artifact_paths["report_json"], args.report)
    if args.json_output:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        for item in report.results:
            label = "PASS" if item.status == "passed" else item.status.upper()
            print(f"[{label}] {item.code} {item.title}")
        print(f"{report.summary['passed']}/{report.summary['total']} passed")
        print(f"report: {report.artifact_paths['report_json']}")
    return 0 if report.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
