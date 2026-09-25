"""Unit tests for the RS-EXCEL-02 acceptance harness's pure decision logic.

These cover the parts that decide a verdict from *observed text* - the strict JSONL parser, the
stream event-order validator, the combined status calculation and report redaction - so a harness
bug surfaces here instead of as a false PASS (or a false FAIL) against real Neo4j and a real model.

No real Neo4j, model gateway or Node process is started.
"""

from __future__ import annotations

import json

from qaneris.scripts.acceptance.cli_ask import (
    EXPECTED_STREAM_EVENTS,
    parse_jsonl,
    stream_order_violation,
)
from qaneris.scripts.acceptance.excel_product_regression import (
    BLOCKED,
    FAILED,
    PASSED,
    StageResult,
    build_report,
    combined_status,
    redacted,
)


def event(name: str, sequence: int = 1) -> str:
    return json.dumps(
        {
            "event_type": name,
            "sequence": sequence,
            "payload": {},
            "occurred_at": "2026-09-23T00:00:00Z",
            "correlation_id": "correlation",
        }
    )


def completed_stream() -> str:
    return "\n".join(
        event(name, index) for index, name in enumerate(EXPECTED_STREAM_EVENTS, start=1)
    )


# --------------------------------------------------------------------------- JSONL parsing


def test_parse_jsonl_reads_one_event_per_line() -> None:
    events, unparsed = parse_jsonl(completed_stream())

    assert [item["event_type"] for item in events] == list(EXPECTED_STREAM_EVENTS)
    assert unparsed == []


def test_parse_jsonl_skips_blank_lines_without_inventing_events() -> None:
    stdout = "\n".join([event("accepted"), "", "   ", event("done", 2), ""])

    events, unparsed = parse_jsonl(stdout)

    assert [item["event_type"] for item in events] == ["accepted", "done"]
    assert unparsed == []


def test_parse_jsonl_rejects_console_noise_mixed_into_stdout() -> None:
    stdout = "\n".join([event("accepted"), "booting pipeline...", event("done", 2)])

    events, unparsed = parse_jsonl(stdout)

    assert len(events) == 2
    assert unparsed == ["booting pipeline..."]


def test_parse_jsonl_rejects_a_non_object_line() -> None:
    events, unparsed = parse_jsonl("[1, 2, 3]")

    assert events == []
    assert unparsed == ["[1, 2, 3]"]


def test_parse_jsonl_does_not_split_a_pretty_printed_document() -> None:
    """A one-shot JSON document on stdout is not JSONL: every line after the first fails to parse."""
    document = json.dumps({"status": "completed", "result": {"row_count": 2}}, indent=2)

    events, unparsed = parse_jsonl(document)

    assert events == []
    assert unparsed


# --------------------------------------------------------------------------- event ordering


def test_event_order_accepts_the_full_completed_chain() -> None:
    events, _ = parse_jsonl(completed_stream())

    assert stream_order_violation(events) == ""


def test_event_order_rejects_a_missing_stage() -> None:
    names = [name for name in EXPECTED_STREAM_EVENTS if name != "grounding_ready"]
    events, _ = parse_jsonl("\n".join(event(name, i) for i, name in enumerate(names, start=1)))

    assert "grounding_ready" in stream_order_violation(events)


def test_event_order_rejects_reordered_stages() -> None:
    names = list(EXPECTED_STREAM_EVENTS)
    names[4], names[5] = names[5], names[4]
    events, _ = parse_jsonl("\n".join(event(name, i) for i, name in enumerate(names, start=1)))

    assert stream_order_violation(events) != ""


def test_event_order_rejects_a_done_that_is_not_last() -> None:
    stdout = "\n".join(
        [completed_stream(), event("result_ready", 99)]
    )
    events, _ = parse_jsonl(stdout)

    assert stream_order_violation(events) != ""


def test_event_order_rejects_an_error_on_a_governed_question() -> None:
    names = [*EXPECTED_STREAM_EVENTS[:-1], "error", "done"]
    events, _ = parse_jsonl("\n".join(event(name, i) for i, name in enumerate(names, start=1)))

    assert "error" in stream_order_violation(events)


def test_event_order_rejects_a_clarification_on_a_governed_question() -> None:
    names = [*EXPECTED_STREAM_EVENTS[:-1], "clarification_required", "done"]
    events, _ = parse_jsonl("\n".join(event(name, i) for i, name in enumerate(names, start=1)))

    assert "clarification_required" in stream_order_violation(events)


