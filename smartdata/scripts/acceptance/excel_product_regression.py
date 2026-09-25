"""RS-EXCEL-02 acceptance: re-prove the Excel product path end to end, on real infrastructure.

Run it from the project root with a real graph backend, a real model endpoint and Node available::

    set -a && . ./.env && . ./.env.acceptance && set +a
    python3 -m smartdata.scripts.acceptance.excel_product_regression

This is a **regression acceptance**, not a new Excel feature. It does not implement a second Excel
path, a second Ask chain or a second Web flow. Its whole job is to run the three existing product
acceptances, in this fixed order, as real subprocesses:

    A. ``import_excel_cli``  - ``.xlsx`` -> real CLI import -> READY datasource -> Neo4j
    B. ``cli_ask``           - real sync Ask, real streaming Ask (strict JSONL), source show/list
    C. ``excel_web_ask``     - real multipart HTTP upload -> Ask -> real frontend modules via Node

and then decide one verdict from their own reported checks. The child scripts already own their
product evidence; this runner imports nothing from the ingestion core, never copies their logic, and
never fabricates a stage. Each child hands its verdicts back through
``SMARTDATA_ACCEPTANCE_SUMMARY`` as data, so a child that fails half a dozen checks is reported by
name rather than as a single opaque exit code.

An unavailable environment - no Neo4j, no model gateway, no Node - is reported as ``BLOCKED``. A
real product failure is reported as ``FAILED``. Neither is ever rounded up to ``PASS``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smartdata.common.artifacts import artifact_directory, project_root
from smartdata.graph import Neo4jConfig
from smartdata.llm.config import resolve_model_profile
from smartdata.scripts.acceptance.summary import ENVIRONMENT_VARIABLE

PASSED = "passed"
FAILED = "failed"
BLOCKED = "blocked"
SKIPPED = "skipped"

#: The three product paths, in the fixed order the card requires. Each entry is a module the
#: installed interpreter can run with ``-m``; none of them is re-implemented here.
STAGES = ("cli_import", "cli_ask", "http_web")

STAGE_MODULES: dict[str, str] = {
    "cli_import": "smartdata.scripts.acceptance.import_excel_cli",
    "cli_ask": "smartdata.scripts.acceptance.cli_ask",
    "http_web": "smartdata.scripts.acceptance.excel_web_ask",
}

#: A single stage drives a whole model round trip plus, for the Web stage, a Node subprocess. The
#: ceiling is generous for the same reason the child scripts use generous HTTP timeouts: a slow
#: provider must be reported as slow, and only a real hang should end the run.
STAGE_TIMEOUT_SECONDS = 3600.0

#: Substrings that mean "the environment could not be used", not "the product regressed". They are
#: the child scripts' own BLOCKED/FAIL wording for a missing backend or model endpoint.
BLOCKED_MARKERS = (
    "no model endpoint configured",
    "Neo4j is not configured",
    "SMARTDATA_GRAPH_STORE=null",
    "installed console command not found",
)

#: Never let a credential, a private key or a model API key reach the report.
REDACTION_MARKERS = ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "CREDENTIAL", "PRIVATE_KEY")


@dataclass
class StageResult:
    """One child acceptance's outcome, as the child itself reported it."""

    name: str
    status: str
    returncode: int | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    failed_checks: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    blocked_reason: str = ""
    stdout: str = ""
    stderr: str = ""

    @property
    def passed(self) -> bool:
        return self.status == PASSED

    def to_report(self) -> dict[str, Any]:
        report: dict[str, Any] = {"status": self.status}
        if self.returncode is not None:
            report["exit_code"] = self.returncode
        if self.blocked_reason:
            report["blocked_reason"] = self.blocked_reason
        if self.checks:
            report["checks_passed"] = sum(1 for passed in self.checks.values() if passed)
            report["checks_total"] = len(self.checks)
            report["failed_checks"] = self.failed_checks
        for key in ("datasource_id", "scan_version", "snapshot_id"):
            if key in self.facts:
                report[key] = self.facts[key]
        return report


def console_command() -> Path:
    """The installed ``smartdata`` console script of the interpreter running this module."""
    candidate = Path(sys.executable).parent / "smartdata"
    if not candidate.is_file():
        raise RuntimeError(f"installed console command not found: {candidate}")
    return candidate


