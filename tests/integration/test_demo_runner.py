from pathlib import Path

from examples.run_demo import prepare_demo
from qaneris.catalog import Catalog


def test_prepare_demo_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "sales.db"
    catalog_path = tmp_path / "catalog.db"

    first = prepare_demo(database_path, catalog_path)
    second = prepare_demo(database_path, catalog_path)
    catalog = Catalog(catalog_path)

    assert database_path.is_file()
    assert first.id == second.id
    assert second.status == "ready"
    assert len(catalog.list_datasources()) == 1
    assert {dataset.name for dataset in catalog.list_datasets()} == {
        "customers",
        "products",
        "orders",
        "order_items",
    }
