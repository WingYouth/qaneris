"""CLI contract tests for ``smartdata ask --stream`` (RS-CLI-01B).

These lock the interface contract of the streaming modes: that the CLI consumes the unified
``AskEvent`` contract from ``SmartDataService.ask_stream()``, that human mode prints only stages the
pipeline really reported, that ``--stream --json`` writes strict JSONL on stdout and nothing else,
that ``done`` stays last, and that the exit codes match the existing CLI convention.

The Ask pipeline itself is covered by the Core suites. The events here are built directly from the
published contract, so these tests exercise exactly what the CLI is allowed to receive.
"""

from __future__ import annotations

import inspect
import io
import json
from typing import Any
from unittest.mock import Mock

import pytest

from smartdata.application.service import SmartDataService
from smartdata.cli import ask as ask_cli
from smartdata.cli import main as cli
from smartdata.cli import source
from smartdata.common.errors import DatasourceNotFoundError
from smartdata.contracts.api import AskRequest, AskResponse, AskStatus, ErrorDetail
from smartdata.contracts.ask_events import AskEvent, AskEventType
from smartdata.contracts.query import (
    AggregateFunction,
    ExecutionEvidence,
    GroundedDataObjectRef,
    GroundedFieldRef,
    GroundedQueryPlan,
    GroundedQueryResult,
    PlanAggregate,
    QueryLanguage,
    QueryResultType,
)

DATASOURCE_ID = "ds_ff606cfffe96"
DATA_OBJECT_ID = "sha_1f8ac10f23ff5a744"
PLAN_ID = "plan_9e107d9d372b"
QUESTION = "对 orders 表，按 region 分组汇总 amount 的总和。"
DISPLAY_COMMAND = 'SELECT t0."region", SUM(t0."amount") AS "sum_amount" FROM "orders" AS t0'
CORRELATION_ID = "corr-2f1c9a"

# Credential-shaped material that must never reach any output, on any stream.
SECRET_TOKEN = "sk-provider-secret-token"
SECRET_ASSIGNMENT = "password=hunter2-super-secret"


def field_ref(path: str) -> GroundedFieldRef:
    return GroundedFieldRef(
        datasource_id=DATASOURCE_ID, data_object_id=DATA_OBJECT_ID, field_path=path
    )


def completed_response(**changes: Any) -> AskResponse:
    locator = GroundedDataObjectRef(
        datasource_id=DATASOURCE_ID, data_object_id=DATA_OBJECT_ID, name="orders"
    )
    values: dict[str, Any] = {
        "question": QUESTION,
        "status": AskStatus.COMPLETED,
        "answer": "已完成，结果为 2 行。",
        "plan": GroundedQueryPlan(
            plan_id=PLAN_ID,
            workspace_id="default",
            datasource_id=DATASOURCE_ID,
            data_object_ids=[DATA_OBJECT_ID],
            data_objects={DATA_OBJECT_ID: locator},
            aggregates=[
                PlanAggregate(
                    function=AggregateFunction.SUM, field=field_ref("amount"), alias="sum_amount"
                )
            ],
            selected_fields=[field_ref("region"), field_ref("amount")],
            group_by=[field_ref("region")],
            expected_result_type=QueryResultType.TABULAR,
        ),
        "result": GroundedQueryResult(
            plan_id=PLAN_ID,
            datasource_id=DATASOURCE_ID,
            query_language=QueryLanguage.SQL,
            columns=["region", "sum_amount"],
            rows=[{"region": "East", "sum_amount": 350.5}],
            row_count=1,
            truncated=False,
            scan_version=1,
        ),
        "evidence": ExecutionEvidence(
            plan_id=PLAN_ID,
            datasource_id=DATASOURCE_ID,
            scan_version=1,
            query_language=QueryLanguage.SQL,
            display_command=DISPLAY_COMMAND,
            row_count=1,
            truncated=False,
        ),
    }
    values.update(changes)
    return AskResponse(**values)


def event(event_type: AskEventType, sequence: int, payload: dict[str, Any]) -> AskEvent:
    return AskEvent(
        event_type=event_type,
        sequence=sequence,
        payload=payload,
        correlation_id=CORRELATION_ID,
    )


def completed_stream(response: AskResponse) -> list[AskEvent]:
    """The event sequence the application service sends for one successful run."""
    return [
        event(AskEventType.ACCEPTED, 1, {"stage": "accepted", "workspace_id": "default"}),
        event(AskEventType.INTENT_READY, 2, {"stage": "intent", "datasource_id": DATASOURCE_ID}),
        event(AskEventType.RETRIEVAL_READY, 3, {"stage": "retrieval", "candidate_count": 4}),
        event(AskEventType.GROUNDING_READY, 4, {"stage": "grounding", "is_executable": True}),
        event(AskEventType.PLAN_READY, 5, {"stage": "plan", "plan": {"plan_id": PLAN_ID}}),
        event(
            AskEventType.QUERY_READY,
            6,
            {"stage": "query", "datasource_id": DATASOURCE_ID, "query_language": "sql"},
        ),
        event(
            AskEventType.EXECUTION_STARTED,
            7,
            {"stage": "execution", "datasource_id": DATASOURCE_ID, "scan_version": 1},
        ),
        event(
            AskEventType.RESULT_READY,
            8,
            {
                "stage": "result",
                "status": AskStatus.COMPLETED.value,
                "row_count": 1,
                "truncated": False,
                "response": response.model_dump(mode="json"),
            },
        ),
        event(
            AskEventType.DONE, 9, {"stage": "done", "status": AskStatus.COMPLETED.value}
        ),
    ]


def clarification_stream(*, stage: str, options: list[str]) -> list[AskEvent]:
    """A clarification raised by the intent stage or by the grounding stage.

    Both are normal product results: the pipeline ends with ``clarification_required`` then
    ``done``, and sends no ``error`` event. An intent-stage clarification returns before retrieval,
    so it deliberately carries no retrieval or grounding event either.
    """
    clarification = {"question": "销售额指哪个字段？", "options": options}
    if stage == "intent":
        return [
            event(AskEventType.ACCEPTED, 1, {"stage": "accepted", "workspace_id": "default"}),
            event(AskEventType.INTENT_READY, 2, {"stage": "intent"}),
            event(
                AskEventType.CLARIFICATION_REQUIRED,
                3,
                {
                    "stage": "clarification",
                    "status": AskStatus.CLARIFICATION_REQUIRED.value,
                    "clarification": [clarification],
                },
            ),
            event(
                AskEventType.DONE,
                4,
                {"stage": "done", "status": AskStatus.CLARIFICATION_REQUIRED.value},
            ),
        ]
    return [
        event(AskEventType.ACCEPTED, 1, {"stage": "accepted", "workspace_id": "default"}),
        event(AskEventType.INTENT_READY, 2, {"stage": "intent"}),
        event(AskEventType.RETRIEVAL_READY, 3, {"stage": "retrieval", "candidate_count": 2}),
        event(AskEventType.GROUNDING_READY, 4, {"stage": "grounding", "is_executable": False}),
        event(
            AskEventType.CLARIFICATION_REQUIRED,
            5,
            {
                "stage": "clarification",
                "status": AskStatus.CLARIFICATION_REQUIRED.value,
                "clarification": [clarification],
            },
        ),
        event(
            AskEventType.DONE,
            6,
            {"stage": "done", "status": AskStatus.CLARIFICATION_REQUIRED.value},
        ),
    ]


def error_stream() -> list[AskEvent]:
    """The pair the service sends for a failed run: ``error`` then ``done``."""
    detail = ErrorDetail(code="graph_unavailable", message="图后端未配置")
    return [
        event(AskEventType.ACCEPTED, 1, {"stage": "accepted", "workspace_id": "default"}),
        event(AskEventType.INTENT_READY, 2, {"stage": "intent"}),
        event(
            AskEventType.ERROR,
            3,
            {"stage": "error", "error": detail.model_dump(mode="json")},
        ),
        event(AskEventType.DONE, 4, {"stage": "done", "status": AskStatus.FAILED.value}),
    ]


def installed(monkeypatch: pytest.MonkeyPatch, service: Any) -> Mock:
    constructor = Mock(return_value=service)
    monkeypatch.setattr(source, "SmartDataService", constructor)
    return constructor


def streaming(monkeypatch: pytest.MonkeyPatch, events: list[AskEvent]) -> Mock:
    ask_stream = Mock(return_value=iter(events))
    installed(monkeypatch, Mock(spec=SmartDataService, ask_stream=ask_stream))
    return ask_stream


def run(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)
    try:
        code = cli.main(argv)
    except SystemExit as error:  # argparse usage errors
        code = error.code if isinstance(error.code, int) else 1
    return code, stdout.getvalue(), stderr.getvalue()


def command(*extra: str) -> list[str]:
    return ["ask", QUESTION, "--stream", *extra]


def jsonl(stdout: str) -> list[dict[str, Any]]:
    """Parse stdout as JSONL, failing if any line is not exactly one JSON document."""
    lines = stdout.splitlines()
    assert lines, "the JSONL stream is empty"
    return [json.loads(line) for line in lines]


# --------------------------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------------------------


def test_the_question_reaches_the_contract_request(monkeypatch: pytest.MonkeyPatch) -> None:
    ask_stream = streaming(monkeypatch, completed_stream(completed_response()))
    run(monkeypatch, command("--workspace", "ws1", "--datasource", DATASOURCE_ID))
    request = ask_stream.call_args.args[0]
    assert isinstance(request, AskRequest)
    assert request.question == QUESTION
    assert request.workspace_id == "ws1"
    assert request.datasource_id == DATASOURCE_ID
    assert ask_stream.call_count == 1


def test_streaming_never_calls_the_synchronous_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ask = Mock(side_effect=AssertionError("streaming must not call the synchronous ask"))
    ask_stream = Mock(return_value=iter(completed_stream(completed_response())))
    installed(monkeypatch, Mock(spec=SmartDataService, ask=ask, ask_stream=ask_stream))

    assert run(monkeypatch, command())[0] == 0
    ask.assert_not_called()


def test_sync_mode_still_never_calls_the_streaming_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ask = Mock(return_value=completed_response())
    ask_stream = Mock(side_effect=AssertionError("sync must not consume the stream"))
    installed(monkeypatch, Mock(spec=SmartDataService, ask=ask, ask_stream=ask_stream))

    assert run(monkeypatch, ["ask", QUESTION])[0] == 0
    ask_stream.assert_not_called()


# --------------------------------------------------------------------------------------------
# human stream
# --------------------------------------------------------------------------------------------


def test_human_stream_reports_the_stages_the_pipeline_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streaming(monkeypatch, completed_stream(completed_response()))
    code, stdout, stderr = run(monkeypatch, command())
    assert code == 0
    stages = [
        "[accepted] Request accepted",
        "[intent] Intent ready",
        "[retrieval] Retrieval ready",
        "[grounding] Grounding ready",
        "[plan] Query plan ready",
        "[query] Query ready",
        "[execution] Executing",
        "[result] Result ready",
        "[done] Completed",
    ]
    positions = [stdout.index(stage) for stage in stages]
    assert positions == sorted(positions)
    assert stderr == ""


