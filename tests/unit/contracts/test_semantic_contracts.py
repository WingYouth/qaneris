import pytest
from pydantic import TypeAdapter, ValidationError

from qaneris.contracts import (
    BusinessObjective,
    BusinessQuery,
    ComparisonSpec,
    GroundedQuery,
    GroundingBinding,
    QueryLanguage,
    QueryPlan,
    QueryResultType,
    QueryTrace,
    RankingSpec,
    RequestedOutput,
    ScalarResult,
    SemanticAssetType,
    SemanticCandidate,
    TypedQueryResult,
)


def test_business_query_round_trips_without_physical_identifiers() -> None:
    query = BusinessQuery(
        question="今年销售额最高的前十个地区",
        objective=BusinessObjective.RANKING,
        entities=["订单"],
        metrics=["销售额"],
        dimensions=["地区"],
        time_expression="今年",
        ranking=RankingSpec(direction="top", limit=10, metric="销售额"),
        requested_output=[RequestedOutput.TABLE],
        confidence=0.92,
    )

    restored = BusinessQuery.model_validate_json(query.model_dump_json())

    assert restored == query
    assert "datasource_id" not in BusinessQuery.model_json_schema()["properties"]
    assert "data_object_id" not in BusinessQuery.model_json_schema()["properties"]


def test_business_query_requires_details_for_ranking_and_comparison() -> None:
    with pytest.raises(ValidationError, match="ranking details"):
        BusinessQuery(question="销售额前十", objective="ranking")

    with pytest.raises(ValidationError, match="comparison details"):
        BusinessQuery(question="销售额同比", objective="comparison")

    comparison = BusinessQuery(
        question="销售额同比",
        objective="comparison",
        comparison=ComparisonSpec(comparison_type="year_over_year"),
    )
    assert comparison.comparison.comparison_type == "year_over_year"


def test_semantic_candidate_bounds_score() -> None:
    with pytest.raises(ValidationError):
        SemanticCandidate(
            asset_type=SemanticAssetType.METRIC,
            asset_id="metric_sales",
            name="销售额",
            score=1.1,
        )


def test_grounded_query_only_accepts_allowlisted_physical_bindings() -> None:
    query = BusinessQuery(question="销售额", objective="lookup", metrics=["销售额"])
    binding = GroundingBinding(
        business_term="销售额",
        asset_type="metric",
        asset_id="metric_sales",
        datasource_id="ds_sales",
        data_object_id="orders",
        field_path="orders.amount",
        score=0.95,
        evidence=["metric registry"],
    )

    grounded = GroundedQuery(
        business_query=query,
        bindings=[binding],
        allowed_datasource_ids=["ds_sales"],
        allowed_data_object_ids=["orders"],
        allowed_field_paths=["orders.amount"],
    )
    assert grounded.bindings[0].asset_id == "metric_sales"

    with pytest.raises(ValidationError, match="field is not in the allowlist"):
        GroundedQuery(
            business_query=query,
            bindings=[binding],
            allowed_datasource_ids=["ds_sales"],
            allowed_data_object_ids=["orders"],
            allowed_field_paths=["orders.net_amount"],
        )


def test_typed_query_result_includes_scalar_shape_and_compatibility_name() -> None:
    adapter = TypeAdapter(TypedQueryResult)
    result = adapter.validate_python(
        {
            "result_type": "scalar",
            "datasource_id": "ds_sales",
            "data_object_ids": ["orders"],
            "row_count": 1,
            "value": 400.5,
            "value_type": "decimal",
        }
    )

    assert isinstance(result, ScalarResult)
    assert result.value == 400.5


def test_query_trace_supports_business_and_grounded_query_without_legacy_intent() -> None:
    business_query = BusinessQuery(question="销售额", objective="lookup", metrics=["销售额"])
    grounded_query = GroundedQuery(business_query=business_query)
    plan = QueryPlan(
        id="plan_1",
        datasource_id="ds_sales",
        query_language=QueryLanguage.SQL,
        data_object_ids=["orders"],
        expected_result_type=QueryResultType.SCALAR,
    )

    trace = QueryTrace(
        trace_id="trace_1",
        business_query=business_query,
        grounded_query=grounded_query,
        plan=plan,
        executed_query="SELECT SUM(amount) FROM orders",
        display_query="SELECT SUM(amount) FROM orders",
        duration_ms=2.5,
        result_summary={"row_count": 1},
    )

    assert trace.intent is None
    assert trace.result_summary == {"row_count": 1}


def test_query_trace_requires_legacy_intent_or_business_query() -> None:
    plan = QueryPlan(
        id="plan_1",
        datasource_id="ds_sales",
        query_language="sql",
        data_object_ids=["orders"],
        expected_result_type="tabular",
    )
    with pytest.raises(ValidationError, match="requires intent or business_query"):
        QueryTrace(
            plan=plan,
            executed_query="SELECT 1",
            display_query="SELECT 1",
            duration_ms=1,
        )
