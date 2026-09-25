from __future__ import annotations

import re
from datetime import date
from typing import Any, Protocol

from smartdata.common.errors import IntentParsingError
from smartdata.contracts.semantic import (
    BusinessFilter,
    BusinessObjective,
    BusinessQuery,
    ComparisonSpec,
    RankingSpec,
)
from smartdata.semantic.clarification import ClarificationBuilder
from smartdata.semantic.merge import BusinessQueryMergePolicy
from smartdata.semantic.models import IntentUnderstandingResult, RuleExtraction

_RANKING = re.compile(
    r"(?P<direction>top|bottom|前|后|最高|最低|最多|最少)\s*(?P<limit>\d+|[一二三四五六七八九十百]+)",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"20\d{2}-\d{2}-\d{2}")
_CHINESE_DATE = re.compile(r"20\d{2}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?")
_RELATIVE_TIME = re.compile(
    r"今年|去年|本月|这个月|上个月|本周|这周|上周|"
    r"(?:最近|近)\s*(?:\d+|[一二三四五六七八九十百]+)\s*(?:天|日|个月|月|年)"
)
_FILTER = re.compile(
    r"(?P<subject>[A-Za-z_][A-Za-z0-9_.]*|[\u4e00-\u9fff]{2,})\s*"
    r"(?P<operator>>=|<=|!=|=|>|<)\s*"
    r"(?P<value>'[^']*'|\"[^\"]*\"|[-+]?\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_-]*|[\u4e00-\u9fff]+)"
)
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
_DATASOURCE = re.compile(
    r"(?:datasource|data_source|数据源)\s*(?:=|:|：)?\s*([A-Za-z_][A-Za-z0-9_-]*)",
    re.IGNORECASE,
)
_PHYSICAL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


class BusinessQueryModel(Protocol):
    def parse_business_query(
        self, question: str, rule_facts: dict[str, Any]
    ) -> BusinessQuery: ...


class BusinessQueryParser(Protocol):
    def parse(self, question: str, rules: RuleExtraction) -> BusinessQuery: ...


class RuleExtractor:
    def extract(
        self, question: str, requested_datasource_id: str | None = None
    ) -> RuleExtraction:
        if not question.strip():
            raise IntentParsingError("问题不能为空")
        ranking = self._ranking(question)
        comparison = self._comparison(question)
        dates, time_expression = self._time(question)
        filters = [self._filter(match) for match in _FILTER.finditer(question)]
        datasource_match = _DATASOURCE.search(question)
        datasource_id = requested_datasource_id or (
            datasource_match.group(1) if datasource_match else None
        )
        return RuleExtraction(
            question=question,
            objective_hint=self._objective(question, ranking, comparison),
            explicit_dates=dates,
            time_expression=time_expression,
            filters=filters,
            comparison=comparison,
            ranking=ranking,
            numeric_values=[self._number(item) for item in _NUMBER.findall(question)],
            requested_datasource_id=datasource_id,
        )

    @staticmethod
    def _objective(
        question: str,
        ranking: RankingSpec | None,
        comparison: ComparisonSpec | None,
    ) -> BusinessObjective | None:
        lowered = question.casefold()
        if any(token in lowered for token in ("为什么", "原因", "诊断", "归因", "why")):
            return BusinessObjective.DIAGNOSIS
        if ranking:
            return BusinessObjective.RANKING
        if comparison:
            return BusinessObjective.COMPARISON
        if any(token in lowered for token in ("趋势", "走势", "变化", "trend")):
            return BusinessObjective.TREND
        return None

    @staticmethod
    def _comparison(question: str) -> ComparisonSpec | None:
        lowered = question.casefold()
        if "同比" in lowered or "year over year" in lowered or "yoy" in lowered:
            return ComparisonSpec(comparison_type="year_over_year")
        if "环比" in lowered or "period over period" in lowered or "mom" in lowered:
            return ComparisonSpec(comparison_type="period_over_period")
        if any(token in lowered for token in ("对比", "比较", "vs", " versus ")):
            return ComparisonSpec(comparison_type="group")
        return None

    @staticmethod
    def _ranking(question: str) -> RankingSpec | None:
        match = _RANKING.search(question)
        if not match:
            return None
        direction_token = match.group("direction").casefold()
        lowered = question.casefold()
        direction = (
            "bottom"
            if direction_token in {"bottom", "后", "最低", "最少"}
            or any(token in lowered for token in ("bottom", "最低", "最少"))
            else "top"
        )
        return RankingSpec(
            direction=direction,
            limit=min(_parse_count(match.group("limit")), 1_000),
        )

    @staticmethod
    def _time(question: str) -> tuple[list[date], str | None]:
        raw_dates = _ISO_DATE.findall(question) + _CHINESE_DATE.findall(question)
        dates = [_parse_date(value) for value in raw_dates]
        if raw_dates:
            return dates, " 至 ".join(value.strip() for value in raw_dates)
        relative = _RELATIVE_TIME.search(question)
        return [], relative.group(0).replace(" ", "") if relative else None

    @staticmethod
    def _filter(match: re.Match[str]) -> BusinessFilter:
        raw = match.group("value").strip("'\"")
        value: str | int | float = raw
        try:
            value = RuleExtractor._number(raw)
        except ValueError:
            pass
        return BusinessFilter(
            subject=match.group("subject"), operator=match.group("operator"), value=value
        )

    @staticmethod
    def _number(value: str) -> int | float:
        return float(value) if "." in value else int(value)


