"""SQL compilation helpers for simple join plans."""

from __future__ import annotations

from qaneris.common.errors import QueryPlanningError
from qaneris.contracts import DatasetInfo, RelationInfo
from qaneris.querying.generation.sql_identifiers import quote_identifier
from qaneris.querying.intent.join_intent import (
    FieldReference,
    JoinQueryIntent,
    RelationRequirement,
)


def compile_join_sql(
    intent: JoinQueryIntent,
    datasets: list[DatasetInfo],
    relations: list[RelationInfo],
) -> str:
    """Compile a bounded single-source join after validating catalog metadata."""
    available = {dataset.name: dataset for dataset in datasets}
    _require_dataset(available, intent.base_dataset)
    aliases = {intent.base_dataset: "t0"}
    joins: list[str] = []

    for requirement in intent.relations:
        _require_relation(requirement, relations)
        _require_field(available, FieldReference(requirement.from_dataset, requirement.from_field))
        _require_field(available, FieldReference(requirement.to_dataset, requirement.to_field))
        joins.append(_compile_join(requirement, aliases))

    _require_field(available, intent.dimension)
    _require_field(available, intent.metric)
    for group_field in intent.group_fields:
        _require_field(available, group_field)
    for query_filter in intent.filters:
        _require_field(available, query_filter.field)
    if intent.secondary_order is not None:
        _require_field(available, intent.secondary_order)

    dimension = _qualified(intent.dimension, aliases)
    metric = _qualified(intent.metric, aliases)
    metric_alias = quote_identifier(intent.metric_alias)
    select = f"SELECT {dimension}, SUM({metric}) AS {metric_alias}"
    from_clause = f" FROM {quote_identifier(intent.base_dataset)} AS {aliases[intent.base_dataset]}"
    where = _compile_filters(intent, aliases)
    group_by = ", ".join(_qualified(field, aliases) for field in intent.group_fields)
    order_direction = "DESC" if intent.order_descending else "ASC"
    order_by = f" ORDER BY {metric_alias} {order_direction}"
    if intent.secondary_order is not None:
        order_by += f", {_qualified(intent.secondary_order, aliases)}"
    limit = f" LIMIT {intent.limit}" if intent.limit is not None else ""
    return (
        select + from_clause + "".join(joins) + where + f" GROUP BY {group_by}" + order_by + limit
    )


def _compile_join(requirement: RelationRequirement, aliases: dict[str, str]) -> str:
    from_alias = aliases.get(requirement.from_dataset)
    to_alias = aliases.get(requirement.to_dataset)
    if from_alias is not None and to_alias is None:
        to_alias = f"t{len(aliases)}"
        aliases[requirement.to_dataset] = to_alias
        return (
            f" JOIN {quote_identifier(requirement.to_dataset)} AS {to_alias} ON "
            f"{to_alias}.{quote_identifier(requirement.to_field)} = "
            f"{from_alias}.{quote_identifier(requirement.from_field)}"
        )
    if to_alias is not None and from_alias is None:
        from_alias = f"t{len(aliases)}"
        aliases[requirement.from_dataset] = from_alias
        return (
            f" JOIN {quote_identifier(requirement.from_dataset)} AS {from_alias} ON "
            f"{from_alias}.{quote_identifier(requirement.from_field)} = "
            f"{to_alias}.{quote_identifier(requirement.to_field)}"
        )
    if from_alias is not None and to_alias is not None:
        return ""
    raise QueryPlanningError(
        "Join relation is disconnected from the current plan: "
        f"{requirement.from_dataset}.{requirement.from_field}"
    )


def _compile_filters(intent: JoinQueryIntent, aliases: dict[str, str]) -> str:
    conditions = [
        f"{_qualified(query_filter.field, aliases)} = {_quote_literal(query_filter.value)}"
        for query_filter in intent.filters
    ]
    return f" WHERE {' AND '.join(conditions)}" if conditions else ""


def _qualified(field: FieldReference, aliases: dict[str, str]) -> str:
    alias = aliases.get(field.dataset)
    if alias is None:
        raise QueryPlanningError(f"数据集未加入查询计划: {field.dataset}")
    return f"{alias}.{quote_identifier(field.field)}"


def _require_dataset(available: dict[str, DatasetInfo], dataset_name: str) -> DatasetInfo:
    dataset = available.get(dataset_name)
    if dataset is None:
        raise QueryPlanningError(f"缺少查询所需数据集: {dataset_name}")
    return dataset


def _require_field(available: dict[str, DatasetInfo], field: FieldReference) -> None:
    dataset = _require_dataset(available, field.dataset)
    if not any(item.name == field.field for item in dataset.fields):
        raise QueryPlanningError(f"缺少查询所需字段: {field.dataset}.{field.field}")


def _require_relation(requirement: RelationRequirement, relations: list[RelationInfo]) -> None:
    found = any(
        relation.from_dataset == requirement.from_dataset
        and relation.from_field == requirement.from_field
        and relation.to_dataset == requirement.to_dataset
        and relation.to_field == requirement.to_field
        and relation.relation_type == "foreign_key"
        for relation in relations
    )
    if not found:
        raise QueryPlanningError(
            "缺少查询所需外键关系: "
            f"{requirement.from_dataset}.{requirement.from_field} -> "
            f"{requirement.to_dataset}.{requirement.to_field}"
        )


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
