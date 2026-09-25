"""Deterministic parameterized SQL for the three formal relational drivers."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from smartdata.common.errors import QueryPlanningError
from smartdata.contracts.query import (
    FilterOperator,
    GroundedFieldRef,
    GroundedQueryPlan,
    NativeQuery,
    PlanSortTarget,
    QueryLanguage,
    SortDirection,
)
from smartdata.contracts.semantic import AggregateFunction

_AGGREGATES = {
    AggregateFunction.COUNT: "COUNT",
    AggregateFunction.SUM: "SUM",
    AggregateFunction.AVERAGE: "AVG",
    AggregateFunction.MINIMUM: "MIN",
    AggregateFunction.MAXIMUM: "MAX",
}


class GroundedSQLCompiler:
    """Compile only physical identities already present in ``GroundedQueryPlan``."""

    def __init__(self, driver: str = "sqlite") -> None:
        if driver not in {"sqlite", "postgresql", "mysql"}:
            raise QueryPlanningError("formal grounded execution is not enabled for this driver")
        self.driver = driver
        self.quote = "`" if driver == "mysql" else '"'
        self.placeholder = "?" if driver == "sqlite" else "%s"

    def compile(self, plan: GroundedQueryPlan, driver: str | None = None) -> NativeQuery:
        if driver is not None and driver != self.driver:
            return GroundedSQLCompiler(driver).compile(plan)
        if len(plan.joins) > 1:
            raise QueryPlanningError("Phase 1 supports only one confirmed direct join")
        aliases = {object_id: f"t{index}" for index, object_id in enumerate(plan.data_object_ids)}
        tables = {object_id: self._table(plan, object_id) for object_id in plan.data_object_ids}
        select = [self._field(field, aliases) for field in plan.selected_fields]
        for aggregate in plan.aggregates:
            argument = "*" if aggregate.field is None else self._field(aggregate.field, aliases)
            select.append(
                f"{_AGGREGATES[aggregate.function]}({argument}) AS "
                f"{self._quote(aggregate.alias)}"
            )
        base = plan.data_object_ids[0]
        command = f"SELECT {', '.join(select)} FROM {tables[base]} AS {aliases[base]}"
        if plan.joins:
            join = plan.joins[0]
            if join.from_data_object_id == base:
                joined, left_object, left_field, right_object, right_field = (
                    join.to_data_object_id,
                    join.from_data_object_id,
                    join.from_field_path,
                    join.to_data_object_id,
                    join.to_field_path,
                )
            elif join.to_data_object_id == base:
                joined, left_object, left_field, right_object, right_field = (
                    join.from_data_object_id,
                    join.to_data_object_id,
                    join.to_field_path,
                    join.from_data_object_id,
                    join.from_field_path,
                )
            else:
                raise QueryPlanningError("Join is disconnected from the plan base object")
            command += (
                f" JOIN {tables[joined]} AS {aliases[joined]} ON "
                f"{aliases[left_object]}.{self._quote(left_field)} = "
                f"{aliases[right_object]}.{self._quote(right_field)}"
            )

        conditions: list[str] = []
        parameters: list[Any] = []
        for item in plan.filters:
            predicate, values = self._predicate(
                self._field(item.field, aliases), item.operator, item.value
            )
            conditions.append(predicate)
            parameters.extend(values)
        if plan.time_field is not None and plan.time_range is not None:
            field = self._field(plan.time_field, aliases)
            if plan.time_range.start is not None:
                conditions.append(f"{field} >= {self.placeholder}")
                parameters.append(self._value(plan.time_range.start))
            if plan.time_range.end is not None:
                conditions.append(f"{field} <= {self.placeholder}")
                parameters.append(self._value(plan.time_range.end))
        if conditions:
            command += " WHERE " + " AND ".join(conditions)
        if plan.group_by:
            command += " GROUP BY " + ", ".join(
                self._field(item, aliases) for item in plan.group_by
            )
        if plan.sorts:
            ordering = []
            for item in plan.sorts:
                target = (
                    self._quote(item.aggregate_alias)
                    if item.target is PlanSortTarget.AGGREGATE
                    else self._field(item.field, aliases)
                )
                direction = "DESC" if item.direction is SortDirection.DESCENDING else "ASC"
                ordering.append(f"{target} {direction}")
            command += " ORDER BY " + ", ".join(ordering)
        command += f" LIMIT {self.placeholder}"
        parameters.append(plan.limit)
        return NativeQuery(
            datasource_id=plan.datasource_id,
            plan_id=plan.plan_id,
            scan_version=plan.scan_version,
            query_language=QueryLanguage.SQL,
            command=command,
            parameters=tuple(parameters),
            display_command=command,
        )

    def _quote(self, value: str) -> str:
        return self.quote + value.replace(self.quote, self.quote * 2) + self.quote

    def _table(self, plan: GroundedQueryPlan, object_id: str) -> str:
        locator = plan.data_objects.get(object_id)
        if locator is None or locator.name is None:
            raise QueryPlanningError(f"Missing native locator for data object: {object_id}")
        if locator.namespace:
            return f"{self._quote(locator.namespace)}.{self._quote(locator.name)}"
        return self._quote(locator.name)

    def _field(self, field: GroundedFieldRef | None, aliases: dict[str, str]) -> str:
        if field is None or field.data_object_id not in aliases:
            raise QueryPlanningError("Field is outside the compiled plan")
        return f"{aliases[field.data_object_id]}.{self._quote(field.field_path)}"

    def _predicate(self, field: str, operator: FilterOperator, value: Any) -> tuple[str, list[Any]]:
        if value is None:
            if operator is FilterOperator.EQUALS:
                return f"{field} IS NULL", []
            if operator is FilterOperator.NOT_EQUALS:
                return f"{field} IS NOT NULL", []
            raise QueryPlanningError("NULL only supports equals and not_equals")
        simple = {
            FilterOperator.EQUALS: "=",
            FilterOperator.NOT_EQUALS: "!=",
            FilterOperator.GREATER_THAN: ">",
            FilterOperator.GREATER_OR_EQUAL: ">=",
            FilterOperator.LESS_THAN: "<",
            FilterOperator.LESS_OR_EQUAL: "<=",
        }
        if operator in simple:
            return f"{field} {simple[operator]} {self.placeholder}", [self._value(value)]
        if operator is FilterOperator.BETWEEN:
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise QueryPlanningError("BETWEEN requires exactly two bounds")
            return (f"{field} BETWEEN {self.placeholder} AND {self.placeholder}",
                    [self._value(value[0]), self._value(value[1])])
        if operator is FilterOperator.IN:
            # Deterministic input only: list / tuple keep order across runs.
            # ``set`` is rejected so the IN expansion is byte-identical for the
            # same plan regardless of hash order.
            if not isinstance(value, (list, tuple)) or not value:
                raise QueryPlanningError("IN requires a non-empty list or tuple")
            values = list(value)
            if any(item is None for item in values):
                raise QueryPlanningError(
                    "IN with NULL is ambiguous; use an explicit NULL predicate"
                )
            return (f"{field} IN ({', '.join(self.placeholder for _ in values)})",
                    [self._value(v) for v in values])
        if operator is FilterOperator.CONTAINS:
            return f"{field} LIKE {self.placeholder}", [f"%{value}%"]
        raise QueryPlanningError(f"Unsupported filter operator: {operator}")

    @staticmethod
    def _value(value: Any) -> Any:
        return value.isoformat() if isinstance(value, (date, datetime)) else value
