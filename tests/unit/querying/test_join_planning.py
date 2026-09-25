from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from examples.create_demo_database import create_demo_database
from qaneris.adapters.relational.sqlite import SQLiteAdapter
from qaneris.common.errors import QueryPlanningError
from qaneris.contracts import Datasource
from qaneris.querying.intent.join_intent import parse_join_intent
from qaneris.querying.planning.simple_planner import plan_query


@pytest.fixture
def scanned_demo(tmp_path: Path):
    database = create_demo_database(tmp_path / "sales.db")
    adapter = SQLiteAdapter("ds_sales", {"path": str(database)})
    datasource = Datasource(
        id="ds_sales",
        name="sales",
        kind="relational",
        workspace_id="default",
        status="ready",
    )
    return datasource, adapter, adapter.scan_metadata(), adapter.scan_relations()


@pytest.mark.parametrize(
    ("question", "expected_rows"),
    [
        (
            "已完成订单按商品分类统计销售额，哪个分类最高？",
            [
                {"category": "电脑", "total_amount": 185691.55},
                {"category": "音频", "total_amount": 53589.6},
                {"category": "配件", "total_amount": 22641.3},
                {"category": "存储", "total_amount": 10307.1},
            ],
        ),
        (
            "各地区已完成订单的销售额分别是多少？",
            [
                {"region": "华北", "total_amount": 72714.45},
                {"region": "西部", "total_amount": 59087.3},
                {"region": "华东", "total_amount": 57584.4},
                {"region": "华南", "total_amount": 53837.1},
                {"region": "华中", "total_amount": 29006.3},
            ],
        ),
        (
            "已完成订单中销量最高的 5 个商品是什么？",
            [
                {"name": "会议麦克风", "quantity": 30},
                {"name": "商务显示器", "quantity": 26},
                {"name": "轻薄笔记本", "quantity": 23},
                {"name": "降噪耳机", "quantity": 21},
                {"name": "无线键盘", "quantity": 17},
            ],
        ),
    ],
)
def test_join_contract_executes_through_query_plan(
    scanned_demo, question: str, expected_rows: list[dict[str, Any]]
) -> None:
    datasource, adapter, datasets, relations = scanned_demo

    step = plan_query(question, datasource, datasets, relations)
    result = adapter.execute(step.query)
    normalized_rows = [
        {key: round(value, 2) if isinstance(value, float) else value for key, value in row.items()}
        for row in result.rows
    ]

    assert normalized_rows == expected_rows
    assert " JOIN " in step.query
    assert step.query_language == "sql"


def test_join_plan_requires_scanned_foreign_key(scanned_demo) -> None:
    datasource, _, datasets, relations = scanned_demo
    without_product_relation = [
        relation
        for relation in relations
        if not (relation.from_dataset == "order_items" and relation.to_dataset == "products")
    ]

    with pytest.raises(QueryPlanningError, match="缺少查询所需外键关系"):
        plan_query(
            "已完成订单按商品分类统计销售额",
            datasource,
            datasets,
            without_product_relation,
        )


def test_join_plan_requires_catalog_fields(scanned_demo) -> None:
    datasource, _, datasets, relations = scanned_demo
    products = next(dataset for dataset in datasets if dataset.name == "products")
    products.fields = [field for field in products.fields if field.name != "category"]

    with pytest.raises(ValueError, match="products.category"):
        plan_query(
            "已完成订单按商品分类统计销售额",
            datasource,
            datasets,
            relations,
        )


def test_top_product_limit_respects_request_limit(scanned_demo) -> None:
    datasource, _, datasets, relations = scanned_demo

    step = plan_query(
        "已完成订单中销量最高的 20 个商品是什么？",
        datasource,
        datasets,
        relations,
        max_rows=3,
    )

    assert step.query.endswith("LIMIT 3")


def test_unrelated_question_does_not_create_join_intent() -> None:
    assert parse_join_intent("orders 的总记录数是多少？") is None
