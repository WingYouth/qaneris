"""Neo4j graph adapter."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from smartdata.adapters.base import DataSourceAdapter
from smartdata.adapters.native_values import normalize_sample_row
from smartdata.contracts import (
    DatasetInfo,
    DatasetSample,
    FieldInfo,
    NormalizedResult,
    RelationInfo,
)

WRITE_CYPHER = re.compile(
    r"\b(CREATE|DELETE|DETACH|DROP|FOREACH|LOAD\s+CSV|MERGE|REMOVE|SET)\b",
    re.IGNORECASE,
)


class Neo4jAdapter(DataSourceAdapter):
    @contextmanager
    def _driver(self) -> Iterator[Any]:
        try:
            from neo4j import GraphDatabase
        except ImportError as error:
            raise RuntimeError("Neo4j support requires smartdata-platform[neo4j]") from error
        uri = self.connection.get("url") or self.connection.get("uri")
        if not uri:
            raise ValueError("Neo4j connection requires url")
        auth = None
        if self.connection.get("username"):
            auth = (self.connection["username"], self.connection.get("password", ""))
        # The materializer supplies an SSLContext, and its URI normalization guarantees the ``+s``
        # scheme is not also present: the driver refuses to combine the two.
        ssl_context = self.connection.get("ssl_context")
        driver_options: dict[str, Any] = {}
        if ssl_context is not None:
            driver_options["ssl_context"] = ssl_context
        driver = GraphDatabase.driver(uri, auth=auth, **driver_options)
        try:
            yield driver
        finally:
            driver.close()

    def _database(self) -> str | None:
        return self.connection.get("database")

    def test_connection(self) -> None:
        with self._driver() as driver:
            driver.verify_connectivity()

    def _query(self, query: str, **parameters: Any) -> list[dict[str, Any]]:
        with self._driver() as driver:
            with driver.session(database=self._database()) as session:
                rows = session.execute_read(
                    lambda transaction: [
                        record.data() for record in transaction.run(query, **parameters)
                    ]
                )
            return [normalize_sample_row(row) for row in rows]

    def scan_metadata(self) -> list[DatasetInfo]:
        labels = self._query("CALL db.labels() YIELD label RETURN label ORDER BY label")
        datasets = []
        for item in labels:
            label = item["label"]
            rows = self._query(
                "MATCH (n) WHERE $label IN labels(n) RETURN properties(n) AS value LIMIT 3",
                label=label,
            )
            properties = sorted({key for row in rows for key in (row.get("value") or {})})
            datasets.append(
                DatasetInfo(
                    datasource_id=self.datasource_id,
                    name=label,
                    kind="node_label",
                    fields=[FieldInfo(name=name, data_type="property") for name in properties],
                )
            )
        return datasets

    def scan_relations(self) -> list[RelationInfo]:
        rows = self._query(
            "MATCH (a)-[r]->(b) UNWIND labels(a) AS from_label "
            "UNWIND labels(b) AS to_label RETURN DISTINCT from_label, type(r) AS relation, "
            "to_label LIMIT 10000"
        )
        return [
            RelationInfo(
                datasource_id=self.datasource_id,
                from_dataset=row["from_label"],
                to_dataset=row["to_label"],
                relation_type=row["relation"],
                source="scan",
            )
            for row in rows
        ]

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        bounded = max(0, min(limit, 3))
        return [
            DatasetSample(
                datasource_id=self.datasource_id,
                dataset=dataset.name,
                rows=self._query(
                    "MATCH (n) WHERE $label IN labels(n) RETURN properties(n) AS value LIMIT $limit",
                    label=dataset.name,
                    limit=bounded,
                ),
            )
            for dataset in datasets
        ]

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        bounded = max(0, min(limit, 20))
        return {
            dataset.name: self._query(
                "MATCH (n) WHERE $label IN labels(n) RETURN properties(n) AS value LIMIT $limit",
                label=dataset.name,
                limit=bounded,
            )
            for dataset in datasets
        }

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        if WRITE_CYPHER.search(query) or ";" in query.rstrip().rstrip(";"):
            raise ValueError("Only one read-only Cypher query is allowed")
        rows = self._query(
            f"CALL {{ {query.rstrip().rstrip(';')} }} RETURN * LIMIT $limit", limit=max_rows + 1
        )
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        columns = sorted({key for row in rows for key in row})
        return NormalizedResult(
            source=self.datasource_id,
            dataset="graph",
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )
