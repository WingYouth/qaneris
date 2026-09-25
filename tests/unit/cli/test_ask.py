"""CLI contract tests for ``smartdata ask`` (RS-CLI-01A).

These lock the interface contract only: argument wiring, the single ``SmartDataService.ask()``
call, the human and JSON output shapes per Ask status, exit codes, stdout/stderr isolation and
the absence of private model output. The Ask pipeline itself - intent, retrieval, grounding,
planning, validation and execution - is covered by the Core suites and is deliberately not
retested here.
"""

from __future__ import annotations

import inspect
import io
import json
from typing import Any
from unittest.mock import Mock, call

import pytest

from smartdata.application.service import SmartDataService
from smartdata.cli import ask as ask_cli
from smartdata.cli import main as cli
from smartdata.cli import source
from smartdata.common.errors import DatasourceNotFoundError
from smartdata.contracts.api import (
    AskClarification,
    AskRequest,
    AskResponse,
    AskStatus,
    ErrorDetail,
)
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
LONG_VALUE = "x" * 200

# A credential-shaped token that must never reach any output.
SECRET_TOKEN = "sk-provider-secret-token"


def field_ref(path: str) -> GroundedFieldRef:
    return GroundedFieldRef(
        datasource_id=DATASOURCE_ID, data_object_id=DATA_OBJECT_ID, field_path=path
    )


def completed_plan() -> GroundedQueryPlan:
    locator = GroundedDataObjectRef(
        datasource_id=DATASOURCE_ID, data_object_id=DATA_OBJECT_ID, name="orders"
    )
    return GroundedQueryPlan(
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
    )


def completed_result(**changes: Any) -> GroundedQueryResult:
    values: dict[str, Any] = {
        "plan_id": PLAN_ID,
        "datasource_id": DATASOURCE_ID,
        "query_language": QueryLanguage.SQL,
        "columns": ["region", "sum_amount"],
        "rows": [
            {"region": "East", "sum_amount": 350.5},
            {"region": "West", "sum_amount": 220.25},
        ],
        "row_count": 2,
        "truncated": False,
        "scan_version": 1,
    }
    values.update(changes)
    return GroundedQueryResult(**values)


def completed_evidence(**changes: Any) -> ExecutionEvidence:
    values: dict[str, Any] = {
        "plan_id": PLAN_ID,
        "datasource_id": DATASOURCE_ID,
        "scan_version": 1,
        "query_language": QueryLanguage.SQL,
        "display_command": DISPLAY_COMMAND,
        "row_count": 2,
        "truncated": False,
    }
    values.update(changes)
    return ExecutionEvidence(**values)


def completed_response(**changes: Any) -> AskResponse:
    values: dict[str, Any] = {
        "question": QUESTION,
        "status": AskStatus.COMPLETED,
        "answer": "已完成，结果为 2 行。",
        "plan": completed_plan(),
        "result": completed_result(),
        "evidence": completed_evidence(),
        "analysis": {"row_count": 2, "model_note": SECRET_TOKEN},
    }
    values.update(changes)
    return AskResponse(**values)


def clarification_response() -> AskResponse:
    return AskResponse(
        question=QUESTION,
        status=AskStatus.CLARIFICATION_REQUIRED,
        answer="销售额指哪个字段？",
        clarification=[
            AskClarification(question="销售额指哪个字段？", options=["实收销售额", "含税销售额"])
        ],
    )


def failed_response() -> AskResponse:
    return AskResponse(
        question=QUESTION,
        status=AskStatus.FAILED,
        answer="",
        error=ErrorDetail(code="intent_parsing_failed", message="模型未能解析该问题"),
    )


def installed(monkeypatch: pytest.MonkeyPatch, service: Any) -> Mock:
    constructor = Mock(return_value=service)
    monkeypatch.setattr(source, "SmartDataService", constructor)
    return constructor


def serving(monkeypatch: pytest.MonkeyPatch, response: AskResponse) -> Mock:
    ask = Mock(return_value=response)
    installed(monkeypatch, Mock(spec=SmartDataService, ask=ask))
    return ask


