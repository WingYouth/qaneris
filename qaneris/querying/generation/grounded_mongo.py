"""Deterministic, single-collection MongoDB read compilation."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from qaneris.common.errors import QueryPlanningError
from qaneris.contracts.query import (
    FilterOperator,
    GroundedQueryPlan,
    NativeQuery,
    PlanSortTarget,
    QueryLanguage,
    SortDirection,
)
from qaneris.contracts.semantic import AggregateFunction

_OPERATORS = {
    FilterOperator.EQUALS: "$eq", FilterOperator.NOT_EQUALS: "$ne",
    FilterOperator.GREATER_THAN: "$gt", FilterOperator.GREATER_OR_EQUAL: "$gte",
    FilterOperator.LESS_THAN: "$lt", FilterOperator.LESS_OR_EQUAL: "$lte",
    FilterOperator.IN: "$in",
}
_AGGREGATES = {
    AggregateFunction.SUM: "$sum", AggregateFunction.AVERAGE: "$avg",
    AggregateFunction.MINIMUM: "$min", AggregateFunction.MAXIMUM: "$max",
}


def _value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _time_value(value: date | datetime, *, end: bool = False) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, datetime.max.time() if end else datetime.min.time())


def _safe_display(command: dict[str, Any]) -> str:
    """Preserve query shape while replacing every business filter value."""
    safe = {"operation": command["operation"], "collection": command["collection"]}
    if command["operation"] == "find":
        safe.update(filter="<param>" if command["filter"] else {},
                    projection=command["projection"], sort=command["sort"],
                    limit=command["limit"])
    else:
        stages = []
        for stage in command["pipeline"]:
            if "$match" in stage:
                stages.append({"$match": "<param>"})
            else:
                stages.append(stage)
        safe["pipeline"] = stages
    return json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class GroundedMongoCompiler:
    def compile(self, plan: GroundedQueryPlan) -> NativeQuery:
        if len(plan.data_object_ids) != 1 or plan.joins:
            raise QueryPlanningError("MongoDB formal execution supports one collection only")
        locator = plan.data_objects.get(plan.data_object_ids[0])
        if locator is None or not locator.name:
            raise QueryPlanningError("MongoDB collection locator is missing")
        allowed = {ref.field_path for ref in plan.selected_fields}
        allowed.update(ref.field_path for ref in plan.group_by)
        allowed.update(item.field.field_path for item in plan.filters)
        allowed.update(item.field.field_path for item in plan.aggregates if item.field)
        allowed.update(item.field.field_path for item in plan.sorts if item.field)
        if plan.time_field:
            allowed.add(plan.time_field.field_path)
        if any(not path or any(part.startswith("$") or not part for part in path.split("."))
               for path in allowed):
            raise QueryPlanningError("MongoDB field path is unsafe")
        clauses: list[dict[str, Any]] = []
        for item in plan.filters:
            field, value = item.field.field_path, item.value
            if item.operator is FilterOperator.CONTAINS:
                clauses.append({field: {"$regex": re.escape(str(value))}})
            elif item.operator is FilterOperator.BETWEEN:
                if not isinstance(value, (list, tuple)) or len(value) != 2:
                    raise QueryPlanningError("BETWEEN requires two bounds")
                clauses.append({field: {"$gte": _value(value[0]), "$lte": _value(value[1])}})
            elif item.operator is FilterOperator.IN:
                if not isinstance(value, (list, tuple)) or not value:
                    raise QueryPlanningError("IN requires values")
                clauses.append({field: {"$in": [_value(v) for v in value]}})
            elif item.operator in _OPERATORS:
                clauses.append({field: {_OPERATORS[item.operator]: _value(value)}})
            else:
                raise QueryPlanningError("Unsupported MongoDB filter")
        if plan.time_field and plan.time_range:
            bounds = {}
            if plan.time_range.start is not None:
                bounds["$gte"] = _time_value(plan.time_range.start)
            if plan.time_range.end is not None:
                bounds["$lte"] = _time_value(plan.time_range.end, end=True)
            if bounds:
                clauses.append({plan.time_field.field_path: bounds})
        match = {"$and": clauses} if len(clauses) > 1 else clauses[0] if clauses else {}
        sort = []
        for item in plan.sorts:
            name = item.aggregate_alias if item.target is PlanSortTarget.AGGREGATE else item.field.field_path
            sort.append((name, -1 if item.direction is SortDirection.DESCENDING else 1))
        if not plan.aggregates and not plan.group_by:
            if any(item.target is PlanSortTarget.AGGREGATE for item in plan.sorts):
                raise QueryPlanningError("MongoDB aggregate sort requires an aggregate")
            projection = {ref.field_path: 1 for ref in plan.selected_fields}
            if "_id" not in projection:
                projection["_id"] = 0
            command = {"operation": "find", "collection": locator.name, "filter": match,
                       "projection": projection,
                       "sort": sort, "limit": plan.limit}
        else:
            group_keys = {ref.field_path.replace(".", "_"): f"${ref.field_path}"
                          for ref in plan.group_by}
            if len(group_keys) != len(plan.group_by):
                raise QueryPlanningError("MongoDB group field aliases collide")
            group: dict[str, Any] = {"_id": group_keys if group_keys else None}
            for item in plan.aggregates:
                if item.alias.startswith("$") or "." in item.alias:
                    raise QueryPlanningError("MongoDB aggregate alias is unsafe")
                if item.alias in group_keys:
                    raise QueryPlanningError("MongoDB aggregate alias collides with a group field")
                if item.function is AggregateFunction.COUNT:
                    group[item.alias] = {"$sum": 1}
                else:
                    group[item.alias] = {
                        _AGGREGATES[item.function]: f"${item.field.field_path}"
                    }
            project: dict[str, Any] = {"_id": 0}
            for ref in plan.group_by:
                project[ref.field_path.replace(".", "_")] = f"$_id.{ref.field_path.replace('.', '_')}"
            for item in plan.aggregates:
                project[item.alias] = 1
            pipeline: list[dict[str, Any]] = []
            if match:
                pipeline.append({"$match": match})
            pipeline.extend([{"$group": group}, {"$project": project}])
            if sort:
                normalized_sort = {}
                for name, direction in sort:
                    key = name.replace(".", "_")
                    if key not in project:
                        raise QueryPlanningError("MongoDB sort is outside projected results")
                    normalized_sort[key] = direction
                pipeline.append({"$sort": normalized_sort})
            pipeline.append({"$limit": plan.limit})
            command = {"operation": "aggregate", "collection": locator.name,
                       "pipeline": pipeline}
        return NativeQuery(datasource_id=plan.datasource_id, plan_id=plan.plan_id,
                           scan_version=plan.scan_version, query_language=QueryLanguage.MONGODB_JSON,
                           command=command, display_command=_safe_display(command))
