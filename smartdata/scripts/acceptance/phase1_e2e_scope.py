"""Build the Phase 1 real-E2E acceptance scope: source SQLite + datasource + published graph.

Why this script exists
----------------------
The Phase 1 Exit Gate requires the formal Ask pipeline to run against a real SQLite
datasource whose published graph carries (a) a real foreign-key relationship, (b) a
real ``DATE``-typed time axis, and (c) rows that the relative-time normalizer can
actually resolve.

The repository's task-data SQLite cannot serve that role: its ``order_date`` is
declared ``VARCHAR(255)`` and its rows are pinned to July 2026, while
``normalize_time_range`` anchors relative expressions on the current date. So the
acceptance source is built here instead, and it is deliberately *not* derived from
``retail.db`` (that dataset is deterministic from a fixed seed and is owned by the
task-data project).

The script is idempotent: the catalog, the datasource and the published graph are
reused or replaced rather than multiplied. Running it twice produces one datasource
with one graph, carrying the latest scan.

Usage::

    SMARTDATA_ARTIFACT_ROOT=... python3 -m smartdata.scripts.acceptance.phase1_e2e_scope
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from smartdata.application.service import SmartDataService
from smartdata.catalog import Catalog
from smartdata.common.artifacts import artifact_directory
from smartdata.common.environment import load_runtime_environment
from smartdata.contracts import DatasourceCreate
from smartdata.graph import Neo4jConfig

DATASOURCE_NAME = "phase1-e2e-sqlite"
WORKSPACE_ID = "default"

#: 14 regions so that a "top 10" ranking actually truncates.
REGIONS = (
    "上海",
    "北京",
    "深圳",
    "广州",
    "杭州",
    "南京",
    "成都",
    "重庆",
    "武汉",
    "西安",
    "天津",
    "苏州",
    "青岛",
    "长沙",
)

CUSTOMERS_PER_REGION = 3
ORDER_DAYS = 45
ORDERS_PER_DAY = 8

ORDERS_DDL = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    order_no TEXT NOT NULL,
    customer_id INTEGER NOT NULL,
    order_date DATE NOT NULL,
    delivery_region TEXT NOT NULL,
    amount REAL NOT NULL,
    total_amount REAL NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
)
"""


def load_dotenv() -> None:
    """Backward-compatible alias for the shared runtime environment bootstrap."""
    load_runtime_environment()


def run_root() -> Path:
    return artifact_directory("acceptance") / "phase1"


