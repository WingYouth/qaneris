from __future__ import annotations

from typing import Any, Protocol

from smartdata.contracts.api import QueryPlanStep
from smartdata.contracts.semantic import BusinessQuery


class SchemaModel(Protocol):
    def parse_business_query(
        self, question: str, rule_facts: dict[str, Any]
    ) -> BusinessQuery: ...

    def summarize_schema(self, context: dict[str, Any]) -> str: ...

    def summarize_schema_inventory(self, question: str, context: dict[str, Any]) -> str: ...

    def plan_query(
        self,
        question: str,
        context: dict[str, Any],
        allowed_datasource_ids: list[str],
        max_rows: int,
    ) -> QueryPlanStep: ...

    def answer_question(self, question: str, result: dict[str, Any]) -> str: ...
