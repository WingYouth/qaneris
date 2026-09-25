"""Cassandra wide-column adapter."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import islice
from typing import Any

from smartdata.adapters.base import DataSourceAdapter
from smartdata.adapters.native_values import normalize_sample_row
from smartdata.contracts import DatasetInfo, DatasetSample, FieldInfo, NormalizedResult

READ_ONLY_CQL = re.compile(r"^\s*SELECT\b", re.IGNORECASE)


class CassandraAdapter(DataSourceAdapter):
    def _keyspace(self) -> str:
        keyspace = self.connection.get("keyspace")
        if not keyspace:
            raise ValueError("Cassandra connection requires keyspace")
        return str(keyspace)

    @contextmanager
    def _session(self) -> Iterator[Any]:
        try:
            from cassandra.auth import PlainTextAuthProvider
            from cassandra.cluster import Cluster
        except ImportError as error:
            raise RuntimeError(
                "Cassandra support requires smartdata-platform[cassandra]"
            ) from error
        auth = None
        if self.connection.get("username"):
            auth = PlainTextAuthProvider(
                username=str(self.connection["username"]),
                password=str(self.connection.get("password", "")),
            )
        # TLS parameters come from the materializer: a live SSLContext plus, only when an override
        # was explicitly configured, the SNI name Cassandra carries through ssl_options.
        cluster = Cluster(
            contact_points=self.connection.get("contact_points", ["127.0.0.1"]),
            port=int(self.connection.get("port", 9042)),
            auth_provider=auth,
            ssl_context=self.connection.get("ssl_context"),
            ssl_options=self.connection.get("ssl_options"),
        )
        session = cluster.connect(self._keyspace())
        try:
            yield session
        finally:
            session.shutdown()
            cluster.shutdown()

    def test_connection(self) -> None:
        with self._session() as session:
            session.execute("SELECT release_version FROM system.local")

    def scan_metadata(self) -> list[DatasetInfo]:
        with self._session() as session:
            keyspace = session.cluster.metadata.keyspaces[self._keyspace()]
            return [
                DatasetInfo(
                    datasource_id=self.datasource_id,
                    name=name,
                    kind="table",
                    fields=[
                        FieldInfo(
                            name=column.name,
                            data_type=str(column.cql_type),
                            primary_key=column.name
                            in {
                                key_column.name
                                for key_column in (*table.partition_key, *table.clustering_key)
                            },
                        )
                        for column in table.columns.values()
                    ],
                )
                for name, table in sorted(keyspace.tables.items())
            ]

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        bounded = max(0, min(limit, 3))
        with self._session() as session:
            return [
                DatasetSample(
                    datasource_id=self.datasource_id,
                    dataset=dataset.name,
                    rows=[
                        normalize_sample_row(row._asdict())
                        for row in session.execute(
                            f'SELECT * FROM "{dataset.name.replace(chr(34), chr(34) * 2)}" '
                            f"LIMIT {bounded}"
                        )
                    ],
                )
                for dataset in datasets
            ]

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        bounded = max(0, min(limit, 20))
        with self._session() as session:
            return {
                dataset.name: [
                    normalize_sample_row(row._asdict())
                    for row in session.execute(
                        f'SELECT * FROM "{dataset.name.replace(chr(34), chr(34) * 2)}" '
                        f"LIMIT {bounded}"
                    )
                ]
                for dataset in datasets
            }

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        if not READ_ONLY_CQL.match(query) or ";" in query.rstrip().rstrip(";"):
            raise ValueError("Only one Cassandra SELECT statement is allowed")
        with self._session() as session:
            rows = [
                normalize_sample_row(row._asdict())
                for row in islice(session.execute(query), max_rows + 1)
            ]
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        columns = list(rows[0]) if rows else []
        return NormalizedResult(
            source=self.datasource_id,
            dataset="query",
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )
