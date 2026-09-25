"""Query plan construction."""

from __future__ import annotations

import uuid

from smartdata.common.errors import QueryPlanningError
from smartdata.contracts.profile import DataObjectProfile, RelationshipProfile
from smartdata.contracts.query import (
    AggregateFunction,
    AggregateSpec,
    QueryContext,
    QueryLanguage,
    QueryPlan,
    QueryResultType,
    RetrievedDataObject,
    SortSpec,
)

_LANGUAGES = {
    "sqlite": QueryLanguage.SQL,
    "postgresql": QueryLanguage.SQL,
    "mysql": QueryLanguage.SQL,
    "oracle": QueryLanguage.SQL,
    "sqlserver": QueryLanguage.SQL,
    "timescaledb": QueryLanguage.SQL,
    "clickhouse": QueryLanguage.SQL,
    "snowflake": QueryLanguage.SQL,
    "bigquery": QueryLanguage.SQL,
    "redis": QueryLanguage.REDIS_JSON,
    "mongodb": QueryLanguage.MONGODB_JSON,
    "couchdb": QueryLanguage.COUCHDB_MANGO,
    "cassandra": QueryLanguage.CQL,
    "hbase": QueryLanguage.HBASE_JSON,
    "neo4j": QueryLanguage.CYPHER,
    "influxdb": QueryLanguage.FLUX,
    "elasticsearch": QueryLanguage.SEARCH_JSON,
    "opensearch": QueryLanguage.SEARCH_JSON,
    "milvus": QueryLanguage.VECTOR_FILTER_JSON,
    "qdrant": QueryLanguage.VECTOR_FILTER_JSON,
    "weaviate": QueryLanguage.GRAPHQL,
}
_RESULT_TYPES = {
    "key_value": QueryResultType.KEY_VALUE,
    "document": QueryResultType.DOCUMENT,
    "wide_column": QueryResultType.WIDE_COLUMN,
    "graph": QueryResultType.GRAPH,
    "time_series": QueryResultType.TIME_SERIES,
    "search": QueryResultType.SEARCH,
    "vector": QueryResultType.VECTOR,
}
_NUMERIC_TOKENS = ("int", "float", "double", "decimal", "numeric", "number", "real")


