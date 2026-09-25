from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.common.errors import QueryExecutionError
from qaneris.contracts import (
    AskEvent,
    AskEventType,
    AskRequest,
    AskResponse,
    BusinessQuery,
    ExecutionEvidence,
    GroundedExecution,
    GroundedQueryPlan,
    GroundedQueryResult,
    PlanAggregate,
    QueryLanguage,
    QueryResultType,
)
from qaneris.contracts.semantic import AggregateFunction, SemanticAssetType
from qaneris.semantic import ClarificationOption, ClarificationRequest


class IntentModel:
    def __init__(self, query: BusinessQuery):
        self.query = query

    def parse_business_query(self, question: str, rule_facts: dict) -> BusinessQuery:
        return self.query

    def plan_query(self, *args, **kwargs):
        raise AssertionError("intent model must not plan queries")

    def answer_question(self, *args, **kwargs):
        raise AssertionError("answer model unavailable")


def business_query(**changes) -> BusinessQuery:
    values = {
        "question": "按地区看销售额",
        "objective": "lookup",
        "metrics": ["销售额"],
        "dimensions": ["地区"],
    }
    values.update(changes)
    return BusinessQuery(**values)


def grounded_plan() -> GroundedQueryPlan:
    return GroundedQueryPlan(
        plan_id="plan-stream",
        workspace_id="workspace-a",
        datasource_id="ds-sales",
        data_object_ids=["orders"],
        data_objects={
            "orders": {
                "datasource_id": "ds-sales",
                "data_object_id": "orders",
                "name": "orders",
            }
        },
        scan_version=7,
        aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count_rows")],
        expected_result_type=QueryResultType.SCALAR,
    )


def grounded_execution() -> GroundedExecution:
    return GroundedExecution(
        result=GroundedQueryResult(
            plan_id="plan-stream",
            datasource_id="ds-sales",
            query_language=QueryLanguage.SQL,
            columns=["count_rows"],
            rows=[{"count_rows": 2}],
            row_count=1,
            scan_version=7,
        ),
        evidence=ExecutionEvidence(
            plan_id="plan-stream",
            datasource_id="ds-sales",
            scan_version=7,
            query_language=QueryLanguage.SQL,
            display_command='SELECT COUNT(*) AS "count_rows" FROM "orders" LIMIT ?',
            row_count=1,
        ),
    )


def make_service(
    tmp_path, query: BusinessQuery | None = None
) -> QanerisService:
    instance = QanerisService(
        Catalog(tmp_path / "catalog.db"), model=IntentModel(query or business_query())
    )
    instance._require_ready_scope = Mock()
    return instance


def executable_grounding() -> SimpleNamespace:
    return SimpleNamespace(
        needs_clarification=False,
        is_executable=True,
        clarifications=[],
        grounded_query=SimpleNamespace(
            bindings=[SimpleNamespace(
                business_term="销售额",
                asset_type=SemanticAssetType.METRIC,
                datasource_id="ds-sales",
                data_object_id="orders",
                field_path="amount",
            )],
            unresolved_ambiguities=[],
            requested_datasource_id="ds-sales",
            scan_version=7,
        ),
    )


def wire_success(instance: QanerisService) -> None:
    from qaneris.contracts import Datasource, DatasourceKind

    instance.catalog.get_datasource = Mock(return_value=(Datasource(
        id="ds-sales", name="sales", workspace_id="workspace-a",
        kind=DatasourceKind.RELATIONAL, driver="sqlite"), {}))
    instance.retrieve_semantics = Mock(
        return_value=SimpleNamespace(
            candidates=[],
            requested_datasource_id="ds-sales",
            scan_version=7,
            retrieval_path="trusted",
        )
    )
    instance.ground_semantics = Mock(return_value=executable_grounding())
    instance.build_query_context = Mock(return_value=object())
    instance.plan_grounded_query = Mock(return_value=grounded_plan())
    instance.execute_grounded_plan = Mock(return_value=grounded_execution())


def event_types(events: list[AskEvent]) -> list[str]:
    return [event.event_type.value for event in events]


