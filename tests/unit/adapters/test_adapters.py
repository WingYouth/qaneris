import sqlite3
from pathlib import Path

import pytest

from smartdata.adapters.base import DataSourceAdapter
from smartdata.adapters.factory import create_adapter
from smartdata.adapters.registry import get_adapter_class, register_adapter
from smartdata.adapters.relational.sqlite import SQLiteAdapter
from smartdata.contracts import DatasetInfo, NormalizedResult, RelationInfo
from smartdata.querying.validation.read_only import UnsafeQueryError


class DummyAdapter(DataSourceAdapter):
    def test_connection(self) -> None:
        return None

    def scan_metadata(self) -> list[DatasetInfo]:
        return []

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        return NormalizedResult(
            source=self.datasource_id,
            dataset="dummy",
            columns=[],
            rows=[],
            row_count=0,
        )


class ReplacementDummyAdapter(DummyAdapter):
    pass


def create_related_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER REFERENCES customers(id),
                amount REAL NOT NULL
            );
            """
        )


def test_sqlite_scans_metadata_and_relations_as_models(tmp_path: Path) -> None:
    source_path = tmp_path / "sales.db"
    create_related_db(source_path)
    adapter = SQLiteAdapter("ds_sales", {"path": str(source_path)})

    datasets = adapter.scan_metadata()
    relations = adapter.scan_relations()

    assert {item.name for item in datasets} == {"customers", "orders"}
    customers = next(item for item in datasets if item.name == "customers")
    assert next(field for field in customers.fields if field.name == "id").primary_key
    assert relations == [
        RelationInfo(
            datasource_id="ds_sales",
            from_dataset="orders",
            from_field="customer_id",
            to_dataset="customers",
            to_field="id",
            relation_type="foreign_key",
            source="scan",
        )
    ]


def test_sqlite_still_rejects_writes(tmp_path: Path) -> None:
    source_path = tmp_path / "sales.db"
    create_related_db(source_path)
    adapter = SQLiteAdapter("ds_sales", {"path": str(source_path)})

    with pytest.raises(UnsafeQueryError):
        adapter.execute("DELETE FROM orders")


def test_registry_creates_sqlite_adapter(tmp_path: Path) -> None:
    source_path = tmp_path / "sales.db"
    create_related_db(source_path)

    adapter = create_adapter(
        "ds_sales",
        "relational",
        {"driver": "sqlite", "path": str(source_path)},
    )

    assert isinstance(adapter, SQLiteAdapter)


def test_registry_supports_new_adapter_without_factory_changes() -> None:
    register_adapter("document", "dummy", DummyAdapter)

    adapter = create_adapter("ds_dummy", "document", {"driver": "dummy"})

    assert isinstance(adapter, DummyAdapter)


def test_registry_replaces_duplicate_registration() -> None:
    register_adapter("key_value", "dummy-replace", DummyAdapter)
    register_adapter("key_value", "dummy-replace", ReplacementDummyAdapter)

    assert get_adapter_class("key_value", "dummy-replace") is ReplacementDummyAdapter


def test_registry_reports_kind_and_driver_for_unknown_adapter() -> None:
    with pytest.raises(NotImplementedError, match="kind=graph, driver=missing"):
        create_adapter("ds_graph", "graph", {"driver": "missing"})
