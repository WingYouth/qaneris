"""Simple query planner retained for the SQLite demo flow."""

from __future__ import annotations

import calendar
import re
from datetime import date

from smartdata.common.errors import QueryPlanningError
from smartdata.contracts import (
    DatasetInfo,
    Datasource,
    DatasourceKind,
    FieldInfo,
    QueryPlanStep,
    RelationInfo,
)
from smartdata.querying.generation.sql_compiler import compile_join_sql
from smartdata.querying.generation.sql_identifiers import quote_identifier
from smartdata.querying.intent.join_intent import parse_join_intent


def _select_dataset(question: str, datasets: list[DatasetInfo]) -> DatasetInfo:
    lowered = question.lower()
    explicit = next((item for item in datasets if item.name.lower() in lowered), None)
    if explicit is not None:
        return explicit
    aliases = (
        ("订单明细", "order_items"),
        ("订单", "orders"),
        ("客户", "customers"),
        ("商品", "products"),
    )
    names = {item.name.lower(): item for item in datasets}
    for alias, dataset_name in aliases:
        if alias in lowered and dataset_name in names:
            return names[dataset_name]
    if len(datasets) == 1:
        return datasets[0]
    raise QueryPlanningError("无法确定问题对应的数据集，请在问题中包含数据集或业务实体名称")


def _numeric_fields(dataset: DatasetInfo) -> list[FieldInfo]:
    numeric_types = ("int", "real", "numeric", "decimal", "float")
    return [
        field
        for field in dataset.fields
        if any(token in field.data_type.lower() for token in numeric_types)
    ]


def _select_metric(question: str, dataset: DatasetInfo) -> FieldInfo | None:
    lowered = question.lower()
    numeric = _numeric_fields(dataset)
    explicit = next((field for field in numeric if field.name.lower() in lowered), None)
    if explicit is not None:
        return explicit
    if any(
        token in lowered for token in ("销售额", "销售总额", "总额", "金额", "sales", "revenue")
    ):
        amount = next((field for field in numeric if "amount" in field.name.lower()), None)
        if amount is not None:
            return amount
    return numeric[0] if len(numeric) == 1 else None


def _field_named(dataset: DatasetInfo, name: str) -> FieldInfo | None:
    return next((field for field in dataset.fields if field.name.lower() == name), None)


def _date_range(question: str) -> tuple[str, str] | None:
    day_range = re.search(
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:到|至)\s*"
        r"(?:(\d{4})\s*年\s*)?(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*日",
        question,
    )
    if day_range:
        start_year, start_month, start_day = map(int, day_range.group(1, 2, 3))
        end_year = int(day_range.group(4) or start_year)
        end_month = int(day_range.group(5) or start_month)
        end_day = int(day_range.group(6))
        return (
            date(start_year, start_month, start_day).isoformat(),
            date(end_year, end_month, end_day).isoformat(),
        )
    month = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", question)
    if month:
        year, month_number = map(int, month.group(1, 2))
        last_day = calendar.monthrange(year, month_number)[1]
        return date(year, month_number, 1).isoformat(), date(
            year, month_number, last_day
        ).isoformat()
    return None


def _where_clause(question: str, dataset: DatasetInfo) -> str:
    lowered = question.lower()
    conditions: list[str] = []
    status_field = _field_named(dataset, "status")
    status_values = (
        (("已完成", "completed"), "completed"),
        (("待处理", "pending"), "pending"),
        (("已取消", "cancelled"), "cancelled"),
    )
    requested_status = next(
        (value for tokens, value in status_values if any(token in lowered for token in tokens)),
        None,
    )
    if requested_status is not None and status_field is None:
        raise QueryPlanningError(f"数据集 {dataset.name} 缺少状态字段 status")
    if status_field is not None and requested_status is not None:
        for tokens, value in status_values:
            if any(token in lowered for token in tokens):
                conditions.append(f"{quote_identifier(status_field.name)} = '{value}'")
                break
    date_field = _field_named(dataset, "order_date")
    requested_range = _date_range(question)
    if requested_range is not None and date_field is None:
        raise QueryPlanningError(f"数据集 {dataset.name} 缺少日期字段 order_date")
    if date_field is not None and requested_range is not None:
        start, end = requested_range
        conditions.append(f"{quote_identifier(date_field.name)} BETWEEN '{start}' AND '{end}'")
    return f" WHERE {' AND '.join(conditions)}" if conditions else ""


