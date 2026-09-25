import pytest

from qaneris.contracts import DatasetInfo, Datasource, FieldInfo
from qaneris.querying.planning.simple_planner import plan_query, plan_sql


@pytest.fixture
def sales_dataset() -> DatasetInfo:
    return DatasetInfo(
        datasource_id="ds_sales",
        name="orders",
        fields=[
            FieldInfo(name="id", data_type="INTEGER", primary_key=True),
            FieldInfo(name="amount", data_type="REAL"),
        ],
    )


@pytest.fixture
def dated_sales_datasets() -> list[DatasetInfo]:
    return [
        DatasetInfo(
            datasource_id="ds_sales",
            name="customers",
            fields=[FieldInfo(name="id", data_type="INTEGER", primary_key=True)],
        ),
        DatasetInfo(
            datasource_id="ds_sales",
            name="orders",
            fields=[
                FieldInfo(name="id", data_type="INTEGER", primary_key=True),
                FieldInfo(name="order_date", data_type="TEXT"),
                FieldInfo(name="status", data_type="TEXT"),
                FieldInfo(name="total_amount", data_type="REAL"),
            ],
        ),
    ]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("orders 有多少条", 'SELECT COUNT(*) AS count FROM "orders"'),
        ("orders amount 平均值", 'SELECT AVG("amount") AS average_amount FROM "orders"'),
        ("orders amount 总额", 'SELECT SUM("amount") AS total_amount FROM "orders"'),
        ("orders amount 最大值", 'SELECT MAX("amount") AS max_amount FROM "orders"'),
        ("orders amount 最小值", 'SELECT MIN("amount") AS min_amount FROM "orders"'),
        ("orders amount 最高的前 3 条", 'ORDER BY "amount" DESC LIMIT 3'),
    ],
)
def test_plan_sql_rules_remain_available(
    sales_dataset: DatasetInfo, question: str, expected: str
) -> None:
    _, query = plan_sql(question, [sales_dataset])

    assert expected in query


def test_plan_query_wraps_sql_plan(sales_dataset: DatasetInfo) -> None:
    datasource = Datasource(
        id="ds_sales",
        name="sales",
        kind="relational",
        workspace_id="default",
    )

    step = plan_query("orders amount 总额", datasource, [sales_dataset], [])

    assert step.datasource_id == datasource.id
    assert step.dataset == "orders"
    assert step.query == 'SELECT SUM("amount") AS total_amount FROM "orders"'
    assert step.query_language == "sql"


def test_plan_query_rejects_unsupported_datasource_kind(
    sales_dataset: DatasetInfo,
) -> None:
    datasource = Datasource(
        id="ds_document",
        name="documents",
        kind="document",
        workspace_id="default",
    )

    with pytest.raises(NotImplementedError, match="kind=document"):
        plan_query("查看数据", datasource, [sales_dataset], [])


def test_plan_sql_resolves_chinese_dataset_alias_and_month_filters(
    dated_sales_datasets: list[DatasetInfo],
) -> None:
    dataset, query = plan_sql("2026 年 7 月已完成的订单有多少笔？", dated_sales_datasets)

    assert dataset == "orders"
    assert query == (
        'SELECT COUNT(*) AS count FROM "orders" WHERE "status" = \'completed\' '
        "AND \"order_date\" BETWEEN '2026-07-01' AND '2026-07-31'"
    )


def test_plan_sql_handles_daily_sales_range(
    dated_sales_datasets: list[DatasetInfo],
) -> None:
    dataset, query = plan_sql(
        "2026 年 7 月 1 日到 5 日每天已完成订单的销售额是多少？",
        dated_sales_datasets,
    )

    assert dataset == "orders"
    assert query == (
        'SELECT "order_date", SUM("total_amount") AS total_amount FROM "orders" '
        "WHERE \"status\" = 'completed' AND \"order_date\" BETWEEN '2026-07-01' "
        'AND \'2026-07-05\' GROUP BY "order_date" ORDER BY "order_date"'
    )
