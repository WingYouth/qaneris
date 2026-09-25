"""Rule-based query intent parsing."""

from __future__ import annotations

import re
from typing import Protocol

from smartdata.contracts.query import (
    AggregateFunction,
    AggregateSpec,
    FilterOperator,
    FilterSpec,
    QueryIntent,
    SortDirection,
    SortSpec,
    TimeRange,
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*|[\u4e00-\u9fff]{2,}")
_LIMIT = re.compile(r"(?:top|limit|前|最高|最多)\s*(\d+)", re.IGNORECASE)
_DATE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
_FILTER = re.compile(
    r"(?P<field>[A-Za-z_][A-Za-z0-9_.]*)\s*(?P<operator>>=|<=|!=|=|>|<)\s*"
    r"(?P<value>'[^']*'|\"[^\"]*\"|[-+]?\d+(?:\.\d+)?)"
)
_STOP_WORDS = {
    "top",
    "limit",
    "select",
    "where",
    "查询",
    "查看",
    "请问",
    "多少",
    "数据",
    "情况",
}


class QueryIntentParser(Protocol):
    def parse(self, question: str, requested_datasource_id: str | None = None) -> QueryIntent: ...


class RuleBasedQueryIntentParser:
    """Compatibility parser for the legacy QueryIntent preparation path."""

    def parse(self, question: str, requested_datasource_id: str | None = None) -> QueryIntent:
        lowered = question.lower()
        aggregate = self._aggregate(lowered)
        identifiers = [
            token
            for token in _IDENTIFIER.findall(lowered)
            if token not in _STOP_WORDS and not token.isdigit()
        ]
        filters = [self._filter(match) for match in _FILTER.finditer(question)]
        filter_fields = {item.field.lower() for item in filters}
        terms = [item for item in identifiers if item.lower() not in filter_fields]
        limit_match = _LIMIT.search(lowered)
        limit = int(limit_match.group(1)) if limit_match else None
        dates = _DATE.findall(question)
        time_range = TimeRange(start=dates[0], end=dates[-1]) if dates else None
        sorts = []
        if any(token in lowered for token in ("top", "最高", "最多", "降序")):
            sorts.append(SortSpec(field="__metric__", direction=SortDirection.DESCENDING))
        return QueryIntent(
            question=question,
            entities=terms,
            metrics=terms if aggregate and aggregate.function != AggregateFunction.COUNT else [],
            time_range=time_range,
            filters=filters,
            aggregates=[aggregate] if aggregate else [],
            sorts=sorts,
            limit=min(limit, 1_000) if limit else None,
            requested_datasource_id=requested_datasource_id,
            confidence=0.75 if terms else 0.5,
            ambiguities=[] if terms else ["未识别出明确的业务实体或字段"],
        )

    @staticmethod
    def _aggregate(question: str) -> AggregateSpec | None:
        rules = (
            (AggregateFunction.AVERAGE, ("平均", "均值", "average", "avg")),
            (AggregateFunction.SUM, ("总额", "合计", "总和", "sum")),
            (AggregateFunction.MAXIMUM, ("最大", "最高", "max")),
            (AggregateFunction.MINIMUM, ("最小", "最低", "min")),
            (AggregateFunction.COUNT, ("多少", "数量", "几条", "count")),
        )
        for function, tokens in rules:
            if any(token in question for token in tokens):
                return AggregateSpec(function=function)
        return None

    @staticmethod
    def _filter(match: re.Match[str]) -> FilterSpec:
        operators = {
            "=": FilterOperator.EQUALS,
            "!=": FilterOperator.NOT_EQUALS,
            ">": FilterOperator.GREATER_THAN,
            ">=": FilterOperator.GREATER_OR_EQUAL,
            "<": FilterOperator.LESS_THAN,
            "<=": FilterOperator.LESS_OR_EQUAL,
        }
        raw = match.group("value").strip("'\"")
        value: str | int | float = raw
        try:
            value = float(raw) if "." in raw else int(raw)
        except ValueError:
            pass
        return FilterSpec(
            field=match.group("field"), operator=operators[match.group("operator")], value=value
        )
