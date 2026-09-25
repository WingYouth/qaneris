from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from examples.create_demo_database import create_demo_database
from smartdata.application.service import SmartDataService
from smartdata.catalog import Catalog
from smartdata.contracts import Datasource, DatasourceCreate
from smartdata.interfaces.api.app import create_app

DEFAULT_DATABASE = Path(__file__).with_name("sales_2026_07.db")
DEFAULT_CATALOG = Path(__file__).with_name("demo_catalog.db")
DEMO_DATASOURCE_NAME = "sales_2026_07"


def prepare_demo(
    database_path: str | Path = DEFAULT_DATABASE,
    catalog_path: str | Path = DEFAULT_CATALOG,
) -> Datasource:
    database = Path(database_path).expanduser().resolve()
    catalog_file = Path(catalog_path).expanduser().resolve()
    if not database.exists():
        create_demo_database(database)

    service = SmartDataService(Catalog(catalog_file))
    datasource = next(
        (item for item in service.list_datasources() if item.name == DEMO_DATASOURCE_NAME),
        None,
    )
    if datasource is None:
        datasource = service.create_datasource(
            DatasourceCreate(
                name=DEMO_DATASOURCE_NAME,
                kind="relational",
                connection={"driver": "sqlite", "path": str(database)},
            )
        )
    service.scan_datasource(datasource.id)
    return service.catalog.get_datasource(datasource.id)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare and run the SmartData demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    args = parser.parse_args()

    datasource = prepare_demo(args.database, args.catalog)
    print(f"Demo datasource ready: {datasource.name} ({datasource.id})")
    print(f"Open http://{args.host}:{args.port}")
    uvicorn.run(create_app(args.catalog), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