def failing(monkeypatch: pytest.MonkeyPatch, error: Exception) -> Mock:
    ask = Mock(side_effect=error)
    installed(monkeypatch, Mock(spec=SmartDataService, ask=ask))
    return ask


def command(*extra: str) -> list[str]:
    return ["ask", QUESTION, *extra]


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


# --------------------------------------------------------------------------------------------
# parser wiring
# --------------------------------------------------------------------------------------------


def test_the_question_reaches_the_contract_request(monkeypatch: pytest.MonkeyPatch) -> None:
    ask = serving(monkeypatch, completed_response())
    run(monkeypatch, command())
    request = ask.call_args.args[0]
    assert isinstance(request, AskRequest)
    assert request.question == QUESTION
    assert ask.call_count == 1


def test_workspace_and_datasource_reach_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    ask = serving(monkeypatch, completed_response())
    run(monkeypatch, command("--workspace", "ws1", "--datasource", DATASOURCE_ID))
    request = ask.call_args.args[0]
    assert request.workspace_id == "ws1"
    assert request.datasource_id == DATASOURCE_ID


def test_max_rows_reaches_the_request_and_defaults_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    ask = serving(monkeypatch, completed_response())
    run(monkeypatch, command("--max-rows", "5"))
    assert ask.call_args.args[0].max_rows == 5
    ask = serving(monkeypatch, completed_response())
    run(monkeypatch, command())
    assert ask.call_args.args[0].max_rows == 200
    assert ask.call_args.args[0].workspace_id == "default"
    assert ask.call_args.args[0].datasource_id is None


def test_one_command_is_exactly_one_ask_call(monkeypatch: pytest.MonkeyPatch) -> None:
    ask = serving(monkeypatch, completed_response())
    code, _, _ = run(monkeypatch, command())
    assert code == 0
    assert ask.call_args_list == [call(ask.call_args.args[0])]


# --------------------------------------------------------------------------------------------
# completed output
# --------------------------------------------------------------------------------------------


def test_human_completed_output_reports_the_public_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    serving(monkeypatch, completed_response())
    code, stdout, _ = run(monkeypatch, command())
    assert code == 0
    assert "Status: completed" in stdout
    assert "Datasource: " + DATASOURCE_ID in stdout
    assert "Scan version: 1" in stdout
    assert "Rows: 2" in stdout
    assert "Truncated: no" in stdout
    assert DISPLAY_COMMAND in stdout
    assert "Objects: orders" in stdout
    assert "Group by: region" in stdout
    assert "Aggregates: sum(amount) AS sum_amount" in stdout
    assert SECRET_TOKEN not in stdout


def test_human_completed_output_renders_the_result_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serving(monkeypatch, completed_response())
    _, stdout, _ = run(monkeypatch, command())
    assert "region" in stdout and "sum_amount" in stdout
    assert "East" in stdout and "350.5" in stdout
    assert "West" in stdout and "220.25" in stdout


def test_human_output_caps_cell_width_without_changing_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = completed_response(
        result=completed_result(
            columns=["region", "note"],
            rows=[{"region": "East", "note": LONG_VALUE}],
            row_count=1,
        ),
        evidence=completed_evidence(row_count=1),
    )
    serving(monkeypatch, response)
    _, stdout, _ = run(monkeypatch, command())
    assert LONG_VALUE not in stdout  # display only; the JSON projection keeps the real value


def test_json_completed_output_is_a_stable_product_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serving(monkeypatch, completed_response())
    code, stdout, stderr = run(monkeypatch, command("--json"))
    assert code == 0
    document = json.loads(stdout)
    assert set(document) == {
        "status",
        "question",
        "answer",
        "business_query",
        "clarification",
        "plan",
        "result",
        "evidence",
        "error",
    }
    assert document["status"] == "completed"
    assert document["evidence"]["display_command"] == DISPLAY_COMMAND
    assert document["result"]["row_count"] == 2
    assert document["result"]["scan_version"] == 1
    assert document["result"]["datasource_id"] == DATASOURCE_ID
    assert "analysis" not in document
    assert "recommended_questions" not in document
    assert SECRET_TOKEN not in stdout + stderr