def source_commit() -> str:
    """The commit actually checked out, never a hardcoded one."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root(),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def blocked_reason(module: str, stdout: str, stderr: str) -> str:
    """The environment reason a stage could not run, or ``""`` when the failure is a real one."""
    text = f"{stdout}\n{stderr}"
    for marker in BLOCKED_MARKERS:
        if marker in text:
            return marker
    if (
        module == STAGE_MODULES["http_web"]
        and "no such file or directory" in text.lower()
        and shutil.which("node") is None
    ):
        return "node runtime not available"
    return ""


def run_stage(name: str, run_root: Path) -> StageResult:
    """Run one child acceptance as a subprocess and collect its own verdicts."""
    summary_path = run_root / f"{name}-summary.json"
    environment = {**os.environ, ENVIRONMENT_VARIABLE: str(summary_path)}
    module = STAGE_MODULES[name]

    try:
        completed = subprocess.run(
            [sys.executable, "-m", module],
            cwd=project_root(),
            env=environment,
            capture_output=True,
            text=True,
            timeout=STAGE_TIMEOUT_SECONDS,
            check=False,
        )
        returncode: int | None = completed.returncode
        stdout, stderr = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as expired:
        returncode = None
        stdout = _as_text(expired.stdout)
        stderr = _as_text(expired.stderr) + f"\n[timeout] stage exceeded {STAGE_TIMEOUT_SECONDS}s"

    (run_root / f"{name}.stdout.log").write_text(stdout, encoding="utf-8", errors="replace")
    (run_root / f"{name}.stderr.log").write_text(stderr, encoding="utf-8", errors="replace")

    result = StageResult(name=name, status=FAILED, returncode=returncode, stdout=stdout, stderr=stderr)

    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = {}
        if isinstance(summary, dict):
            result.checks = {
                str(key): bool(value) for key, value in (summary.get("checks") or {}).items()
            }
            result.failed_checks = [str(item) for item in (summary.get("failed_checks") or [])]
            result.facts = {
                key: value
                for key, value in summary.items()
                if key not in {"checks", "failed_checks", "status"}
            }

    # A child states its own verdict; the runner never upgrades one. A missing or failed summary
    # alongside a non-zero exit is a failure with whatever evidence the child left behind.
    if returncode == 0 and result.checks and not result.failed_checks:
        result.status = PASSED
        return result

    reason = blocked_reason(module, stdout, stderr)
    if reason:
        result.status = BLOCKED
        result.blocked_reason = reason
    return result


def _as_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def combined_status(stages: list[StageResult]) -> str:
    """The one verdict: PASSED only when every stage passed; BLOCKED outranks FAILED.

    A blocked environment means the product was never given a chance to answer, so reporting FAILED
    would blame the product for an unfinished setup. The distinction is kept in the report either
    way - neither becomes PASSED.
    """
    if all(stage.passed for stage in stages):
        return PASSED
    if any(stage.status == BLOCKED for stage in stages):
        return BLOCKED
    return FAILED


def redacted(text: str) -> str:
    """Strip any credential-looking environment value from report text."""
    safe = text
    for name, value in os.environ.items():
        if (
            value
            and len(value) >= 4
            and any(marker in name.upper() for marker in REDACTION_MARKERS)
        ):
            safe = safe.replace(value, "<redacted>")
    return safe


def build_report(
    *,
    status: str,
    commit: str,
    started_at: str,
    completed_at: str,
    stages: list[StageResult],
    run_root: Path,
) -> dict[str, Any]:
    """The report's machine shape. Only product identity is published, never environment dumps."""
    by_name = {stage.name: stage for stage in stages}
    import_facts = by_name["cli_import"].facts if "cli_import" in by_name else {}
    ask_facts = by_name["cli_ask"].facts if "cli_ask" in by_name else {}

    checks: dict[str, str] = {
        name: by_name[name].status if name in by_name else SKIPPED for name in STAGES
    }
    # ``cli_stream`` is not a separate process: stage B proves the sync ask, the strict-JSONL
    # stream, source show/list and the sync/stream agreement in one run over one datasource, so
    # the stream's verdict is that stage's verdict - it is neither invented nor double-counted.
    checks["cli_stream"] = checks["cli_ask"]

    # Stage B imports its own datasource with its own isolated catalog, so its identity is the one
    # a reader should compare the streamed and sync answers against.
    datasource_id = ask_facts.get("datasource_id") or import_facts.get("datasource_id")
    scan_version = ask_facts.get("scan_version", import_facts.get("scan_version"))

    return {
        "status": status,
        "source_commit": commit,
        "started_at": started_at,
        "completed_at": completed_at,
        "checks": checks,
        "datasource": {"id": datasource_id, "scan_version": scan_version},
        "stages": {name: by_name[name].to_report() for name in STAGES if name in by_name},
        "evidence_directory": str(run_root),
    }


