"""Native query generation from validated plans."""

from __future__ import annotations

import json
from typing import Any

from smartdata.common.errors import QueryPlanningError
from smartdata.contracts.profile import DataObjectProfile, RelationshipProfile
from smartdata.contracts.query import (
    AggregateFunction,
    FilterOperator,
    GeneratedQuery,
    QueryContext,
    QueryLanguage,
    QueryPlan,
)


def _sql_identifier(value: str, quote: str = '"') -> str:
    if quote == "[":
        return ".".join(f"[{part.replace(']', ']]')}]" for part in value.split("."))
    return ".".join(f"{quote}{part.replace(quote, quote * 2)}{quote}" for part in value.split("."))


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


class QueryGenerator:
    def generate(self, plan: QueryPlan, context: QueryContext) -> GeneratedQuery:
        data_objects, relationships, driver = self._targets(plan, context)
        data_object = data_objects[0]
        language = plan.query_language
        self._validate_capabilities(plan)
        if language in (QueryLanguage.SQL, QueryLanguage.CQL):
            command: str | dict[str, Any] = self._sql(plan, data_objects, relationships, driver)
        elif language == QueryLanguage.MONGODB_JSON:
            command = self._mongo(plan, data_object)
        elif language == QueryLanguage.COUCHDB_MANGO:
            command = self._couchdb(plan)
        elif language == QueryLanguage.REDIS_JSON:
            command = {"command": "SCAN", "args": ["0", "MATCH", "*", "COUNT", plan.limit]}
        elif language == QueryLanguage.HBASE_JSON:
            command = {"table": data_object.name, "limit": plan.limit}
        elif language == QueryLanguage.CYPHER:
            command = self._cypher(plan, data_object)
        elif language == QueryLanguage.FLUX:
            command = self._flux(plan, data_object)
        elif language == QueryLanguage.SEARCH_JSON:
            command = self._search(plan, data_object)
        elif language == QueryLanguage.VECTOR_FILTER_JSON:
            command = {
                "collection": data_object.name,
                "filter": "" if driver == "milvus" else {},
                "limit": plan.limit,
            }
        elif language == QueryLanguage.GRAPHQL:
            command = self._graphql(plan, data_object)
        else:
            raise QueryPlanningError(f"尚无查询生成器: {language.value}")
        serialized = (
            json.dumps(command, ensure_ascii=False) if isinstance(command, dict) else command
        )
        return GeneratedQuery(
            datasource_id=plan.datasource_id,
            language=language,
            command=serialized,
            display_command=serialized,
        )

    @staticmethod
    def _validate_capabilities(plan: QueryPlan) -> None:
        if plan.aggregates and plan.query_language not in {
            QueryLanguage.SQL,
            QueryLanguage.CQL,
        }:
            raise QueryPlanningError(f"{plan.query_language.value} 第一版尚不支持聚合查询")
        if plan.filters and plan.query_language not in {
            QueryLanguage.SQL,
            QueryLanguage.CQL,
            QueryLanguage.MONGODB_JSON,
            QueryLanguage.COUCHDB_MANGO,
            QueryLanguage.SEARCH_JSON,
            QueryLanguage.CYPHER,
        }:
            raise QueryPlanningError(f"{plan.query_language.value} 第一版尚不支持字段过滤")
        if plan.time_range and plan.query_language not in {
            QueryLanguage.SQL,
            QueryLanguage.CQL,
            QueryLanguage.MONGODB_JSON,
            QueryLanguage.COUCHDB_MANGO,
            QueryLanguage.SEARCH_JSON,
            QueryLanguage.FLUX,
        }:
            raise QueryPlanningError(f"{plan.query_language.value} 第一版尚不支持时间范围过滤")

    @staticmethod
    def _targets(
        plan: QueryPlan, context: QueryContext
    ) -> tuple[list[DataObjectProfile], list[RelationshipProfile], str]:
        for datasource in context.data_sources:
            if datasource.datasource_id != plan.datasource_id:
                continue
            available = {item.profile.id: item.profile for item in datasource.data_objects}
            if set(plan.data_object_ids).issubset(available):
                relationships = [
                    item for item in context.relationships if item.id in plan.relationship_ids
                ]
                return (
                    [available[object_id] for object_id in plan.data_object_ids],
                    relationships,
                    datasource.driver,
                )
        raise QueryPlanningError("查询计划引用了未召回的数据对象")

    def _sql(
        self,
        plan: QueryPlan,
        data_objects: list[DataObjectProfile],
        relationships: list[RelationshipProfile],
        driver: str,
    ) -> str:
        quote = "`" if driver in {"mysql", "bigquery"} else "[" if driver == "sqlserver" else '"'
        use_aliases = driver != "cassandra"
        aliases = {
            item.id: f"t{index}" if use_aliases else "" for index, item in enumerate(data_objects)
        }
        objects = {item.id: item for item in data_objects}
        dimensions = [self._sql_field(field, aliases, quote) for field in plan.dimensions]
        expressions = list(dimensions)
        functions = {
            AggregateFunction.COUNT: "COUNT",
            AggregateFunction.SUM: "SUM",
            AggregateFunction.AVERAGE: "AVG",
            AggregateFunction.MINIMUM: "MIN",
            AggregateFunction.MAXIMUM: "MAX",
        }
        for aggregate in plan.aggregates:
            argument = (
                "*" if aggregate.field is None else self._sql_field(aggregate.field, aliases, quote)
            )
            expression = f"{functions[aggregate.function]}({argument})"
            if aggregate.alias:
                expression += f" AS {_sql_identifier(aggregate.alias, quote)}"
            expressions.append(expression)
        if not expressions:
            expressions = [
                self._sql_field(field, aliases, quote) for field in plan.output_fields
            ] or ["*"]
        primary = data_objects[0]
        select_prefix = f"SELECT TOP ({plan.limit})" if driver == "sqlserver" else "SELECT"
        primary_alias = f" {aliases[primary.id]}" if aliases[primary.id] else ""
        query = (
            f"{select_prefix} {', '.join(expressions)} FROM "
            f"{self._sql_table(primary, quote)}{primary_alias}"
        )
        joined = {primary.id}
        for relationship in relationships:
            source_id = relationship.from_object_id
            target_id = relationship.to_object_id
            if target_id not in objects or source_id not in objects:
                raise QueryPlanningError("查询关系引用了计划外数据对象")
            if relationship.from_field_path is None or relationship.to_field_path is None:
                raise QueryPlanningError("关系型查询需要字段级关联关系")
            if source_id in joined and target_id not in joined:
                joined_id = target_id
            elif target_id in joined and source_id not in joined:
                joined_id = source_id
            elif source_id in joined and target_id in joined:
                continue
            else:
                raise QueryPlanningError("查询关系不能形成连续的连接路径")
            query += (
                f" JOIN {self._sql_table(objects[joined_id], quote)} {aliases[joined_id]} ON "
                f"{aliases[source_id]}.{_sql_identifier(relationship.from_field_path, quote)} = "
                f"{aliases[target_id]}.{_sql_identifier(relationship.to_field_path, quote)}"
            )
            joined.add(joined_id)
        predicates = [
            self._sql_predicate(item.field, item.operator, item.value, aliases, quote)
            for item in plan.filters
        ]
        predicates.extend(self._sql_time_predicates(plan, aliases, quote))
        if predicates:
            query += " WHERE " + " AND ".join(predicates)
        if plan.group_by:
            query += " GROUP BY " + ", ".join(
                self._sql_field(field, aliases, quote) for field in plan.group_by
            )
        if plan.sorts:
            query += " ORDER BY " + ", ".join(
                f"{self._sql_sort_field(item.field, aliases, quote)} "
                f"{'DESC' if item.direction.value == 'descending' else 'ASC'}"
                for item in plan.sorts
            )
        if driver == "sqlserver":
            return query
        if driver == "oracle":
            return f"{query} FETCH FIRST {plan.limit} ROWS ONLY"
        return f"{query} LIMIT {plan.limit}"

    @classmethod
    def _sql_time_predicates(
        cls, plan: QueryPlan, aliases: dict[str, str], quote: str
    ) -> list[str]:
        if plan.time_range is None or plan.time_field is None:
            return []
        field = cls._sql_field(plan.time_field, aliases, quote)
        predicates = []
        if plan.time_range.start is not None:
            predicates.append(f"{field} >= {_literal(plan.time_range.start)}")
        if plan.time_range.end is not None:
            predicates.append(f"{field} <= {_literal(plan.time_range.end)}")
        return predicates

    @staticmethod
    def _sql_table(data_object: DataObjectProfile, quote: str) -> str:
        name = ".".join(filter(None, (data_object.namespace, data_object.name)))
        return _sql_identifier(name, quote)

    @staticmethod
    def _sql_field(reference: str, aliases: dict[str, str], quote: str) -> str:
        try:
            object_id, field = reference.split("::", 1)
            alias = aliases[object_id]
        except (ValueError, KeyError) as error:
            raise QueryPlanningError(f"字段引用不属于查询计划: {reference}") from error
        prefix = f"{alias}." if alias else ""
        return f"{prefix}{_sql_identifier(field, quote)}"

    @classmethod
    def _sql_sort_field(cls, reference: str, aliases: dict[str, str], quote: str) -> str:
        return (
            cls._sql_field(reference, aliases, quote)
            if "::" in reference
            else _sql_identifier(reference, quote)
        )

    @staticmethod
    def _sql_operator(operator: FilterOperator) -> str:
        operators = {
            FilterOperator.EQUALS: "=",
            FilterOperator.NOT_EQUALS: "!=",
            FilterOperator.GREATER_THAN: ">",
            FilterOperator.GREATER_OR_EQUAL: ">=",
            FilterOperator.LESS_THAN: "<",
            FilterOperator.LESS_OR_EQUAL: "<=",
            FilterOperator.IN: "IN",
            FilterOperator.BETWEEN: "BETWEEN",
            FilterOperator.CONTAINS: "LIKE",
        }
        return operators[operator]

    @classmethod
    def _sql_predicate(
        cls,
        field: str,
        operator: FilterOperator,
        value: Any,
        aliases: dict[str, str],
        quote: str,
    ) -> str:
        expression = cls._sql_field(field, aliases, quote)
        if operator == FilterOperator.IN:
            if not isinstance(value, (list, tuple)) or not value:
                raise QueryPlanningError("IN 过滤需要非空值列表")
            return f"{expression} IN ({', '.join(_literal(item) for item in value)})"
        if operator == FilterOperator.BETWEEN:
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise QueryPlanningError("BETWEEN 过滤需要两个边界值")
            return f"{expression} BETWEEN {_literal(value[0])} AND {_literal(value[1])}"
        if operator == FilterOperator.CONTAINS:
            return f"{expression} LIKE {_literal('%' + str(value) + '%')}"
        return f"{expression} {cls._sql_operator(operator)} {_literal(value)}"

    @staticmethod
    def _mongo(plan: QueryPlan, data_object: DataObjectProfile) -> dict[str, Any]:
        operators = {
            FilterOperator.EQUALS: None,
            FilterOperator.NOT_EQUALS: "$ne",
            FilterOperator.GREATER_THAN: "$gt",
            FilterOperator.GREATER_OR_EQUAL: "$gte",
            FilterOperator.LESS_THAN: "$lt",
            FilterOperator.LESS_OR_EQUAL: "$lte",
            FilterOperator.IN: "$in",
            FilterOperator.CONTAINS: "$regex",
        }
        filters = {}
        for item in plan.filters:
            operator = operators.get(item.operator)
            field = QueryGenerator._native_field(item.field, data_object)
            if item.operator == FilterOperator.BETWEEN:
                if not isinstance(item.value, (list, tuple)) or len(item.value) != 2:
                    raise QueryPlanningError("BETWEEN 过滤需要两个边界值")
                filters[field] = {"$gte": item.value[0], "$lte": item.value[1]}
            else:
                filters[field] = item.value if operator is None else {operator: item.value}
        QueryGenerator._add_document_time_filter(filters, plan, data_object)
        return {
            "collection": data_object.name,
            "filter": filters,
            "projection": {
                QueryGenerator._native_field(field, data_object): 1 for field in plan.output_fields
            }
            or None,
            "limit": plan.limit,
        }

    @staticmethod
    def _couchdb(plan: QueryPlan) -> dict[str, Any]:
        operators = {
            FilterOperator.EQUALS: "$eq",
            FilterOperator.NOT_EQUALS: "$ne",
            FilterOperator.GREATER_THAN: "$gt",
            FilterOperator.GREATER_OR_EQUAL: "$gte",
            FilterOperator.LESS_THAN: "$lt",
            FilterOperator.LESS_OR_EQUAL: "$lte",
            FilterOperator.IN: "$in",
        }
        selector = {}
        for item in plan.filters:
            field = item.field.split("::", 1)[-1]
            if item.operator == FilterOperator.BETWEEN:
                if not isinstance(item.value, (list, tuple)) or len(item.value) != 2:
                    raise QueryPlanningError("BETWEEN 过滤需要两个边界值")
                selector[field] = {"$gte": item.value[0], "$lte": item.value[1]}
            elif item.operator == FilterOperator.CONTAINS:
                selector[field] = {"$regex": str(item.value)}
            else:
                selector[field] = {operators[item.operator]: item.value}
        QueryGenerator._add_document_time_filter(selector, plan, None)
        payload: dict[str, Any] = {"selector": selector, "limit": plan.limit}
        if plan.output_fields:
            payload["fields"] = [item.split("::", 1)[-1] for item in plan.output_fields]
        return payload

    @staticmethod
    def _cypher(plan: QueryPlan, data_object: DataObjectProfile) -> str:
        label = data_object.name.replace("`", "``")
        returns = ", ".join(
            f"n.`{QueryGenerator._native_field(field, data_object).replace('`', '``')}`"
            for field in plan.output_fields
        )
        query = f"MATCH (n:`{label}`)"
        if plan.filters:
            operators = {
                FilterOperator.EQUALS: "=",
                FilterOperator.NOT_EQUALS: "<>",
                FilterOperator.GREATER_THAN: ">",
                FilterOperator.GREATER_OR_EQUAL: ">=",
                FilterOperator.LESS_THAN: "<",
                FilterOperator.LESS_OR_EQUAL: "<=",
                FilterOperator.CONTAINS: "CONTAINS",
            }
            clauses = []
            for item in plan.filters:
                if item.operator not in operators:
                    raise QueryPlanningError("Cypher 第一版不支持该过滤操作")
                field = QueryGenerator._native_field(item.field, data_object).replace("`", "``")
                clauses.append(f"n.`{field}` {operators[item.operator]} {_literal(item.value)}")
            query += " WHERE " + " AND ".join(clauses)
        return f"{query} RETURN {returns or 'n'} LIMIT {plan.limit}"

    @staticmethod
    def _flux(plan: QueryPlan, data_object: DataObjectProfile) -> str:
        if plan.time_range is None:
            raise QueryPlanningError("Flux 查询必须提供明确时间范围")
        measurement = data_object.name.replace('"', '\\"')
        start = plan.time_range.start
        end = plan.time_range.end
        if start is None:
            raise QueryPlanningError("Flux 查询必须提供开始时间")
        range_arguments = f"start: {start.isoformat()}"
        if end is not None:
            range_arguments += f", stop: {end.isoformat()}"
        return (
            'from(bucket: "__configured_bucket__") '
            f"|> range({range_arguments}) "
            f'|> filter(fn: (r) => r._measurement == "{measurement}") '
            f"|> limit(n: {plan.limit})"
        )

    @staticmethod
    def _search(plan: QueryPlan, data_object: DataObjectProfile) -> dict[str, Any]:
        clauses: list[dict[str, Any]] = []
        range_operators = {
            FilterOperator.GREATER_THAN: "gt",
            FilterOperator.GREATER_OR_EQUAL: "gte",
            FilterOperator.LESS_THAN: "lt",
            FilterOperator.LESS_OR_EQUAL: "lte",
        }
        for item in plan.filters:
            field = QueryGenerator._native_field(item.field, data_object)
            if item.operator == FilterOperator.EQUALS:
                clauses.append({"term": {field: item.value}})
            elif item.operator in range_operators:
                clauses.append({"range": {field: {range_operators[item.operator]: item.value}}})
            elif item.operator == FilterOperator.CONTAINS:
                clauses.append({"match": {field: item.value}})
            else:
                raise QueryPlanningError("搜索数据库第一版不支持该过滤操作")
        if plan.time_range and plan.time_field:
            field = QueryGenerator._native_field(plan.time_field, data_object)
            bounds = {}
            if plan.time_range.start is not None:
                bounds["gte"] = plan.time_range.start.isoformat()
            if plan.time_range.end is not None:
                bounds["lte"] = plan.time_range.end.isoformat()
            clauses.append({"range": {field: bounds}})
        payload: dict[str, Any] = {
            "index": data_object.name,
            "query": {"bool": {"filter": clauses}} if clauses else {"match_all": {}},
            "size": plan.limit,
        }
        if plan.output_fields:
            payload["_source"] = [
                QueryGenerator._native_field(item, data_object) for item in plan.output_fields
            ]
        return payload

    @staticmethod
    def _add_document_time_filter(
        target: dict[str, Any], plan: QueryPlan, data_object: DataObjectProfile | None
    ) -> None:
        if plan.time_range is None or plan.time_field is None:
            return
        field = (
            QueryGenerator._native_field(plan.time_field, data_object)
            if data_object is not None
            else plan.time_field.split("::", 1)[-1]
        )
        bounds = {}
        if plan.time_range.start is not None:
            bounds["$gte"] = plan.time_range.start.isoformat()
        if plan.time_range.end is not None:
            bounds["$lte"] = plan.time_range.end.isoformat()
        target[field] = bounds

    @staticmethod
    def _graphql(plan: QueryPlan, data_object: DataObjectProfile) -> str:
        fields = (
            " ".join(
                QueryGenerator._native_field(field, data_object) for field in plan.output_fields
            )
            or "_additional { id }"
        )
        return f"{{ Get {{ {data_object.name}(limit: {plan.limit}) {{ {fields} }} }} }}"

    @staticmethod
    def _native_field(reference: str, data_object: DataObjectProfile) -> str:
        object_id, separator, field = reference.partition("::")
        if separator and object_id != data_object.id:
            raise QueryPlanningError("字段引用不属于目标数据对象")
        return field if separator else reference
