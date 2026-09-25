"""Compile governed Redis virtual fields to a small read-only command set."""

from __future__ import annotations

import json
from typing import Any

from qaneris.common.errors import QueryPlanningError
from qaneris.contracts.query import FilterOperator, GroundedQueryPlan, NativeQuery, QueryLanguage

VIRTUAL_FIELDS = frozenset({"key", "value", "type", "ttl", "exists", "cardinality"})
_VALUE_COMMAND = {"string": "GET", "hash": "HGETALL", "list": "LRANGE",
                  "set": "SMEMBERS", "zset": "ZRANGE"}


class GroundedRedisCompiler:
    def compile(self, plan: GroundedQueryPlan) -> NativeQuery:
        if len(plan.data_object_ids) != 1 or plan.joins or plan.aggregates or plan.group_by:
            raise QueryPlanningError("Redis does not support joins or SQL aggregation")
        if plan.time_range or plan.time_field or plan.sorts:
            raise QueryPlanningError("Redis does not support time ranges or arbitrary sorting")
        locator = plan.data_objects.get(plan.data_object_ids[0])
        if locator is None or locator.object_kind not in _VALUE_COMMAND:
            raise QueryPlanningError("Redis object kind is unsupported")
        fields = {field.field_path for field in plan.selected_fields}
        if not fields or not fields <= VIRTUAL_FIELDS:
            raise QueryPlanningError("Redis selected field is not a governed virtual field")
        key_values: list[Any] | None = None
        for item in plan.filters:
            if item.field.field_path != "key" or key_values is not None:
                raise QueryPlanningError("Redis supports one key filter only")
            if item.operator is FilterOperator.EQUALS and isinstance(item.value, str):
                key_values = [item.value]
            elif item.operator is FilterOperator.IN and isinstance(item.value, (list, tuple)):
                key_values = list(item.value)
            else:
                raise QueryPlanningError("Redis key filter must be equals or IN")
            if not key_values or len(key_values) > plan.limit or any(
                not isinstance(key, str) or not key for key in key_values
            ):
                raise QueryPlanningError("Redis key filter is invalid or exceeds the limit")
        if key_values is None:
            if fields != {"key"}:
                raise QueryPlanningError("Redis key listing may select only key")
            command = {"command": "SCAN", "args": [], "kind": locator.object_kind,
                       "limit": plan.limit}
        else:
            if len(fields) != 1:
                raise QueryPlanningError("Redis exact-key query selects one virtual field")
            selected = next(iter(fields))
            if selected == "key":
                raise QueryPlanningError("Redis exact-key query must select a read capability")
            if selected == "value":
                if locator.object_kind == "string" and len(key_values) > 1:
                    command = {"command": "MGET", "args": key_values}
                else:
                    if len(key_values) != 1:
                        raise QueryPlanningError("Redis collection values require one exact key")
                    op = _VALUE_COMMAND[locator.object_kind]
                    args = [key_values[0]]
                    if op in {"LRANGE", "ZRANGE"}:
                        args.extend([0, plan.limit - 1])
                    command = {"command": op, "args": args}
            else:
                if len(key_values) != 1:
                    raise QueryPlanningError("Redis metadata reads require one exact key")
                mapping = {"exists": "EXISTS", "type": "TYPE", "ttl": "TTL"}
                if selected == "cardinality":
                    if locator.object_kind not in {"set", "zset"}:
                        raise QueryPlanningError("Redis cardinality needs a set or zset")
                    op = "SCARD" if locator.object_kind == "set" else "ZCARD"
                else:
                    op = mapping.get(selected)
                if op is None:
                    raise QueryPlanningError("Unsupported Redis virtual field")
                command = {"command": op, "args": key_values}
        safe = {"command": command["command"],
                "args": ["<param>" for _ in command["args"]]}
        if command["command"] == "SCAN":
            safe.update(kind=command["kind"], limit=command["limit"])
        return NativeQuery(datasource_id=plan.datasource_id, plan_id=plan.plan_id,
                           scan_version=plan.scan_version, query_language=QueryLanguage.REDIS_JSON,
                           command=command,
                           display_command=json.dumps(safe, ensure_ascii=False, sort_keys=True,
                                                      separators=(",", ":")))