def test_success_event_order_sequence_serialization_and_done(tmp_path) -> None:
    instance = make_service(tmp_path)
    wire_success(instance)

    events = list(
        instance.ask_stream(
            AskRequest(
                question="按地区看销售额",
                workspace_id="workspace-a",
                datasource_id="ds-sales",
            )
        )
    )

    assert event_types(events) == [
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
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[-1].event_type is AskEventType.DONE
    assert len({event.correlation_id for event in events}) == 1
    for event in events:
        assert json.loads(event.model_dump_json())["event_type"] == event.event_type.value


def test_low_confidence_execution_event_order(tmp_path) -> None:
    instance = make_service(tmp_path, business_query(confidence=0.4))
    wire_success(instance)

    events = list(
        instance.ask_stream(AskRequest(question="销售额", datasource_id="ds-sales"))
    )

    assert event_types(events) == [
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
    instance.retrieve_semantics.assert_called_once()


def test_rule_conflict_stops_before_retrieval(tmp_path) -> None:
    question = "orders.status=completed 的订单数量"
    instance = make_service(
        tmp_path,
        business_query(question=question, entities=["订单"], metrics=[], dimensions=[]),
    )
    instance.retrieve_semantics = Mock()

    events = list(instance.ask_stream(AskRequest(question=question, datasource_id="ds-sales")))

    assert event_types(events) == [
        "accepted", "intent_ready", "clarification_required", "done",
    ]
    assert "orders.status" in events[-2].payload["clarification"][0]["question"]
    instance.retrieve_semantics.assert_not_called()


def test_grounding_clarification_event_order(tmp_path) -> None:
    instance = make_service(tmp_path)
    instance.retrieve_semantics = Mock(
        return_value=SimpleNamespace(
            candidates=[],
            requested_datasource_id="ds-sales",
            scan_version=7,
            retrieval_path="trusted",
        )
    )
    instance.ground_semantics = Mock(
        return_value=SimpleNamespace(
            needs_clarification=True,
            is_executable=False,
            clarifications=[
                ClarificationRequest(
                    clarification_id="metric",
                    field="metric",
                    question="请选择销售额口径",
                    options=[ClarificationOption(option_id="paid", label="实收销售额")],
                )
            ],
            grounded_query=SimpleNamespace(
                bindings=[],
                unresolved_ambiguities=["销售额口径不唯一"],
                requested_datasource_id="ds-sales",
                scan_version=7,
            ),
        )
    )

    events = list(
        instance.ask_stream(AskRequest(question="销售额", datasource_id="ds-sales"))
    )

    assert event_types(events) == [
        "accepted",
        "intent_ready",
        "retrieval_ready",
        "grounding_ready",
        "clarification_required",
        "done",
    ]


def test_execution_failure_emits_error_then_done_and_one_terminal(tmp_path) -> None:
    instance = make_service(tmp_path)
    wire_success(instance)
    instance.execute_grounded_plan.side_effect = QueryExecutionError("database unavailable")

    events = list(
        instance.ask_stream(AskRequest(question="销售额", datasource_id="ds-sales"))
    )

    assert event_types(events)[-2:] == ["error", "done"]
    terminal = {
        AskEventType.RESULT_READY,
        AskEventType.CLARIFICATION_REQUIRED,
        AskEventType.ERROR,
    }
    assert sum(event.event_type in terminal for event in events) == 1
    assert events[-2].payload["error"]["code"] == "execution_failed"


def test_event_contract_removes_private_reasoning_and_credentials(monkeypatch) -> None:
    secret = "unit-stream-secret"
    monkeypatch.setenv("QANERIS_TEST_TOKEN", secret)

    event = AskEvent(
        event_type="error",
        sequence=1,
        correlation_id="correlation",
        payload={
            "password": "plain-password",
            "access_token": "plain-token",
            "parameters": ["bound-sensitive-value"],
            "reasoning_content": "private chain of thought",
            "message": f"provider rejected {secret}; api_key=standalone-secret-value",
        },
    )
    serialized = event.model_dump_json()

    assert secret not in serialized
    assert "plain-password" not in serialized
    assert "plain-token" not in serialized
    assert "bound-sensitive-value" not in serialized
    assert "standalone-secret-value" not in serialized
    assert "private chain of thought" not in serialized
    assert "reasoning_content" not in serialized
    assert serialized.count("<redacted>") >= 3


def test_stream_terminal_response_matches_ask_response(tmp_path) -> None:
    instance = make_service(tmp_path)
    wire_success(instance)
    request = AskRequest(
        question="按地区看销售额",
        workspace_id="workspace-a",
        datasource_id="ds-sales",
    )

    expected = instance.ask(request)
    events = list(instance.ask_stream(request))
    result_event = next(
        event for event in events if event.event_type is AskEventType.RESULT_READY
    )
    streamed = AskResponse.model_validate(result_event.payload["response"])

    assert streamed == expected