def render_markdown(report: dict[str, Any]) -> str:
    """A short human report: the verdict, then what each product path proved."""
    lines = [
        "# RS-EXCEL-02 — Excel Product-path Regression Acceptance",
        "",
        f"- Verdict: **{report['status'].upper()}**",
        f"- Source commit: `{report['source_commit']}`",
        f"- Started: {report['started_at']}",
        f"- Completed: {report['completed_at']}",
        (
            f"- Datasource: `{report['datasource']['id']}` "
            f"(scan_version={report['datasource']['scan_version']})"
        ),
        "",
        "## Product paths",
        "",
        "| Path | Status | Checks |",
        "| --- | --- | --- |",
    ]
    for name in ("cli_import", "cli_ask", "cli_stream", "http_web"):
        stage = report["stages"].get(
            {"cli_stream": "cli_ask"}.get(name, name), {}
        )
        total = stage.get("checks_total")
        passed = stage.get("checks_passed")
        checks = f"{passed}/{total}" if total else "-"
        lines.append(f"| {name} | {report['checks'].get(name, SKIPPED)} | {checks} |")

    failed_any = {
        name: stage.get("failed_checks") or []
        for name, stage in report["stages"].items()
        if stage.get("failed_checks")
    }
    if failed_any:
        lines.extend(["", "## Failed checks", ""])
        for name, failed in failed_any.items():
            lines.append(f"### {name}")
            lines.extend(f"- {item}" for item in failed)

    blocked = {
        name: stage.get("blocked_reason", "")
        for name, stage in report["stages"].items()
        if stage.get("blocked_reason")
    }
    if blocked:
        lines.extend(["", "## Blocked by the environment", ""])
        for name, reason in blocked.items():
            lines.append(f"- {name}: {reason}")

    lines.extend(["", f"Evidence: `{report['evidence_directory']}`", ""])
    return "\n".join(lines)


def preflight() -> str:
    """The environment reason this acceptance cannot run at all, or ``""`` when it can."""
    try:
        console_command()
    except RuntimeError as error:
        return str(error)
    if os.getenv("SMARTDATA_GRAPH_STORE", "neo4j").strip().lower() == "null":
        return "SMARTDATA_GRAPH_STORE=null: READY requires a verified publication"
    try:
        Neo4jConfig.from_environment()
    except ValueError as error:
        return f"Neo4j is not configured: {error}"
    if resolve_model_profile() is None:
        return "no model endpoint configured"
    return ""


def main() -> int:
    started_at = datetime.now(UTC).isoformat()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_root = artifact_directory("acceptance") / f"excel-product-regression-{run_id}"
    run_root.mkdir(parents=True, exist_ok=True)

    commit = source_commit()
    readiness = preflight()

    if readiness:
        # A missing backend is BLOCKED, not FAILED and never PASS: the product was not given a
        # chance to answer, and faking a backend to change that is exactly what this forbids.
        print(f"[BLOCKED] {readiness}")
        stages = [
            StageResult(name=name, status=BLOCKED, blocked_reason=readiness) for name in STAGES
        ]
    else:
        stages = []
        for name in STAGES:
            print(f"\n=== stage: {name} ===", flush=True)
            result = run_stage(name, run_root)
            stages.append(result)
            print(
                f"--- {name}: {result.status.upper()} "
                f"({len(result.checks) - len(result.failed_checks)}/{len(result.checks)} checks) ---",
                flush=True,
            )
            if result.status != PASSED:
                # The card requires the runner to stop naming further stages as passed and to
                # report which stage failed. It does not silently continue to a green verdict.
                print(
                    f"    stage '{name}' did not pass; remaining stages are not treated as proof.",
                    flush=True,
                )

    status = combined_status(stages)
    report = build_report(
        status=status,
        commit=commit,
        started_at=started_at,
        completed_at=datetime.now(UTC).isoformat(),
        stages=stages,
        run_root=run_root,
    )

    report = json.loads(redacted(json.dumps(report, ensure_ascii=False)))
    (run_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_root / "report.md").write_text(redacted(render_markdown(report)), encoding="utf-8")

    print()
    print("================ RS-EXCEL-02 ================")
    for name in ("cli_import", "cli_ask", "cli_stream", "http_web"):
        print(f"{name:<16}: {report['checks'].get(name, SKIPPED)}")
    print(f"source_commit   : {report['source_commit']}")
    print(f"datasource      : {report['datasource']['id']} "
          f"(scan_version={report['datasource']['scan_version']})")
    print(f"verdict         : {status.upper()}")
    print(f"evidence        : {run_root}")
    print(f"report          : {run_root / 'report.json'}")
    return 0 if status == PASSED else 1


if __name__ == "__main__":
    raise SystemExit(main())
