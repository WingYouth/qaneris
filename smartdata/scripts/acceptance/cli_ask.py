"""RS-CLI-01A acceptance: the installed ``smartdata ask`` command against real infrastructure.

Run it from the project root with a real graph backend and a real model endpoint configured
(``SMARTDATA_GRAPH_STORE=neo4j`` with the ``SMARTDATA_NEO4J_*`` credentials, and
``SMARTDATA_MODEL_BASE_URL`` / ``SMARTDATA_MODEL_API_KEY`` / ``SMARTDATA_MODEL_NAME``)::

    set -a && . ./.env && set +a
    python3 -m smartdata.scripts.acceptance.cli_ask

Unlike the EXCEL-01C acceptance, which drives the HTTP boundary, this one drives the **installed
console command** as a subprocess - never an in-process ``SmartDataService.ask()``:

    smartdata source import-excel orders.xlsx --json   ->  datasource_id
    governance registration (administrator seeds, resolved against the published graph)
    smartdata ask QUESTION --datasource <id> --json    ->  one pure JSON document
    smartdata ask QUESTION --datasource <id>           ->  human-readable report

It proves, on real infrastructure, that the command walks the existing trusted chain - intent,
retrieval, grounding, plan, validation, read-only execution - and that its human and JSON outputs
are stable product projections: pure JSON, correct aggregates, a safe ``display_command``, no
traceback, no private model output, and the conventional exit codes (0 verdict, 1 failure,
2 usage).

Business governance follows the EXCEL-01C finding: a physical field is not a governed metric or
dimension, so the fixture's governed semantic assets are registered explicitly through the
existing ``SemanticAssetBootstrap`` before any ask.

A model that cannot be reached at all is retried a bounded number of times; a real product
verdict (completed / clarification_required / failed) is never retried away.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smartdata.common.artifacts import artifact_directory, project_root
from smartdata.llm.config import resolve_model_profile
from smartdata.scripts.acceptance.excel_web_ask import (
    build_orders_workbook,
    fixture_sums,
    register_governance,
)
from smartdata.scripts.acceptance.summary import write_summary

WORKSPACE = "default"
DATASOURCE_NAME = "CLI Ask Orders"
QUESTION = "对 orders 表，按 region 分组汇总 amount 的总和。"
UNGOVERNED_QUESTION = "对 orders 表，按 region 分组汇总 profit 的总和。"
MISSING_DATASOURCE = "ds_does_not_exist"
MODEL_ERROR_CODE = "model_invocation_failed"

#: Bounded retries for the *only* non-verdict outcome: the provider could not be reached at all.
#: A completed / clarification / failed response is a product verdict and is never retried.
ASK_ATTEMPTS = 4


def datetime_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def console_command() -> Path:
    """The installed ``smartdata`` console script of the same interpreter that runs this module."""
    candidate = Path(sys.executable).parent / "smartdata"
    if not candidate.is_file():
        raise RuntimeError(f"installed console command not found: {candidate}")
    return candidate


def run_cli(
    command: Path,
    arguments: list[str],
    environment: dict[str, str],
    cwd: Path,
) -> tuple[int, str, str]:
    """Run one console command invocation with the run's environment."""
    completed = subprocess.run(
        [str(command), *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=900.0,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def pure_json(stdout: str) -> dict[str, Any] | None:
    """Parse stdout as exactly one JSON document, or ``None`` if it is not one."""
    try:
        document = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


#: The stages a completed, already-governed question must pass through, in this order. A run that
#: skips a stage, reorders one, or interleaves ``error`` / ``clarification_required`` is a
#: different verdict than the one this acceptance fixes.
EXPECTED_STREAM_EVENTS = (
    "accepted",
    "intent_ready",
    "retrieval_ready",
    "grounding_ready",
    "plan_ready",
    "query_ready",
    "execution_started",
    "result_ready",
    "done",
)

#: A JSONL line that carries any of these means the product projection failed, not that the run
#: was slow. They are matched against the parsed line's own text.
STREAM_FORBIDDEN_TOKENS = (
    "SMARTDATA_MODEL_API_KEY",
    "sk-",
    "BEGIN CERTIFICATE",
    "BEGIN PRIVATE KEY",
    "chain_of_thought",
    "reasoning_content",
    "Traceback",
)


def parse_jsonl(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse stdout as strict JSONL: one JSON object per non-empty line.

    Returns the parsed events and the lines that could not be parsed. A console banner, a driver
    notice or a progress spinner mixed into stdout is a failure here rather than something the
    parser is expected to tolerate - stdout belongs to ``AskEvent`` lines alone in this mode.
    Blank lines are skipped, not counted as events.
    """
    events: list[dict[str, Any]] = []
    unparsed: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            unparsed.append(line)
            continue
        if not isinstance(parsed, dict):
            unparsed.append(line)
            continue
        events.append(parsed)
    return events, unparsed


def stream_event_type(event: dict[str, Any]) -> str:
    """The event's stage name as the contract serializes it."""
    return str(event.get("event_type", ""))


def stream_order_violation(events: list[dict[str, Any]]) -> str:
    """Describe how the observed event order breaks the expected chain, or ``""`` when it does not.

    The required chain must appear as a subsequence in order, ``done`` must be last, and no
    failure or clarification event may appear on the fixed governed question.
    """
    observed = [stream_event_type(event) for event in events]
    interruptions = [
        name
        for name in observed
        if name in {"error", "clarification_required"}
    ]
    if interruptions:
        return f"unexpected event(s) on a governed question: {interruptions}"
    cursor = 0
    for name in observed:
        if cursor < len(EXPECTED_STREAM_EVENTS) and name == EXPECTED_STREAM_EVENTS[cursor]:
            cursor += 1
    if cursor != len(EXPECTED_STREAM_EVENTS):
        missing = list(EXPECTED_STREAM_EVENTS[cursor:])
        return f"missing or reordered stage(s): {missing} (observed {observed})"
    if not observed or observed[-1] != "done":
        return f"the last event is {observed[-1] if observed else '<none>'}, not done"
    return ""


def ask_once(
    command: Path,
    arguments: list[str],
    environment: dict[str, str],
    cwd: Path,
) -> tuple[int, str, str, int]:
    """One ask invocation, retried only while the provider is unreachable.

    ``model_invocation_failed`` is the gateway's own code for "no provider verdict happened".
    Anything else - including exit 1 with another code and every product verdict - returns as is.
    """
    attempts = 0
    while attempts < ASK_ATTEMPTS:
        attempts += 1
        code, stdout, stderr = run_cli(command, arguments, environment, cwd)
        if code == 1 and MODEL_ERROR_CODE in stdout + stderr:
            continue
        return code, stdout, stderr, attempts
    return code, stdout, stderr, attempts


def session(command: Path, workbook: Path, catalog_path: Path, cwd: Path) -> dict[str, Any]:
    """Drive the console command end to end and collect everything the checks inspect."""
    environment = {**os.environ, "SMARTDATA_CATALOG": str(catalog_path)}
    observed: dict[str, Any] = {}

    code, stdout, stderr = run_cli(
        command,
        [
            "source",
            "import-excel",
            str(workbook),
            "--name",
            DATASOURCE_NAME,
            "--workspace",
            WORKSPACE,
            "--json",
        ],
        environment,
        cwd,
    )
    observed["import"] = {
        "code": code,
        "stdout": stdout,
        "stderr": stderr,
        "document": pure_json(stdout) if code == 0 else None,
    }
    payload = observed["import"]["document"] or {}
    datasource_id = str(payload.get("datasource_id", ""))
    observed["datasource_id"] = datasource_id
    observed["scan_version"] = payload.get("scan_version")

    if datasource_id:
        # ---- the catalog surfaces over the freshly imported datasource ---------------------
        code, stdout, stderr = run_cli(
            command,
            ["source", "show", datasource_id, "--json"],
            environment,
            cwd,
        )
        observed["show"] = {
            "code": code,
            "stdout": stdout,
            "stderr": stderr,
            "document": pure_json(stdout) if code == 0 else None,
        }

        code, stdout, stderr = run_cli(
            command,
            ["source", "list", "--workspace", WORKSPACE, "--json"],
            environment,
            cwd,
        )
        observed["list"] = {
            "code": code,
            "stdout": stdout,
            "stderr": stderr,
            "document": pure_json(stdout) if code == 0 else None,
        }

        observed["governance"] = register_governance(catalog_path, datasource_id, WORKSPACE)

        json_arguments = [
            "ask",
            QUESTION,
            "--workspace",
            WORKSPACE,
            "--datasource",
            datasource_id,
            "--json",
        ]
        code, stdout, stderr, attempts = ask_once(command, json_arguments, environment, cwd)
        observed["ask_json"] = {
            "code": code,
            "stdout": stdout,
            "stderr": stderr,
            "attempts": attempts,
            "document": pure_json(stdout) if code == 0 else None,
        }
        document = observed["ask_json"]["document"] or {}
        observed["answer"] = document
        result = document.get("result") or {}
        observed["rows"] = {
            row.get("region"): row.get("sum_amount")
            for row in result.get("rows", [])
            if isinstance(row, dict)
        }
        observed["evidence"] = document.get("evidence") or {}
        observed["forbidden_hits"] = [
            token
            for token in FORBIDDEN_TOKENS
            if token in stdout + stderr
        ]

        code, stdout, stderr, attempts = ask_once(
            command,
            ["ask", QUESTION, "--workspace", WORKSPACE, "--datasource", datasource_id],
            environment,
            cwd,
        )
        observed["ask_human"] = {
            "code": code,
            "stdout": stdout,
            "stderr": stderr,
            "attempts": attempts,
        }

        code, stdout, stderr, attempts = ask_once(
            command,
            ["ask", UNGOVERNED_QUESTION, "--datasource", datasource_id, "--json"],
            environment,
            cwd,
        )
        observed["ungoverned"] = {
            "code": code,
            "stdout": stdout,
            "stderr": stderr,
            "document": pure_json(stdout) if code == 0 else None,
        }

        # ---- the same question, consumed as the unified event stream ----------------------
        code, stdout, stderr, attempts = ask_once(
            command,
            [
                "ask",
                QUESTION,
                "--workspace",
                WORKSPACE,
                "--datasource",
                datasource_id,
                "--stream",
                "--json",
            ],
            environment,
            cwd,
        )
        events, unparsed = parse_jsonl(stdout) if code == 0 else ([], [])
        result_event = next(
            (event for event in events if stream_event_type(event) == "result_ready"), None
        )
        query_event = next(
            (event for event in events if stream_event_type(event) == "query_ready"), None
        )
        payload = (result_event or {}).get("payload") or {}
        response = payload.get("response") or {}
        streamed_result = response.get("result") or {}
        streamed_evidence = response.get("evidence") or {}
        observed["stream"] = {
            "code": code,
            "stdout": stdout,
            "stderr": stderr,
            "attempts": attempts,
            "events": events,
            "unparsed_lines": unparsed,
            "event_types": [stream_event_type(event) for event in events],
            "order_violation": stream_order_violation(events) if code == 0 else "stream did not run",
            "result_event": result_event,
            "query_event": query_event,
            "response": response,
            "result": streamed_result,
            "evidence": streamed_evidence,
            "rows": {
                row.get("region"): row.get("sum_amount")
                for row in streamed_result.get("rows", [])
                if isinstance(row, dict)
            },
            "status": response.get("status"),
            "forbidden_hits": [
                token for token in STREAM_FORBIDDEN_TOKENS if token in stdout
            ],
        }

    code, stdout, stderr = run_cli(
        command,
        ["ask", QUESTION, "--datasource", MISSING_DATASOURCE],
        environment,
        cwd,
    )
    observed["missing_datasource"] = {"code": code, "stdout": stdout, "stderr": stderr}

    code, stdout, stderr = run_cli(
        command, ["ask", QUESTION, "--max-rows", "0"], environment, cwd
    )
    observed["max_rows_zero"] = {"code": code, "stdout": stdout, "stderr": stderr}

    code, stdout, stderr = run_cli(
        command, ["ask", QUESTION, "--workspace", "   "], environment, cwd
    )
    observed["blank_workspace"] = {"code": code, "stdout": stdout, "stderr": stderr}

    return observed


FORBIDDEN_TOKENS = (
    "sk-provider-secret",
    "SMARTDATA_MODEL_API_KEY",
    "Traceback (most recent call last)",
    "data.sqlite3",
    "manifest.json",
    "analysis",
)


def checks_for(observed: dict[str, Any]) -> dict[str, bool]:
    imported = observed.get("import", {})
    payload = imported.get("document") or {}
    ask_json = observed.get("ask_json", {})
    document = ask_json.get("document") or {}
    evidence = observed.get("evidence", {})
    result = document.get("result") or {}
    human = observed.get("ask_human", {})
    human_out = human.get("stdout", "")
    ungoverned = observed.get("ungoverned", {})
    missing = observed.get("missing_datasource", {})
    display = evidence.get("display_command", "")

    return {
        "Console command is the installed entry point": bool(observed),
        "Import via CLI exits zero": imported.get("code") == 0,
        "Import stdout is one pure JSON document": imported.get("document") is not None,
        "Import reports READY with verified publication": (
            payload.get("status") == "READY"
            and payload.get("neo4j_publication_verified") is True
        ),
        "Governance resolved against the published graph": bool(observed.get("governance")),
        "Ask --json exits zero": ask_json.get("code") == 0,
        "Ask --json stdout is one pure JSON document": ask_json.get("document") is not None,
        "Ask status is completed": document.get("status") == "completed",
        "Ask JSON carries the scoped datasource": (
            (result.get("datasource_id") == observed.get("datasource_id"))
            and (evidence.get("datasource_id") == observed.get("datasource_id"))
        ),
        "Ask JSON carries the snapshot scan version": (
            result.get("scan_version") == observed.get("scan_version")
            and evidence.get("scan_version") == observed.get("scan_version")
        ),
        "Aggregate equals the fixture exactly": (
            observed.get("rows") == fixture_sums()
            and result.get("row_count") == len(fixture_sums())
        ),
        "Evidence carries the safe display command": bool(display.strip()),
        "Display command is read-only": not any(
            word in display.upper()
            for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "PRAGMA")
        ),
        "JSON projection excludes private analysis": "analysis" not in document,
        "No secret or traceback in JSON output": not observed.get("forbidden_hits"),
        "Human output exits zero": human.get("code") == 0,
        "Human output reports completed status": "Status: completed" in human_out,
        "Human output reports rows and truncation": (
            "Rows: 2" in human_out and "Truncated: no" in human_out
        ),
        "Human output renders both regions": (
            "East" in human_out and "350.5" in human_out
            and "West" in human_out and "220.25" in human_out
        ),
        "Human output shows the display command": display in human_out,
        "Human output shows the plan digest": (
            "Objects: orders" in human_out and "Aggregates: sum(amount)" in human_out
        ),
        "Human output has no traceback": "Traceback" not in human_out + human.get("stderr", ""),
        "Ungoverned metric asks for clarification with exit zero": (
            ungoverned.get("code") == 0
            and (ungoverned.get("document") or {}).get("status") == "clarification_required"
        ),
        "Missing datasource exits one without a traceback": (
            missing.get("code") == 1
            and missing.get("stdout", "") == ""
            and "Traceback" not in missing.get("stderr", "")
        ),
        "Out-of-range --max-rows is a usage error": (
            observed.get("max_rows_zero", {}).get("code") == 2
        ),
        "Blank --workspace is a usage error": (
            observed.get("blank_workspace", {}).get("code") == 2
        ),
        # The stream must agree with the sync answer, so the sync rows are passed in rather than
        # recomputed: a divergence between the two modes is the point of these checks.
        **_stream_checks(
            observed,
            datasource_id=observed.get("datasource_id"),
            rows=observed.get("rows") or {},
        ),
        **_catalog_checks(observed, datasource_id=observed.get("datasource_id")),
    }


def _stream_checks(
    observed: dict[str, Any], *, datasource_id: Any, rows: dict[str, Any]
) -> dict[str, bool]:
    """The streaming verdicts: JSONL purity, event order, and agreement with the sync answer.

    Nothing here invents an event: the completed body is read from the ``result_ready`` event's own
    public ``response``, so a stream that never completed cannot be scored as if it had.
    """
    stream = observed.get("stream") or {}
    events = stream.get("events") or []
    result = stream.get("result") or {}
    evidence = stream.get("evidence") or {}
    query_event = stream.get("query_event") or {}
    query_payload = query_event.get("payload") or {}
    display_command = evidence.get("display_command", "")
    expected = fixture_sums()

    return {
        "Stream --json exits zero": stream.get("code") == 0,
        "Stream stdout is strict JSONL": bool(stream.get("stdout"))
        and not stream.get("unparsed_lines")
        and bool(events),
        "Stream emits no extra stdout noise": not stream.get("unparsed_lines"),
        "Stream event order is the accepted chain": stream.get("order_violation") == "",
        "Stream ends with done": bool(events)
        and stream.get("event_types", [])[-1:] == ["done"],
        "Stream sequence numbers are contiguous": [event.get("sequence") for event in events]
        == list(range(1, len(events) + 1)),
        "Stream carries one correlation id": len(
            {event.get("correlation_id") for event in events}
        )
        == 1,
        "Stream result status is completed": stream.get("status") == "completed",
        "Stream scopes the imported datasource": result.get("datasource_id") == datasource_id
        and evidence.get("datasource_id") == datasource_id,
        "Stream carries the imported scan version": result.get("scan_version")
        == observed.get("scan_version")
        and evidence.get("scan_version") == observed.get("scan_version"),
        "Stream result agrees with the sync answer": stream.get("rows") == rows
        and stream.get("rows") == expected
        and result.get("row_count") == len(expected),
        "Stream query event carries a safe display command": bool(
            str(query_payload.get("display_command", "")).strip()
        )
        and not any(
            word in str(query_payload.get("display_command", "")).upper()
            for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "PRAGMA")
        ),
        "Stream query event exposes no bound parameters": "parameters" not in query_payload,
        "Stream leaks no secret or private analysis": not stream.get("forbidden_hits"),
        "Sync answer carries a display command to compare": bool(str(display_command).strip()),
    }


def _catalog_checks(observed: dict[str, Any], *, datasource_id: Any) -> dict[str, bool]:
    """``source show`` / ``source list`` after a successful import."""
    show = observed.get("show") or {}
    show_document = show.get("document") or {}
    listing = observed.get("list") or {}
    list_document = listing.get("document") or {}
    entries = list_document.get("datasources") or []
    imported = [
        entry for entry in entries if isinstance(entry, dict) and entry.get("id") == datasource_id
    ]

    return {
        "source show exits zero": show.get("code") == 0,
        "source show stdout is one pure JSON document": show.get("document") is not None,
        "source show reports the imported identity": show_document.get("id") == datasource_id
        and show_document.get("status") == "ready"
        and show_document.get("scan_version") == observed.get("scan_version"),
        "source show reports a dataset": (
            isinstance(show_document.get("dataset_count"), int)
            and show_document.get("dataset_count", 0) >= 1
        ),
        "source show reports the workspace": show_document.get("workspace_id") == WORKSPACE,
        "source show hides connection material": not any(
            token in (show.get("stdout", "") + show.get("stderr", ""))
            for token in (
                "connection_profile",
                "data.sqlite3",
                "SecretReference",
                "secret_id",
                "password",
                "token",
                "certificate",
            )
        ),
        "source list exits zero": listing.get("code") == 0,
        "source list stdout is one pure JSON document": listing.get("document") is not None,
        "source list carries the imported datasource": len(imported) == 1,
        "source list reports the physical kind": bool(imported)
        and imported[0].get("kind") == "relational"
        and imported[0].get("driver") == "sqlite",
        "source list reports READY in the workspace": bool(imported)
        and imported[0].get("status") == "ready"
        and imported[0].get("workspace_id") == WORKSPACE,
        "source list hides connection material": not any(
            token in (listing.get("stdout", "") + listing.get("stderr", ""))
            for token in ("connection_profile", "SecretReference", "secret_id", "password", "token")
        ),
    }


def main() -> int:
    if resolve_model_profile() is None:
        print("[BLOCKED] no model endpoint configured: a real acceptance cannot be faked")
        return 1

    command = console_command()
    run_id = datetime_stamp()
    run_root = artifact_directory("acceptance") / f"cli-ask-{run_id}"
    run_root.mkdir(parents=True, exist_ok=False)
    workbook = build_orders_workbook(run_root / "orders.xlsx")
    catalog_path = run_root / "smartdata.db"
    cwd = project_root()

    observed: dict[str, Any] = {}
    session_error = ""
    try:
        observed = session(command, workbook, catalog_path, cwd)
    except Exception as error:  # noqa: BLE001 - the acceptance reports, it does not crash
        session_error = f"{type(error).__name__}: {error}"

    checks = checks_for(observed)
    checks["Session completed without an unexpected failure"] = not session_error

    for name, passed in checks.items():
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")

    write_summary(
        checks,
        stage="cli_ask",
        datasource_id=observed.get("datasource_id"),
        scan_version=observed.get("scan_version"),
        run_root=str(run_root),
    )

    if session_error:
        print()
        print("--- diagnostics ---")
        print(f"session error: {session_error}")
        print("--- end diagnostics ---")

    document = (observed.get("ask_json") or {}).get("document") or {}
    result = document.get("result") or {}
    print()
    print(f"Command         : {command}")
    print(f"Datasource      : {observed.get('datasource_id')}")
    print(f"Scan version    : {observed.get('scan_version')}")
    print(f"Question        : {QUESTION}")
    print(
        f"Ask --json      : status={document.get('status')} "
        f"(attempts={(observed.get('ask_json') or {}).get('attempts')})"
    )
    print(f"Display query   : {(observed.get('evidence') or {}).get('display_command')}")
    stream = observed.get("stream") or {}
    print(
        f"Ask --stream    : {len(stream.get('events') or [])} JSONL event(s) "
        f"(attempts={stream.get('attempts')}, "
        f"order={'ok' if stream.get('order_violation') == '' else stream.get('order_violation')})"
    )
    print(f"Expected rows   : {fixture_sums()}")
    print(f"Actual rows     : {observed.get('rows')} (row_count={result.get('row_count')})")
    print(
        "Model gateway   : "
        f"{os.getenv('SMARTDATA_MODEL_NAME', '<unset>')} @ {os.getenv('SMARTDATA_MODEL_BASE_URL', '<unset>')}"
    )
    print(f"Evidence        : {run_root}")
    failed = [name for name, passed in checks.items() if not passed]
    print(
        f"Result          : {'PASS' if not failed else 'FAIL'} "
        f"({len(checks) - len(failed)}/{len(checks)})"
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
