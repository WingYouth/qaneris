from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.common.errors import ModelInvocationError, QueryExecutionError
from qaneris.contracts import (
    AskRequest,
    AskStatus,
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
from qaneris.semantic import ClarificationOption, ClarificationRequest, RuleExtractor
from qaneris.semantic.time import normalize_time_range


class IntentModel:
    def __init__(self, query: BusinessQuery):
        self.query = query
        self.calls: list[tuple[str, dict]] = []

    def parse_business_query(self, question: str, rule_facts: dict) -> BusinessQuery:
        self.calls.append((question, rule_facts))
        return self.query

    def plan_query(self, *args, **kwargs):
        raise AssertionError("the intent model must not plan SQL")

    def answer_question(self, *args, **kwargs):
        raise AssertionError("answer model unavailable")


def query(**changes) -> BusinessQuery:
    values = {
        "question": "按地区看销售额",
        "objective": "lookup",
        "metrics": ["销售额"],
        "dimensions": ["地区"],
    }
    values.update(changes)
    return BusinessQuery(**values)


def plan() -> GroundedQueryPlan:
    return GroundedQueryPlan(
        plan_id="plan-ask",
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
        scan_version=4,
        aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count_rows")],
        expected_result_type=QueryResultType.SCALAR,
    )


def execution() -> GroundedExecution:
    result = GroundedQueryResult(
        plan_id="plan-ask",
        datasource_id="ds-sales",
        query_language=QueryLanguage.SQL,
        columns=["count_rows"],
        rows=[{"count_rows": 2}],
        row_count=1,
        scan_version=4,
    )
    evidence = ExecutionEvidence(
        plan_id="plan-ask",
        datasource_id="ds-sales",
        scan_version=4,
        query_language=QueryLanguage.SQL,
        display_command='SELECT COUNT(*) AS "count_rows" FROM "orders" LIMIT ?',
        row_count=1,
    )
    return GroundedExecution(result=result, evidence=evidence)


def service(tmp_path, business_query: BusinessQuery | None = None) -> tuple[QanerisService, IntentModel]:
    model = IntentModel(business_query or query())
    instance = QanerisService(Catalog(tmp_path / "catalog.db"), model=model)
    instance._require_ready_scope = Mock()
    return instance, model


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
                time_axis=False,
            )],
            unresolved_ambiguities=[],
        ),
    )


def wire_success(instance: QanerisService):
    from qaneris.contracts import Datasource, DatasourceKind

    instance.catalog.get_datasource = Mock(return_value=(Datasource(
        id="ds-sales", name="sales", workspace_id="workspace-a",
        kind=DatasourceKind.RELATIONAL, driver="sqlite"), {}))
    retrieval = object()
    grounding = executable_grounding()
    context = object()
    grounded_plan = plan()
    instance.retrieve_semantics = Mock(return_value=retrieval)
    instance.ground_semantics = Mock(return_value=grounding)
    instance.build_query_context = Mock(return_value=context)
    instance.plan_grounded_query = Mock(return_value=grounded_plan)
    instance.execute_grounded_plan = Mock(return_value=execution())
    return retrieval, grounding, context, grounded_plan


def test_natural_language_uses_intent_retrieval_and_formal_execution(tmp_path) -> None:
    instance, model = service(tmp_path)
    retrieval, _, context, grounded_plan = wire_success(instance)

    response = instance.ask(
        AskRequest(
            question="按地区看销售额",
            workspace_id="workspace-a",
            datasource_id="ds-sales",
        )
    )

    assert len(model.calls) == 1
    assert model.calls[0][0] == "按地区看销售额"
    assert model.calls[0][1]["requested_datasource_id"] == "ds-sales"
    parsed = instance.retrieve_semantics.call_args.args[0]
    instance.retrieve_semantics.assert_called_once_with(
        parsed, workspace_id="workspace-a", requested_datasource_id="ds-sales"
    )
    instance.ground_semantics.assert_called_once_with(parsed, retrieval)
    instance.execute_grounded_plan.assert_called_once_with(
        context, grounded_plan, workspace_id="workspace-a", max_rows=200
    )
    assert response.status is AskStatus.COMPLETED
    assert response.business_query == parsed
    assert response.plan == grounded_plan
    assert response.result == execution().result
    assert response.evidence == execution().evidence
    assert response.analysis["answer_source"] == "deterministic_fallback"


