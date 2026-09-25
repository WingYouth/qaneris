from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.common.artifacts import artifact_directory
from qaneris.contracts import DatasourceCreate
from qaneris.graph import Neo4jConfig


def main() -> int:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_root = artifact_directory("acceptance") / f"sqlite-{run_id}"
    run_root.mkdir(parents=True, exist_ok=False)
    source_path = run_root / "source.db"
    catalog_path = run_root / "catalog.db"
    with sqlite3.connect(source_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                region TEXT
            );
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                FOREIGN KEY(customer_id) REFERENCES customers(id)
            );
            CREATE INDEX idx_orders_customer ON orders(customer_id);
            """
        )

    service = QanerisService(Catalog(catalog_path))
    datasource = service.create_datasource(
        DatasourceCreate(
            name=f"sqlite-acceptance-{run_id}",
            kind="relational",
            connection={"driver": "sqlite", "path": str(source_path)},
        )
    )
    datasets = service.scan_datasource(datasource.id)

    config = Neo4jConfig.from_environment()
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(config.uri, auth=(config.username, config.password))
    try:
        with driver.session(database=config.database) as session:
            row = session.run(
                "MATCH (d:Database {datasource_id: $datasource_id}) "
                "OPTIONAL MATCH (d)-[*1..]->(n) "
                "WITH d, collect(DISTINCT n) AS nodes "
                "OPTIONAL MATCH (a:DataObject)-[r:RELATES_TO]->(b:DataObject) "
                "WHERE a.datasource_id=$datasource_id AND b.datasource_id=$datasource_id "
                "RETURN count(DISTINCT d) AS databases, "
                "size([n IN nodes WHERE n:DataObject]) AS objects, "
                "size([n IN nodes WHERE n:Field]) AS fields, "
                "size([n IN nodes WHERE n:Index]) AS indexes, "
                "size([n IN nodes WHERE n:Constraint]) AS constraints, "
                "count(DISTINCT r) AS relationships, "
                "collect(DISTINCT {from: a.qualified_name, to: b.qualified_name, "
                "from_field: r.from_field_path, to_field: r.to_field_path, "
                "source: r.source, confirmed: r.confirmed}) AS relationship_details",
                datasource_id=datasource.id,
            ).single()
            integrity = session.run(
                "MATCH (d:Database {datasource_id: $datasource_id})-[*0..]->(n) "
                "WITH collect(DISTINCT n) AS nodes "
                "UNWIND nodes AS n WITH nodes, n.id AS id, count(*) AS occurrences "
                "WITH nodes, sum(CASE WHEN occurrences > 1 THEN 1 ELSE 0 END) AS duplicates "
                "RETURN duplicates, size([n IN nodes WHERE n.id IS NULL]) AS missing_ids",
                datasource_id=datasource.id,
            ).single()
    finally:
        driver.close()

    checks = {
        "Connection": True,
        "Database node": row["databases"] == 1,
        "DataObject count": row["objects"] == 3 == len(datasets),
        "Field count": row["fields"] == 9,
        "Index": row["indexes"] >= 1,
        "Constraint": row["constraints"] >= 4,
        "Declared relationship": row["relationships"] == 1,
        "No false relationship": row["relationship_details"]
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
        "Graph integrity": integrity["duplicates"] == 0 and integrity["missing_ids"] == 0,
    }
    for name, passed in checks.items():
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    print(f"Artifacts: {run_root}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
