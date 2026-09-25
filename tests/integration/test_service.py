import sqlite3
from pathlib import Path

from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.contracts import AskRequest, DatasourceCreate


def create_sales_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER REFERENCES customers(id),
                amount REAL NOT NULL
            );
            INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob');
            INSERT INTO orders VALUES (1, 1, 120.5), (2, 2, 80), (3, 1, 200);
            """
        )


def test_scan_and_ask(tmp_path: Path) -> None:
    source_path = tmp_path / "sales.db"
    create_sales_db(source_path)
    service = QanerisService(Catalog(tmp_path / "catalog.db"))
    datasource = service.create_datasource(
        DatasourceCreate(
            name="sales",
            kind="relational",
            connection={"driver": "sqlite", "path": str(source_path)},
        )
    )

    datasets = service.scan_datasource(datasource.id)

    snapshot = service.catalog.get_active_snapshot(datasource.id)
    assert snapshot is not None
    document_path = Path(snapshot.document_path or "")
    assert document_path == tmp_path / "profiles" / "sales"
    assert snapshot.datasource_id not in document_path.parts
    assert snapshot.id not in document_path.parts
    documents = list(document_path.rglob("*.md"))
    assert [item.name for item in documents] == ["database_profile.md"]
    database_document = documents[0].read_text(encoding="utf-8")
    assert f'datasource_id: "{snapshot.datasource_id}"' in database_document
    assert 'datasource_name: "sales"' in database_document
    assert f'snapshot_id: "{snapshot.id}"' in database_document
    assert f"snapshot_version: {snapshot.version}" in database_document
    assert 'database_kind: "relational"' in database_document
    assert 'database_category: "relational"' in database_document
    assert "# 数据库总览：sales" in database_document
    assert "该数据库主要承载客户管理、订单交易相关数据" in database_document
    assert "保存客户或用户资料" in database_document
    assert "保存订单及交易过程信息" in database_document
    assert "## 主要数据关系" in database_document
    assert "orders | customer_id | customers | id" in database_document
    assert "## 完整字段目录" in database_document
    assert "字段样例上限：每个字段最多 10 个" in database_document

    samples = service.catalog.list_samples(datasource_id=datasource.id)
    assert samples
    assert all(len(sample.rows) <= 3 for sample in samples)
    assert {item.name for item in datasets} == {"customers", "orders"}
    orders = next(item for item in datasets if item.name == "orders")
    assert {field.name for field in orders.fields} == {"id", "customer_id", "amount"}
    relations = service.catalog.list_relations(datasource_id=datasource.id)
    assert len(relations) == 1
    assert relations[0].from_dataset == "orders"
    assert relations[0].from_field == "customer_id"
    assert relations[0].to_dataset == "customers"
    assert relations[0].to_field == "id"
    assert relations[0].relation_type == "foreign_key"
    assert relations[0].source == "scan"

    response = service.ask(
        AskRequest(
            question="orders 中 amount 最高的前 2 条记录",
            datasource_id=datasource.id,
            sql='SELECT * FROM "orders" ORDER BY "amount" DESC LIMIT 2',
        )
    )
    assert [row["amount"] for row in response.result.rows] == [200.0, 120.5]
    assert response.plan[0].query.endswith('ORDER BY "amount" DESC LIMIT 2')
    assert response.plan[0].query_language == "sql"


def test_explicit_sql_is_still_validated(tmp_path: Path) -> None:
    source_path = tmp_path / "sales.db"
    create_sales_db(source_path)
    service = QanerisService(Catalog(tmp_path / "catalog.db"))
    datasource = service.create_datasource(
        DatasourceCreate(
            name="sales",
            kind="relational",
            connection={"driver": "sqlite", "path": str(source_path)},
        )
    )
    service.scan_datasource(datasource.id)

    response = service.ask(
        AskRequest(
            question="订单总额",
            datasource_id=datasource.id,
            sql="SELECT SUM(amount) total FROM orders",
        )
    )
    assert response.result.rows == [{"total": 400.5}]