def test_human_stream_reports_only_the_events_it_received(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A run that fails during retrieval must not announce planning, query or result stages.
    detail = ErrorDetail(code="graph_unavailable", message="图后端未配置")
    events = [
        event(AskEventType.ACCEPTED, 1, {"stage": "accepted"}),
        event(AskEventType.ERROR, 2, {"stage": "error", "error": detail.model_dump(mode="json")}),
        event(AskEventType.DONE, 3, {"stage": "done", "status": AskStatus.FAILED.value}),
    ]
    streaming(monkeypatch, events)
    _, stdout, _ = run(monkeypatch, command())
    for absent in ("[plan]", "[query]", "[execution]", "[result]", "[retrieval]", "[grounding]"):
        assert absent not in stdout


def test_human_stream_renders_the_completed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    streaming(monkeypatch, completed_stream(completed_response()))
    _, stdout, _ = run(monkeypatch, command())
    assert "Answer: 已完成，结果为 2 行。" in stdout
    assert "region" in stdout and "sum_amount" in stdout
    assert "East" in stdout and "350.5" in stdout
    assert DISPLAY_COMMAND in stdout


def test_human_stream_reports_the_public_clarification_exiting_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streaming(monkeypatch, clarification_stream(stage="intent", options=["实收销售额", "含税销售额"]))
    code, stdout, _ = run(monkeypatch, command())
    assert code == 0
    assert "[clarification] Clarification required" in stdout
    assert "Question: 销售额指哪个字段？" in stdout
    assert "实收销售额" in stdout and "含税销售额" in stdout
    assert stdout.rstrip().endswith("[done] Clarification required")


def test_human_stream_reports_a_grounding_clarification_exiting_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streaming(monkeypatch, clarification_stream(stage="grounding", options=["下单日期", "支付日期"]))
    code, stdout, _ = run(monkeypatch, command())
    assert code == 0
    assert "[clarification] Clarification required" in stdout
    assert "下单日期" in stdout and "支付日期" in stdout
    assert stdout.rstrip().endswith("[done] Clarification required")


def test_human_stream_error_exits_one_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streaming(monkeypatch, error_stream())
    code, stdout, stderr = run(monkeypatch, command())
    assert code == 1
    assert "[error] Ask failed: graph_unavailable: 图后端未配置" in stdout
    assert stdout.rstrip().endswith("[done] Failed")
    assert "Traceback" not in stdout + stderr


def test_a_stream_that_breaks_outside_the_contract_emits_no_fabricated_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stages already emitted stay emitted, and the gap is never covered by an invented event."""

    def broken(request: AskRequest) -> Any:
        yield event(AskEventType.ACCEPTED, 1, {"stage": "accepted"})
        raise RuntimeError(f"provider stream died {SECRET_TOKEN}")

    installed(monkeypatch, Mock(spec=SmartDataService, ask_stream=Mock(side_effect=broken)))
    code, stdout, stderr = run(monkeypatch, command())
    assert code == 1
    # The first stage was already printed, so the CLI emits as it consumes rather than at exit.
    assert "[accepted] Request accepted" in stdout
    # No ``done`` was sent, so none is printed.
    assert "[done]" not in stdout
    assert "Ask failed" in stderr
    assert SECRET_TOKEN not in stdout + stderr
    assert "Traceback" not in stdout + stderr


# --------------------------------------------------------------------------------------------
# JSONL stream
# --------------------------------------------------------------------------------------------


def test_jsonl_stdout_is_one_contract_event_per_line(monkeypatch: pytest.MonkeyPatch) -> None:
    streaming(monkeypatch, completed_stream(completed_response()))
    code, stdout, stderr = run(monkeypatch, command("--json"))
    assert code == 0
    assert stderr == ""
    documents = jsonl(stdout)
    assert [item["event_type"] for item in documents] == [
        "accepted",
        "intent_ready",
        "retrieval_ready",
        "grounding_ready",
        "plan_ready",
        "query_ready",
        "execution_started",
        "result_ready",
        "done",
    ]
    # Each line is the contract's own event, with no wrapper and no CLI-only schema.
    for document in documents:
        assert set(document) == {
            "event_type",
            "sequence",
            "payload",
            "occurred_at",
            "correlation_id",
        }


def test_jsonl_keeps_sequence_order_done_last_and_the_shared_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streaming(monkeypatch, completed_stream(completed_response()))
    _, stdout, _ = run(monkeypatch, command("--json"))
    documents = jsonl(stdout)
    assert [item["sequence"] for item in documents] == list(range(1, len(documents) + 1))
    assert documents[-1]["event_type"] == "done"
    assert {item["correlation_id"] for item in documents} == {CORRELATION_ID}


def test_jsonl_error_stream_ends_with_done_and_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streaming(monkeypatch, error_stream())
    code, stdout, _ = run(monkeypatch, command("--json"))
    assert code == 1
    documents = jsonl(stdout)
    assert [item["event_type"] for item in documents] == [
        "accepted",
        "intent_ready",
        "error",
        "done",
    ]
    assert documents[-1]["payload"]["status"] == "failed"


def test_jsonl_clarification_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    streaming(monkeypatch, clarification_stream(stage="grounding", options=["下单日期"]))
    code, stdout, _ = run(monkeypatch, command("--json"))
    assert code == 0
    documents = jsonl(stdout)
    assert documents[-2]["event_type"] == "clarification_required"
    assert documents[-1]["payload"]["status"] == "clarification_required"


def test_jsonl_stdout_stays_pure_when_the_pipeline_prints_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def noisy(request: AskRequest) -> Any:
        print("driver warning noise")
        yield event(AskEventType.ACCEPTED, 1, {"stage": "accepted"})
        yield event(
            AskEventType.DONE, 2, {"stage": "done", "status": AskStatus.COMPLETED.value}
        )
        print("provider debug noise")

    installed(monkeypatch, Mock(spec=SmartDataService, ask_stream=Mock(side_effect=noisy)))
    code, stdout, stderr = run(monkeypatch, command("--json"))
    assert code == 0
    documents = jsonl(stdout)  # every stdout line is a complete JSON document
    assert [item["event_type"] for item in documents] == ["accepted", "done"]
    assert "driver warning noise" in stderr
    assert "provider debug noise" in stderr


def test_jsonl_drops_private_reasoning_and_redacts_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        event(
            AskEventType.ACCEPTED,
            1,
            {
                "stage": "accepted",
                "reasoning": f"private chain of thought mentioning {SECRET_TOKEN}",
                "note": SECRET_ASSIGNMENT,
            },
        ),
        event(
            AskEventType.DONE, 2, {"stage": "done", "status": AskStatus.COMPLETED.value}
        ),
    ]
    streaming(monkeypatch, events)
    _, stdout, stderr = run(monkeypatch, command("--json"))
    assert "reasoning" not in stdout
    assert "chain of thought" not in stdout
    assert SECRET_TOKEN not in stdout + stderr
    assert "hunter2-super-secret" not in stdout + stderr
    assert jsonl(stdout)[0]["payload"]["note"] == "password=<redacted>"


def test_human_stream_never_prints_private_reasoning_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        event(
            AskEventType.ACCEPTED,
            1,
            {
                "stage": "accepted",
                "reasoning": "private chain of thought",
                "api_key": SECRET_TOKEN,
            },
        ),
        event(
            AskEventType.PLAN_READY,
            2,
            {"stage": "plan", "plan": {"plan_id": PLAN_ID}, "connection_string": SECRET_TOKEN},
        ),
        event(
            AskEventType.DONE, 3, {"stage": "done", "status": AskStatus.COMPLETED.value}
        ),
    ]
    streaming(monkeypatch, events)
    _, stdout, stderr = run(monkeypatch, command())
    assert "private chain of thought" not in stdout + stderr
    assert SECRET_TOKEN not in stdout + stderr
    assert "<redacted>" not in stdout  # the CLI never prints the placeholder as a stage fact


# --------------------------------------------------------------------------------------------
# usage errors
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        ["ask", QUESTION, "--stream", "--max-rows", "0"],
        ["ask", QUESTION, "--stream", "--workspace", "   "],
        ["ask", QUESTION, "--stream", "--datasource", ""],
    ],
)
def test_stream_usage_errors_exit_two(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    installed(monkeypatch, Mock(spec=SmartDataService))
    code, _, _ = run(monkeypatch, arguments)
    assert code == 2


def test_stream_jsonl_usage_error_keeps_stdout_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    installed(monkeypatch, Mock(spec=SmartDataService))
    code, stdout, stderr = run(monkeypatch, command("--json", "--max-rows", "5000"))
    assert code == 2
    # No event exists yet, so stdout stays empty and the document is reported on stderr.
    assert stdout == ""
    assert json.loads(stderr)["status"] == "usage_error"


# --------------------------------------------------------------------------------------------
# product failures
# --------------------------------------------------------------------------------------------


def test_product_exception_before_the_stream_is_reported_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed(
        monkeypatch,
        Mock(spec=SmartDataService, ask_stream=Mock(side_effect=DatasourceNotFoundError("gone"))),
    )
    code, stdout, stderr = run(monkeypatch, command())
    assert code == 1
    assert stdout == ""
    assert "Ask failed" in stderr
    assert "Traceback" not in stdout + stderr


# --------------------------------------------------------------------------------------------
# boundary hygiene
# --------------------------------------------------------------------------------------------


def test_the_ask_cli_still_never_reaches_past_the_application_contract() -> None:
    module_source = inspect.getsource(ask_cli)
    forbidden = (
        "IntentParser",
        "SemanticRetriever",
        "Grounded",
        "Grounder",
        "Planner(",
        "Compiler",
        "create_adapter",
        "GraphStore",
        "neo4j",
        "ModelGateway",
        "model_dump()",  # the projection is an explicit field selection, not a raw dump
    )
    for name in forbidden:
        assert name not in module_source, name
    assert "application_service" in module_source
    assert "ask_stream" in module_source
    assert "AskEvent" in module_source
