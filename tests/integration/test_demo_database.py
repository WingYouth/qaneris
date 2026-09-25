from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from examples.create_demo_database import create_demo_database
from qaneris.adapters.relational.sqlite import SQLiteAdapter
from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.contracts import AskRequest, DatasourceCreate

EXAMPLES_DIR = Path(__file__).parents[2] / "examples"
QUESTION_CONTRACT = EXAMPLES_DIR / "questions_2026_07.json"


def load_cases() -> list[dict[str, Any]]:
    contract = json.loads(QUESTION_CONTRACT.read_text(encoding="utf-8"))
    return contract["cases"]


def rows_for(connection: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query).fetchall()]


def normalize_value(value: Any) -> Any:
    return round(value, 2) if isinstance(value, float) else value


@pytest.fixture
def demo_database(tmp_path: Path) -> Path:
    return create_demo_database(tmp_path / "sales.db")


def test_demo_database_has_consistent_sales_data(demo_database: Path) -> None:
    with sqlite3.connect(demo_database) as connection:
        connection.row_factory = sqlite3.Row
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("customers", "products", "orders", "order_items")
        }
        date_range = connection.execute(
            "SELECT MIN(order_date), MAX(order_date), COUNT(DISTINCT order_date) FROM orders"
        ).fetchone()
        mismatched_totals = connection.execute(
            "SELECT COUNT(*) FROM orders AS o WHERE ABS(o.total_amount - "
            "(SELECT SUM(i.line_amount) FROM order_items AS i WHERE i.order_id=o.id)) > 0.001"
        ).fetchone()[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert counts == {"customers": 10, "products": 8, "orders": 62, "order_items": 124}
    assert tuple(date_range) == ("2026-07-01", "2026-07-31", 31)
    assert mismatched_totals == 0
    assert foreign_key_errors == []


def test_question_contract_matches_demo_database(demo_database: Path) -> None:
    with sqlite3.connect(demo_database) as connection:
        connection.row_factory = sqlite3.Row
        for case in load_cases():
            assert rows_for(connection, case["verification_sql"]) == case["expected_rows"], case[
                "id"
            ]


def test_demo_database_generation_is_safe_and_repeatable(tmp_path: Path) -> None:
    database_path = create_demo_database(tmp_path / "sales.db")
    with pytest.raises(FileExistsError):
        create_demo_database(database_path)

    create_demo_database(database_path, overwrite=True)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 62


def test_sqlite_adapter_scans_demo_database(demo_database: Path) -> None:
    adapter = SQLiteAdapter("ds_demo", {"path": str(demo_database)})

    datasets = adapter.scan_metadata()
    relations = adapter.scan_relations()

    assert {item.name for item in datasets} == {
        "customers",
        "products",
        "orders",
        "order_items",
    }
    assert len(relations) == 3
    assert all(item.relation_type == "foreign_key" for item in relations)


def test_current_verification_sql_contract_runs_through_service(
    demo_database: Path, tmp_path: Path
) -> None:
    service = QanerisService(Catalog(tmp_path / "catalog.db"))
    datasource = service.create_datasource(
        DatasourceCreate(
            name="sales_2026_07",
            kind="relational",
            connection={"driver": "sqlite", "path": str(demo_database)},
        )
    )
    service.scan_datasource(datasource.id)

    for case in (item for item in load_cases() if item["supported_now"]):
        response = service.ask(AskRequest(question=case["question"], datasource_id=datasource.id,
                                          sql=case["verification_sql"]))
        expected = case["expected_rows"]
        projected_rows = [
            {key: normalize_value(row[key]) for key in expected_row}
            for row, expected_row in zip(response.result.rows, expected, strict=True)
        ]
        assert projected_rows == expected, case["id"]