def test_answer_model_composes_from_executed_result(tmp_path) -> None:
    instance, model = service(tmp_path)
    wire_success(instance)
    model.answer_question = Mock(return_value="查询得到 2 条记录。")

    response = instance.ask(AskRequest(question="销售额", datasource_id="ds-sales"))

    assert response.status is AskStatus.COMPLETED
    assert response.answer == "查询得到 2 条记录。"
    assert response.analysis["answer_source"] == "model"
    model.answer_question.assert_called_once()
    passed_question, payload = model.answer_question.call_args.args
    assert passed_question == "销售额"
    assert payload["rows"] == [{"count_rows": 2}]
    assert set(payload) == {
        "columns", "rows", "row_count", "truncated", "deterministic_numeric_summary",
    }


@pytest.mark.parametrize(
    "model_behavior",
    [ModelInvocationError("HTTP 429"), "查询得到 150 条记录。"],
)
def test_answer_failure_preserves_completed_query(tmp_path, model_behavior) -> None:
    instance, model = service(tmp_path)
    wire_success(instance)
    if isinstance(model_behavior, Exception):
        model.answer_question = Mock(side_effect=model_behavior)
    else:
        model.answer_question = Mock(return_value=model_behavior)

    response = instance.ask(AskRequest(question="销售额", datasource_id="ds-sales"))

    assert response.status is AskStatus.COMPLETED
    assert response.result == execution().result
    assert response.evidence == execution().evidence
    assert response.analysis["answer_source"] == "deterministic_fallback"
    assert "150" not in response.answer


def test_grounding_clarification_stops_before_context_planning_and_execution(tmp_path) -> None:
    instance, _ = service(tmp_path)
    clarification = ClarificationRequest(
        clarification_id="internal-field-id",
        field="metric",
        question="请选择销售额口径",
        options=[
            ClarificationOption(option_id="graph-field-123", label="实收销售额"),
            ClarificationOption(option_id="graph-field-456", label="含税销售额"),
        ],
    )
    instance.retrieve_semantics = Mock(return_value=object())
    instance.ground_semantics = Mock(
        return_value=SimpleNamespace(
            needs_clarification=True,
            is_executable=False,
            clarifications=[clarification],
            grounded_query=SimpleNamespace(unresolved_ambiguities=["ambiguous"]),
        )
    )
    instance.build_query_context = Mock()
    instance.plan_grounded_query = Mock()
    instance.execute_grounded_plan = Mock()

    response = instance.ask(AskRequest(question="销售额", datasource_id="ds-sales"))

    assert response.status is AskStatus.CLARIFICATION_REQUIRED
    assert response.result is None
    assert response.clarification[0].options == ["实收销售额", "含税销售额"]
    assert "graph-field" not in response.model_dump_json()
    instance.build_query_context.assert_not_called()
    instance.plan_grounded_query.assert_not_called()
    instance.execute_grounded_plan.assert_not_called()


def test_missing_published_dimension_is_not_a_text_confirmation() -> None:
    response = QanerisService._clarification_response(
        "当前表格中都有什么商品？",
        query(question="当前表格中都有什么商品？", metrics=[], dimensions=["商品"]),
        [
            ClarificationRequest(
                clarification_id="grounding_missing_dimension_product",
                field="dimension:商品",
                question="请先发布商品维度定义。",
            )
        ],
    )

    assert response.clarification[0].actionable is False
    assert response.clarification[0].options == []


def test_low_confidence_continues_through_unique_grounding(tmp_path) -> None:
    instance, _ = service(tmp_path, query(confidence=0.4))
    wire_success(instance)

    response = instance.ask(AskRequest(question="销售额", datasource_id="ds-sales"))

    assert response.status is AskStatus.COMPLETED
    assert response.result == execution().result
    assert response.business_query.confidence == 0.4
    instance.retrieve_semantics.assert_called_once()
    instance.ground_semantics.assert_called_once()
    instance.execute_grounded_plan.assert_called_once()