def test_json_output_survives_incidental_pipeline_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ask = Mock(side_effect=lambda request: print("driver warning noise") or completed_response())
    installed(monkeypatch, Mock(spec=SmartDataService, ask=ask))
    code, stdout, stderr = run(monkeypatch, command("--json"))
    assert code == 0
    assert json.loads(stdout)["status"] == "completed"
    assert "driver warning noise" in stderr


# --------------------------------------------------------------------------------------------
# clarification
# --------------------------------------------------------------------------------------------


def test_human_clarification_outputs_the_public_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    serving(monkeypatch, clarification_response())
    code, stdout, _ = run(monkeypatch, command())
    assert code == 0
    assert "Clarification required" in stdout
    assert "销售额指哪个字段？" in stdout
    assert "实收销售额" in stdout and "含税销售额" in stdout


def test_json_clarification_keeps_the_status_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serving(monkeypatch, clarification_response())
    code, stdout, _ = run(monkeypatch, command("--json"))
    assert code == 0
    document = json.loads(stdout)
    assert document["status"] == "clarification_required"
    assert document["clarification"][0]["options"] == ["实收销售额", "含税销售额"]
    assert "analysis" not in document


# --------------------------------------------------------------------------------------------
# failed
# --------------------------------------------------------------------------------------------


def test_human_failed_output_stays_on_stderr_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serving(monkeypatch, failed_response())
    code, stdout, stderr = run(monkeypatch, command())
    assert code == 1
    assert stdout == ""
    assert "Status: failed" in stderr
    assert "intent_parsing_failed" in stderr
    assert "Traceback" not in stderr


def test_json_failed_output_is_a_pure_document_exiting_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serving(monkeypatch, failed_response())
    code, stdout, _ = run(monkeypatch, command("--json"))
    assert code == 1
    document = json.loads(stdout)
    assert document["status"] == "failed"
    assert document["error"]["code"] == "intent_parsing_failed"


def test_product_exception_reports_redacted_and_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    failing(monkeypatch, DatasourceNotFoundError("工作区 default 中不存在数据源 ds_missing"))
    code, stdout, stderr = run(monkeypatch, command())
    assert code == 1
    assert "Traceback" not in stdout + stderr
    assert "Ask failed" in stderr


def test_unexpected_exception_exposes_only_the_type(monkeypatch: pytest.MonkeyPatch) -> None:
    failing(monkeypatch, RuntimeError(f"unexpected {SECRET_TOKEN}"))
    code, stdout, stderr = run(monkeypatch, command("--json"))
    assert code == 1
    document = json.loads(stdout)
    assert document["error"]["type"] == "RuntimeError"
    assert SECRET_TOKEN not in stdout + stderr
    assert "Traceback" not in stdout + stderr


# --------------------------------------------------------------------------------------------
# usage errors
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        ["ask", QUESTION, "--workspace", "   "],
        ["ask", QUESTION, "--datasource", ""],
        ["ask", QUESTION, "--max-rows", "0"],
        ["ask", QUESTION, "--max-rows", "1001"],
        ["ask"],
    ],
)
def test_usage_errors_exit_two(arguments: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    installed(monkeypatch, Mock(spec=SmartDataService))
    code, _, _ = run(monkeypatch, arguments)
    assert code == 2


def test_max_rows_out_of_range_names_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    serving(monkeypatch, completed_response())
    code, _, stderr = run(monkeypatch, command("--max-rows", "5000"))
    assert code == 2
    # The allowed range lives in AskRequest; the CLI reports the contract's own violation.
    assert "max_rows" in stderr


# --------------------------------------------------------------------------------------------
# boundary hygiene
# --------------------------------------------------------------------------------------------


def test_the_ask_cli_never_reaches_past_the_application_contract() -> None:
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
    assert ".ask(" in module_source


def test_datasource_scope_errors_do_not_print_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing(monkeypatch, DatasourceNotFoundError("no such datasource"))
    code, stdout, stderr = run(monkeypatch, command())
    assert code == 1
    assert stdout == ""
    assert "Traceback" not in stderr