class LLMBusinessParser:
    def __init__(self, model: BusinessQueryModel):
        self.model = model

    def parse(self, question: str, rules: RuleExtraction) -> BusinessQuery:
        query = self.model.parse_business_query(question, rules.model_dump(mode="json"))
        self._reject_physical_identifiers(query)
        return query

    @staticmethod
    def _reject_physical_identifiers(query: BusinessQuery) -> None:
        terms = [*query.entities, *query.metrics, *query.dimensions]
        terms.extend(item.subject for item in query.filters)
        terms.extend(item.metric for item in query.derivations if item.metric)
        if query.ranking and query.ranking.metric:
            terms.append(query.ranking.metric)
        physical = next((item for item in terms if _PHYSICAL_IDENTIFIER.fullmatch(item)), None)
        if physical:
            raise IntentParsingError(f"模型返回了物理字段标识：{physical}")


class IntentUnderstandingPipeline:
    def __init__(
        self,
        parser: BusinessQueryParser,
        rule_extractor: RuleExtractor | None = None,
        merge_policy: BusinessQueryMergePolicy | None = None,
        clarification_builder: ClarificationBuilder | None = None,
    ):
        self.parser = parser
        self.rule_extractor = rule_extractor or RuleExtractor()
        self.merge_policy = merge_policy or BusinessQueryMergePolicy()
        self.clarification_builder = clarification_builder or ClarificationBuilder()

    def understand(
        self, question: str, requested_datasource_id: str | None = None
    ) -> IntentUnderstandingResult:
        rules = self.rule_extractor.extract(question, requested_datasource_id)
        model_query = self.parser.parse(question, rules)
        business_query, conflicts = self.merge_policy.merge(question, rules, model_query)
        return IntentUnderstandingResult(
            business_query=business_query,
            rule_extraction=rules,
            conflicts=conflicts,
            clarifications=self.clarification_builder.build(
                business_query,
                rule_ambiguities=self.merge_policy.rule_ambiguities(rules),
            ),
            requested_datasource_id=rules.requested_datasource_id,
        )


def _parse_count(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if value == "百":
        return 100
    if "百" in value:
        hundreds, remainder = value.split("百", 1)
        return digits.get(hundreds, 1) * 100 + (_parse_count(remainder) if remainder else 0)
    if "十" in value:
        tens, ones = value.split("十", 1)
        return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return digits[value]


def _parse_date(value: str) -> date:
    if "-" in value:
        return date.fromisoformat(value)
    numbers = [int(item) for item in re.findall(r"\d+", value)]
    if len(numbers) == 2:
        return date(numbers[0], numbers[1], 1)
    return date(numbers[0], numbers[1], numbers[2])