class RuleBasedQueryPlanner:
    def plan(self, context: QueryContext, max_rows: int = 200) -> QueryPlan:
        if not context.data_sources:
            raise QueryPlanningError("查询上下文没有召回可用数据源")
        source = context.data_sources[0]
        if not source.data_objects:
            raise QueryPlanningError("查询上下文没有召回可用数据对象")
        try:
            language = _LANGUAGES[source.driver]
        except KeyError as error:
            raise QueryPlanningError(f"数据源驱动尚无查询生成器: {source.driver}") from error
        if language == QueryLanguage.SQL:
            objects, relationships = self._connected_objects(source.data_objects, context)
        else:
            objects, relationships = [source.data_objects[0].profile], []
        aggregates = [
            self._resolve_aggregate(item, context, objects) for item in context.intent.aggregates
        ]
        filters = []
        for item in context.intent.filters:
            resolved = self._resolve_field(item.field, objects)
            if resolved is not None:
                filters.append(item.model_copy(update={"field": resolved}))
        dimensions = self._matched_dimensions(context, objects)
        time_field = (
            self._best_time_field(context.intent.question, objects)
            if context.intent.time_range
            else None
        )
        if context.intent.time_range and time_field is None:
            raise QueryPlanningError("问题包含时间范围，但没有召回可用时间字段")
        output_fields = dimensions or ([] if aggregates else self._default_fields(objects[0]))
        sorts = self._resolve_sorts(context.intent.sorts, aggregates, dimensions)
        return QueryPlan(
            id=f"plan_{uuid.uuid4().hex[:12]}",
            datasource_id=source.datasource_id,
            query_language=language,
            data_object_ids=[item.id for item in objects],
            output_fields=output_fields,
            metrics=[item.field for item in aggregates if item.field],
            dimensions=dimensions,
            relationship_ids=[item.id for item in relationships],
            filters=filters,
            time_range=context.intent.time_range,
            time_field=time_field,
            aggregates=aggregates,
            group_by=dimensions if aggregates else [],
            sorts=sorts,
            limit=min(context.intent.limit or max_rows, max_rows, 1_000),
            expected_result_type=_RESULT_TYPES.get(source.kind.value, QueryResultType.TABULAR),
            rationale=[
                *source.data_objects[0].reasons,
                "第一版仅选择一个数据源，可使用源内已确认关系",
            ],
            unresolved_ambiguities=context.intent.ambiguities,
        )

    def _resolve_aggregate(
        self,
        aggregate: AggregateSpec,
        context: QueryContext,
        data_objects: list[DataObjectProfile],
    ) -> AggregateSpec:
        if aggregate.function == AggregateFunction.COUNT:
            return aggregate
        matched = self._best_field(context.intent.question, data_objects, numeric_only=True)
        if matched is None:
            raise QueryPlanningError("聚合问题没有召回可用的数值字段")
        return aggregate.model_copy(update={"field": matched, "alias": aggregate.alias or "value"})

    def _matched_dimensions(
        self, context: QueryContext, data_objects: list[DataObjectProfile]
    ) -> list[str]:
        question = context.intent.question.lower()
        candidates = [
            (data_object, field.path)
            for data_object in data_objects
            for field in data_object.fields
            if field.path.lower() in question and not self._is_numeric(field.data_type)
        ]
        chosen: dict[str, tuple[DataObjectProfile, str]] = {}
        for data_object, field in candidates:
            current = chosen.get(field.lower())
            if current is None or self._mention_distance(
                question, data_object.name, field
            ) < self._mention_distance(question, current[0].name, current[1]):
                chosen[field.lower()] = (data_object, field)
        return [self._reference(item[0], item[1]) for item in chosen.values()][:3]

    @staticmethod
    def _mention_distance(question: str, object_name: str, field: str) -> tuple[int, int]:
        object_position = question.find(object_name.lower())
        field_position = question.find(field.lower())
        if object_position < 0 or field_position < 0:
            return (2, len(question) + 1)
        return (int(object_position > field_position), abs(object_position - field_position))

    @staticmethod
    def _resolve_sorts(
        sorts: list[SortSpec], aggregates: list[AggregateSpec], dimensions: list[str]
    ) -> list[SortSpec]:
        target = (
            aggregates[0].alias or aggregates[0].field
            if aggregates
            else dimensions[0]
            if dimensions
            else None
        )
        return [item.model_copy(update={"field": target or item.field}) for item in sorts]

    @classmethod
    def _best_field(
        cls,
        question: str,
        data_objects: list[DataObjectProfile],
        *,
        numeric_only: bool,
    ) -> str | None:
        fields = [
            (data_object, field)
            for data_object in data_objects
            for field in data_object.fields
            if not numeric_only or cls._is_numeric(field.data_type)
        ]
        matched = [item for item in fields if item[1].path.lower() in question.lower()]
        chosen = (matched or fields)[0] if fields else None
        return cls._reference(chosen[0], chosen[1].path) if chosen else None

    @classmethod
    def _best_time_field(cls, question: str, data_objects: list[DataObjectProfile]) -> str | None:
        time_tokens = ("date", "time", "timestamp", "日期", "时间")
        fields = [
            (data_object, field)
            for data_object in data_objects
            for field in data_object.fields
            if any(
                token in field.data_type.lower() or token in field.path.lower()
                for token in time_tokens
            )
        ]
        matched = [item for item in fields if item[1].path.lower() in question.lower()]
        chosen = (matched or fields)[0] if fields else None
        return cls._reference(chosen[0], chosen[1].path) if chosen else None

    @classmethod
    def _resolve_field(cls, reference: str, data_objects: list[DataObjectProfile]) -> str | None:
        lowered = reference.lower()
        matches = [
            (data_object, field)
            for data_object in data_objects
            for field in data_object.fields
            if field.path.lower() == lowered or field.name.lower() == lowered.rsplit(".", 1)[-1]
        ]
        return cls._reference(matches[0][0], matches[0][1].path) if len(matches) == 1 else None

    @staticmethod
    def _reference(data_object: DataObjectProfile, field: str) -> str:
        return f"{data_object.id}::{field}"

    @classmethod
    def _default_fields(cls, data_object: DataObjectProfile) -> list[str]:
        return [cls._reference(data_object, field.path) for field in data_object.fields[:10]]

    @staticmethod
    def _connected_objects(
        retrieved: list[RetrievedDataObject], context: QueryContext
    ) -> tuple[list[DataObjectProfile], list[RelationshipProfile]]:
        selected = [retrieved[0].profile]
        relationships: list[RelationshipProfile] = []
        candidates = {item.profile.id: item.profile for item in retrieved[1:]}
        progress = True
        while progress:
            progress = False
            selected_ids = {item.id for item in selected}
            for relation in context.relationships:
                if relation.from_datasource_id != relation.to_datasource_id:
                    continue
                endpoints = {relation.from_object_id, relation.to_object_id}
                missing = endpoints - selected_ids
                if endpoints & selected_ids and len(missing) == 1:
                    object_id = next(iter(missing))
                    if object_id in candidates:
                        selected.append(candidates.pop(object_id))
                        relationships.append(relation)
                        progress = True
        return selected, relationships

    @staticmethod
    def _is_numeric(data_type: str) -> bool:
        return any(token in data_type.lower() for token in _NUMERIC_TOKENS)