def plan_sql(question: str, datasets: list[DatasetInfo], max_rows: int = 200) -> tuple[str, str]:
    if not datasets:
        raise QueryPlanningError("数据源尚未扫描到可用数据集")
    lowered = question.lower()
    dataset = _select_dataset(question, datasets)
    table = quote_identifier(dataset.name)
    metric = _select_metric(question, dataset)
    where = _where_clause(question, dataset)
    limit_match = re.search(r"(?:top|前)\s*(\d+)", lowered)
    limit = min(int(limit_match.group(1)), max_rows) if limit_match else min(20, max_rows)

    date_field = _field_named(dataset, "order_date")
    if (
        date_field is not None
        and metric is not None
        and any(token in lowered for token in ("每天", "每日", "按日"))
    ):
        date_column = quote_identifier(date_field.name)
        metric_column = quote_identifier(metric.name)
        query = (
            f"SELECT {date_column}, SUM({metric_column}) AS total_amount FROM {table} "
            f"{where.strip()} GROUP BY {date_column} ORDER BY {date_column}"
        )
        return dataset.name, query

    if any(token in lowered for token in ("多少笔", "多少条", "记录数", "数量", "count", "几条")):
        return dataset.name, f"SELECT COUNT(*) AS count FROM {table}{where}"

    if metric and any(token in lowered for token in ("平均", "average", "avg")):
        field = quote_identifier(metric.name)
        return (
            dataset.name,
            f"SELECT AVG({field}) AS average_{metric.name} FROM {table}{where}",
        )
    if metric and any(token in lowered for token in ("总", "合计", "sum")):
        field = quote_identifier(metric.name)
        return dataset.name, f"SELECT SUM({field}) AS total_{metric.name} FROM {table}{where}"
    if metric and any(token in lowered for token in ("最大值", "maximum", "max")):
        field = quote_identifier(metric.name)
        return dataset.name, f"SELECT MAX({field}) AS max_{metric.name} FROM {table}{where}"
    if metric and any(token in lowered for token in ("最小值", "minimum", "min")):
        field = quote_identifier(metric.name)
        return dataset.name, f"SELECT MIN({field}) AS min_{metric.name} FROM {table}{where}"
    if metric and (limit_match or any(token in lowered for token in ("最高", "最多", "top"))):
        field = quote_identifier(metric.name)
        return dataset.name, f"SELECT * FROM {table}{where} ORDER BY {field} DESC LIMIT {limit}"
    if any(
        token in lowered
        for token in ("平均", "average", "avg", "总额", "合计", "sum", "最大值", "最小值")
    ):
        raise QueryPlanningError(f"无法确定数据集 {dataset.name} 中要计算的数值字段")
    return dataset.name, f"SELECT * FROM {table}{where} LIMIT {limit}"


def plan_query(
    question: str,
    datasource: Datasource,
    datasets: list[DatasetInfo],
    relations: list[RelationInfo] | None = None,
    max_rows: int = 200,
) -> QueryPlanStep:
    if datasource.kind != DatasourceKind.RELATIONAL:
        raise NotImplementedError(
            f"Query planning is not implemented for kind={datasource.kind.value}"
        )
    join_intent = parse_join_intent(question, max_rows)
    if join_intent is not None:
        query = compile_join_sql(join_intent, datasets, relations or [])
        dataset_name = join_intent.base_dataset
    else:
        dataset_name, query = plan_sql(question, datasets, max_rows)
    return QueryPlanStep(
        id="q1",
        datasource_id=datasource.id,
        dataset=dataset_name,
        purpose=f"回答：{question}",
        query=query,
        query_language="sql",
    )