def test_event_order_rejects_an_empty_stream() -> None:
    assert stream_order_violation([]) != ""


# --------------------------------------------------------------------------- combined status


def passing(name: str) -> StageResult:
    return StageResult(name=name, status=PASSED)


def test_combined_status_passes_only_when_every_stage_passes() -> None:
    assert combined_status([passing("cli_import"), passing("cli_ask"), passing("http_web")]) == PASSED


def test_combined_status_fails_when_any_stage_fails() -> None:
    stages = [passing("cli_import"), StageResult(name="cli_ask", status=FAILED), passing("http_web")]

    assert combined_status(stages) == FAILED


def test_combined_status_reports_blocked_over_failed() -> None:
    stages = [
        StageResult(name="cli_import", status=BLOCKED, blocked_reason="no model endpoint configured"),
        StageResult(name="cli_ask", status=FAILED),
    ]

    assert combined_status(stages) == BLOCKED


def test_a_blocked_stage_never_becomes_a_pass() -> None:
    stages = [
        StageResult(name="cli_import", status=BLOCKED),
        passing("cli_ask"),
        passing("http_web"),
    ]

    assert combined_status(stages) != PASSED


def test_stage_result_is_not_passed_on_a_missing_summary() -> None:
    """A stage that exited zero but handed back no summary is not evidence of anything."""
    result = StageResult(name="cli_ask", status=FAILED, returncode=0)

    assert not result.passed
    assert combined_status([result]) == FAILED


# --------------------------------------------------------------------------- report shape


def test_report_records_the_commit_and_product_identity() -> None:
    stages = [
        StageResult(
            name="cli_import",
            status=PASSED,
            checks={"a": True},
            facts={"datasource_id": "ds_import", "scan_version": 1},
        ),
        StageResult(
            name="cli_ask",
            status=PASSED,
            checks={"b": True},
            facts={"datasource_id": "ds_ask", "scan_version": 3},
        ),
        StageResult(name="http_web", status=PASSED, checks={"c": True}),
    ]

    report = build_report(
        status=PASSED,
        commit="abc123",
        started_at="start",
        completed_at="end",
        stages=stages,
        run_root=__import__("pathlib").Path("/tmp/evidence"),
    )

    assert report["status"] == PASSED
    assert report["source_commit"] == "abc123"
    assert report["checks"] == {
        "cli_import": PASSED,
        "cli_ask": PASSED,
        "cli_stream": PASSED,
        "http_web": PASSED,
    }
    # The Ask stage's own isolated catalog is what the sync/stream answers belong to.
    assert report["datasource"] == {"id": "ds_ask", "scan_version": 3}
    # ``exit_code`` is recorded only when the stage actually ran.
    assert "exit_code" not in report["stages"]["cli_import"]
    assert report["stages"]["cli_ask"]["checks_passed"] == 1


def test_report_fails_closed_on_a_failing_stage() -> None:
    stages = [
        StageResult(name="cli_import", status=PASSED, checks={"a": True}),
        StageResult(
            name="cli_ask",
            status=FAILED,
            returncode=1,
            checks={"a": True, "b": False},
            failed_checks=["b"],
        ),
        StageResult(name="http_web", status=PASSED, checks={"c": True}),
    ]

    report = build_report(
        status=FAILED,
        commit="abc123",
        started_at="start",
        completed_at="end",
        stages=stages,
        run_root=__import__("pathlib").Path("/tmp/evidence"),
    )

    assert report["status"] == FAILED
    assert report["checks"]["cli_ask"] == FAILED
    assert report["checks"]["cli_stream"] == FAILED
    assert report["stages"]["cli_ask"]["failed_checks"] == ["b"]


# --------------------------------------------------------------------------- redaction


def test_redacted_removes_credential_environment_values(monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_NEO4J_PASSWORD", "super-secret-password")
    monkeypatch.setenv("QANERIS_MODEL_API_KEY", "sk-live-model-key-value")

    text = redacted("bolt://neo4j:super-secret-password@host and sk-live-model-key-value")

    assert "super-secret-password" not in text
    assert "sk-live-model-key-value" not in text
    assert text.count("<redacted>") == 2


def test_redacted_leaves_ordinary_text_alone(monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_NEO4J_PASSWORD", "super-secret-password")

    assert redacted("cli_import: passed") == "cli_import: passed"