def test_intent_ambiguities_are_advisory_evidence(tmp_path) -> None:
    """Model-reported ambiguities are traced in ``analysis`` and must not block execution."""
    instance, _ = service(
        tmp_path,
        query(ambiguities=["销售额可能指实收或含税口径"]),
    )
    wire_success(instance)

    response = instance.ask(AskRequest(question="销售额", datasource_id="ds-sales"))

    assert response.status is AskStatus.COMPLETED
    assert response.clarification == []
    assert response.analysis["intent_ambiguities"] == ["销售额可能指实收或含税口径"]
    instance.execute_grounded_plan.assert_called_once()


def test_execution_error_keeps_its_structured_error_type(tmp_path) -> None:
    instance, _ = service(tmp_path)
    wire_success(instance)
    instance.execute_grounded_plan.side_effect = QueryExecutionError("database unavailable")

    with pytest.raises(QueryExecutionError, match="database unavailable"):
        instance.ask(AskRequest(question="销售额", datasource_id="ds-sales"))


def test_time_expression_without_unique_grounded_date_dimension_clarifies(tmp_path) -> None:
    instance, _ = service(
        tmp_path,
        query(question="最近一个月销售额", dimensions=[], time_expression="最近一个月"),
    )
    instance.retrieve_semantics = Mock(return_value=object())
    instance.ground_semantics = Mock(return_value=executable_grounding())
    instance.build_query_context = Mock()
    instance.execute_grounded_plan = Mock()

    response = instance.ask(
        AskRequest(question="最近一个月销售额", datasource_id="ds-sales")
    )

    assert response.status is AskStatus.CLARIFICATION_REQUIRED
    assert "时间维度" in response.clarification[0].question
    instance.build_query_context.assert_not_called()
    instance.execute_grounded_plan.assert_not_called()


def test_relative_time_normalization_is_deterministic() -> None:
    rules = RuleExtractor().extract("最近一个月销售额")
    normalized = normalize_time_range(rules, today=date(2026, 9, 19))
    assert normalized is not None
    assert normalized.start == date(2026, 8, 19)
    assert normalized.end == date(2026, 9, 19)


def test_unique_grounded_time_dimension_flows_to_context(tmp_path) -> None:
    instance, _ = service(
        tmp_path,
        query(
            question="2026-07-01 到 2026-07-31 按地区看销售额",
            dimensions=["地区", "下单日期"],
            time_expression="2026-07-01 至 2026-07-31",
        ),
    )
    instance.retrieve_semantics = Mock(return_value=object())
    grounding = executable_grounding()
    grounding.grounded_query.bindings = [
        SimpleNamespace(
            asset_type=SemanticAssetType.DIMENSION,
            business_term="下单日期",
            data_type="date",
            time_axis=True,
        )
    ]
    instance.ground_semantics = Mock(return_value=grounding)
    context = object()
    grounded_plan = plan()
    instance.build_query_context = Mock(return_value=context)
    instance.plan_grounded_query = Mock(return_value=grounded_plan)
    instance.execute_grounded_plan = Mock(return_value=execution())
    from qaneris.contracts import Datasource, DatasourceKind

    instance.catalog.get_datasource = Mock(return_value=(Datasource(
        id="ds-sales", name="sales", workspace_id="workspace-a",
        kind=DatasourceKind.RELATIONAL, driver="sqlite"), {}))

    instance.ask(
        AskRequest(
            question="2026-07-01 到 2026-07-31 按地区看销售额",
            workspace_id="workspace-a",
            datasource_id="ds-sales",
        )
    )

    call = instance.build_query_context.call_args
    assert call.kwargs["time_range"].start == date(2026, 7, 1)
    assert call.kwargs["time_range"].end == date(2026, 7, 31)
    assert call.kwargs["time_spec"].dimension == "下单日期"