def build_source(path: Path, *, today: date) -> dict[str, int]:
    """Create the acceptance SQLite source deterministically, anchored on ``today``.

    The file is rebuilt in place rather than deleted first: an acceptance script that unlinks
    its own artefact on every run trips bulk-delete guards, and dropping the tables is enough.
    """
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE IF EXISTS orders;
            DROP TABLE IF EXISTS customers;
            PRAGMA foreign_keys = ON;
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                region TEXT NOT NULL
            );
            """
            + ORDERS_DDL
            + """
            ;
            CREATE INDEX idx_orders_customer ON orders(customer_id);
            CREATE INDEX idx_orders_date ON orders(order_date);
            """
        )

        customers: list[tuple[int, str, str]] = []
        for index, region in enumerate(REGIONS):
            for slot in range(CUSTOMERS_PER_REGION):
                customer_id = index * CUSTOMERS_PER_REGION + slot + 1
                customers.append((customer_id, f"{region}客户{slot + 1}", region))
        connection.executemany(
            "INSERT INTO customers (id, name, region) VALUES (?, ?, ?)", customers
        )

        orders: list[tuple[int, str, int, str, str, float, float]] = []
        order_id = 0
        for offset in range(ORDER_DAYS - 1, -1, -1):
            day = today - timedelta(days=offset)
            for slot in range(ORDERS_PER_DAY):
                order_id += 1
                region_index = (offset * ORDERS_PER_DAY + slot) % len(REGIONS)
                region = REGIONS[region_index]
                customer_id = region_index * CUSTOMERS_PER_REGION + (slot % CUSTOMERS_PER_REGION) + 1
                amount = 100.0 + float((order_id * 37) % 900)
                orders.append(
                    (
                        order_id,
                        f"SO{order_id:06d}",
                        customer_id,
                        day.isoformat(),
                        region,
                        amount,
                        amount + 50.0,
                    )
                )
        connection.executemany(
            "INSERT INTO orders (id, order_no, customer_id, order_date, delivery_region,"
            " amount, total_amount) VALUES (?, ?, ?, ?, ?, ?, ?)",
            orders,
        )
        connection.commit()

    return {"customers": len(customers), "orders": len(orders)}


def ensure_datasource(service: SmartDataService, source_path: Path):
    for datasource in service.list_datasources(WORKSPACE_ID):
        if datasource.name == DATASOURCE_NAME:
            return datasource, True
    return (
        service.create_datasource(
            DatasourceCreate(
                name=DATASOURCE_NAME,
                kind="relational",
                workspace_id=WORKSPACE_ID,
                connection={"driver": "sqlite", "path": str(source_path)},
            )
        ),
        False,
    )


def verify_graph(datasource_id: str) -> dict[str, object]:
    config = Neo4jConfig.from_environment()
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(config.uri, auth=(config.username, config.password))
    facts: dict[str, object] = {}
    try:
        with driver.session(database=config.database) as session:
            row = session.run(
                """
                MATCH (d:Database {datasource_id: $datasource_id})
                OPTIONAL MATCH (d)-[:CONTAINS]->(o:DataObject)
                OPTIONAL MATCH (o)-[:HAS_FIELD]->(f:Field)
                RETURN d.scan_version AS scan_version,
                       count(DISTINCT o) AS objects,
                       count(DISTINCT f) AS fields
                """,
                datasource_id=datasource_id,
            ).single()
            facts["scan_version"] = row["scan_version"]
            facts["objects"] = row["objects"]
            facts["fields"] = row["fields"]

            facts["relationships"] = [
                {
                    "from": record["frm"],
                    "to": record["to"],
                    "from_field": record["from_field"],
                    "to_field": record["to_field"],
                    "source": record["source"],
                    "confirmed": record["confirmed"],
                }
                for record in session.run(
                    """
                    MATCH (a:DataObject {datasource_id: $datasource_id})
                          -[r:RELATES_TO]->
                          (b:DataObject {datasource_id: $datasource_id})
                    RETURN a.name AS frm, b.name AS to,
                           r.from_field_path AS from_field, r.to_field_path AS to_field,
                           r.source AS source, r.confirmed AS confirmed
                    """,
                    datasource_id=datasource_id,
                )
            ]

            facts["date_fields"] = [
                {
                    "object": record["obj"],
                    "path": record["path"],
                    "data_type": record["dt"],
                }
                for record in session.run(
                    """
                    MATCH (o:DataObject {datasource_id: $datasource_id})-[:HAS_FIELD]->(f:Field)
                    WHERE toLower(f.data_type) IN ['date', 'datetime', 'timestamp', 'timestamptz']
                    RETURN o.name AS obj, f.path AS path, f.data_type AS dt
                    ORDER BY obj, path
                    """,
                    datasource_id=datasource_id,
                )
            ]
    finally:
        driver.close()
    return facts


def main() -> int:
    load_dotenv()
    today = datetime.now(UTC).date()
    root = run_root()
    root.mkdir(parents=True, exist_ok=True)
    source_path = root / "source.db"
    catalog_path = root / "catalog.db"

    counts = build_source(source_path, today=today)
    print(f"Acceptance root : {root}")
    print(f"Source SQLite   : {source_path}")
    print(f"Catalog         : {catalog_path}")
    print(f"Anchor date     : {today.isoformat()}")
    print(f"Rows            : {counts}")

    service = SmartDataService(Catalog(catalog_path))
    datasource, reused = ensure_datasource(service, source_path)
    print(f"Datasource      : {datasource.id} ({'reused' if reused else 'created'})")

    datasets = service.scan_datasource(datasource.id)
    print(f"Scanned objects : {[item.name for item in datasets]}")

    facts = verify_graph(datasource.id)
    relationships = facts["relationships"]
    checks = {
        "Database node published": facts["scan_version"] is not None,
        "DataObject count == 2": facts["objects"] == 2,
        "Foreign key became RELATES_TO": relationships
        == [
            {
                "from": "orders",
                "to": "customers",
                "from_field": "customer_id",
                "to_field": "id",
                "source": "database",
                "confirmed": True,
            }
        ],
        "DATE-typed time axis present": [
            (item["object"], item["path"]) for item in facts["date_fields"]
        ]
        == [("orders", "order_date")],
    }
    print()
    for name, passed in checks.items():
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    print(f"scan_version    : {facts['scan_version']}")
    print(f"date field      : {facts['date_fields']}")
    print(f"relationships   : {relationships}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
