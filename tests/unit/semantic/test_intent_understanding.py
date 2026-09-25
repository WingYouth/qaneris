from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from smartdata.common.errors import IntentParsingError
from smartdata.contracts import BusinessQuery, ComparisonSpec, RankingSpec
from smartdata.semantic import (
    BusinessQueryMergePolicy,
    IntentUnderstandingPipeline,
    LLMBusinessParser,
    RuleExtractor,
)


class FakeBusinessModel:
    def __init__(self, query: BusinessQuery):
        self.query = query
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def parse_business_query(
        self, question: str, rule_facts: dict[str, Any]
    ) -> BusinessQuery:
        self.calls.append((question, rule_facts))
        return self.query


def test_rule_extractor_captures_explicit_high_confidence_facts() -> None:
    extraction = RuleExtractor().extract(
        "数据源 ds_sales 2026-07-01 到 2026-07-31 地区=华东 销售额前 10",
    )

    assert extraction.requested_datasource_id == "ds_sales"
    assert extraction.explicit_dates == [date(2026, 7, 1), date(2026, 7, 31)]
    assert extraction.time_expression == "2026-07-01 至 2026-07-31"
    assert extraction.filters[0].model_dump() == {
        "subject": "地区",
        "operator": "=",
        "value": "华东",
    }
    assert extraction.ranking == RankingSpec(direction="top", limit=10)
    assert extraction.objective_hint == "ranking"


def test_rule_extractor_supports_chinese_ranking_numbers() -> None:
    extraction = RuleExtractor().extract("销售额最低的前二十个渠道")

    assert extraction.ranking is not None
    assert extraction.ranking.limit == 20
    assert extraction.ranking.direction == "bottom"


@pytest.mark.parametrize(
    ("question", "comparison_type"),
    [("销售额同比", "year_over_year"), ("销售额环比", "period_over_period")],
)
def test_rule_extractor_captures_comparison_type(
    question: str, comparison_type: str
) -> None:
    extraction = RuleExtractor().extract(question)

    assert extraction.objective_hint == "comparison"
    assert extraction.comparison == ComparisonSpec(comparison_type=comparison_type)


def test_rule_extractor_prefers_explicit_datasource_argument() -> None:
    extraction = RuleExtractor().extract(
        "数据源 ds_in_question 查询销售额", requested_datasource_id="ds_requested"
    )

    assert extraction.requested_datasource_id == "ds_requested"


def test_rule_extractor_rejects_blank_question() -> None:
    with pytest.raises(IntentParsingError, match="不能为空"):
        RuleExtractor().extract("  ")


def test_llm_business_parser_rejects_physical_identifiers() -> None:
    model = FakeBusinessModel(
        BusinessQuery(
            question="销售额",
            objective="lookup",
            metrics=["orders.amount"],
        )
    )
    rules = RuleExtractor().extract("销售额")

    with pytest.raises(IntentParsingError, match="物理字段"):
        LLMBusinessParser(model).parse("销售额", rules)


def test_merge_policy_gives_explicit_facts_priority_and_preserves_semantics() -> None:
    question = "今年 地区=华东 销售额前 10"
    rules = RuleExtractor().extract(question)
    model_query = BusinessQuery(
        question=question,
        objective="trend",
        metrics=["销售额"],
        dimensions=["地区"],
        filters=[{"subject": "地区", "operator": "=", "value": "华南"}],
        time_expression="去年",
        ranking={"direction": "bottom", "limit": 5, "metric": "销售额"},
        requested_output=["table"],
        confidence=0.95,
    )

    merged, conflicts = BusinessQueryMergePolicy().merge(question, rules, model_query)

    assert merged.objective == "ranking"
    assert merged.time_expression == "今年"
    assert merged.filters[0].value == "华东"
    assert merged.ranking == RankingSpec(direction="top", limit=10, metric="销售额")
    assert merged.confidence == 0.8
    assert {item.field for item in conflicts} >= {
        "objective",
        "time_expression",
        "ranking",
        "filters.地区",
    }


def test_qualified_rule_filter_becomes_clarification_not_business_field() -> None:
    question = "orders.status=completed 的订单数量"
    model = FakeBusinessModel(
        BusinessQuery(question=question, objective="lookup", entities=["订单"])
    )
    result = IntentUnderstandingPipeline(LLMBusinessParser(model)).understand(question)

    assert result.business_query.filters == []
    assert "orders.status" in result.business_query.ambiguities[0]
    assert result.needs_clarification


def test_intent_pipeline_returns_structured_clarification_and_datasource_hint() -> None:
    """A low-confidence parse still reaches grounding with the selected datasource."""
    question = "数据源 ds_sales 查询销售额"
    model = FakeBusinessModel(
        BusinessQuery(
            question=question,
            objective="lookup",
            metrics=["销售额"],
            ambiguities=["销售额可能指成交总额或实收销售额"],
            confidence=0.5,
        )
    )

    result = IntentUnderstandingPipeline(LLMBusinessParser(model)).understand(question)

    assert result.requested_datasource_id == "ds_sales"
    assert not result.needs_clarification
    assert result.clarifications == []
    assert result.business_query.confidence == 0.5
    assert result.business_query.ambiguities == ["销售额可能指成交总额或实收销售额"]
    assert model.calls[0][1]["requested_datasource_id"] == "ds_sales"


def test_model_ambiguities_alone_do_not_request_clarification() -> None:
    """The model's free-text notes are advisory; governed assets define the business terms.

    A confident parse with model-reported ambiguities must reach execution, otherwise the
    pipeline's behaviour would depend on how chatty the configured model happens to be.
    """
    question = "所有订单的销售额合计是多少？"
    model = FakeBusinessModel(
        BusinessQuery(
            question=question,
            objective="lookup",
            metrics=["销售额"],
            ambiguities=["未指定时间范围", "统计口径未明确（是否扣除退款）"],
            confidence=0.95,
        )
    )

    result = IntentUnderstandingPipeline(LLMBusinessParser(model)).understand(question)

    assert not result.needs_clarification
    assert result.clarifications == []
    assert result.business_query.ambiguities == [
        "未指定时间范围",
        "统计口径未明确（是否扣除退款）",
    ]


def test_low_confidence_query_without_rule_conflict_continues() -> None:
    question = "看一下经营情况"
    model = FakeBusinessModel(
        BusinessQuery(question=question, objective="lookup", confidence=0.4)
    )

    result = IntentUnderstandingPipeline(LLMBusinessParser(model)).understand(question)

    assert not result.needs_clarification
    assert result.clarifications == []
    assert result.business_query.confidence == 0.4
