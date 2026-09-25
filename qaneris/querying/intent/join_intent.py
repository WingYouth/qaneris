"""Join-oriented query intent parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FieldReference:
    dataset: str
    field: str


@dataclass(frozen=True)
class RelationRequirement:
    from_dataset: str
    from_field: str
    to_dataset: str
    to_field: str


@dataclass(frozen=True)
class QueryFilter:
    field: FieldReference
    value: str


@dataclass(frozen=True)
class JoinQueryIntent:
    base_dataset: str
    dimension: FieldReference
    metric: FieldReference
    metric_alias: str
    relations: tuple[RelationRequirement, ...]
    filters: tuple[QueryFilter, ...] = ()
    group_fields: tuple[FieldReference, ...] = ()
    order_descending: bool = True
    secondary_order: FieldReference | None = None
    limit: int | None = None


def parse_join_intent(question: str, max_rows: int = 200) -> JoinQueryIntent | None:
    """Recognize only the foreign-key-backed multi-table questions in the contract."""
    lowered = question.lower()
    completed_filter = (QueryFilter(FieldReference("orders", "status"), "completed"),)
    order_items_to_products = RelationRequirement("order_items", "product_id", "products", "id")
    order_items_to_orders = RelationRequirement("order_items", "order_id", "orders", "id")

    if "商品分类" in lowered and _mentions_sales(lowered):
        dimension = FieldReference("products", "category")
        return JoinQueryIntent(
            base_dataset="order_items",
            dimension=dimension,
            metric=FieldReference("order_items", "line_amount"),
            metric_alias="total_amount",
            relations=(order_items_to_products, order_items_to_orders),
            filters=completed_filter if _mentions_completed(lowered) else (),
            group_fields=(dimension,),
        )

    if "地区" in lowered and _mentions_sales(lowered):
        dimension = FieldReference("customers", "region")
        return JoinQueryIntent(
            base_dataset="orders",
            dimension=dimension,
            metric=FieldReference("orders", "total_amount"),
            metric_alias="total_amount",
            relations=(RelationRequirement("orders", "customer_id", "customers", "id"),),
            filters=completed_filter if _mentions_completed(lowered) else (),
            group_fields=(dimension,),
        )

    if "商品" in lowered and any(token in lowered for token in ("销量", "销售数量")):
        dimension = FieldReference("products", "name")
        return JoinQueryIntent(
            base_dataset="order_items",
            dimension=dimension,
            metric=FieldReference("order_items", "quantity"),
            metric_alias="quantity",
            relations=(order_items_to_products, order_items_to_orders),
            filters=completed_filter if _mentions_completed(lowered) else (),
            group_fields=(FieldReference("products", "id"), dimension),
            secondary_order=dimension,
            limit=_parse_limit(lowered, max_rows),
        )

    return None


def _mentions_sales(question: str) -> bool:
    return any(token in question for token in ("销售额", "销售总额", "金额"))


def _mentions_completed(question: str) -> bool:
    return "已完成" in question or "completed" in question


def _parse_limit(question: str, max_rows: int) -> int | None:
    match = re.search(r"(?:top|前|最高的)\s*(\d+)", question)
    return min(int(match.group(1)), max_rows) if match else None
