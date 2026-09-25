"""Milvus vector adapter."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from smartdata.adapters.base import DataSourceAdapter
from smartdata.adapters.native_query import bounded_limit, parse_query_payload
from smartdata.adapters.native_values import normalize_sample_row
from smartdata.contracts import DatasetInfo, DatasetSample, FieldInfo, NormalizedResult


class MilvusAdapter(DataSourceAdapter):
    @contextmanager
    def _client(self) -> Iterator[Any]:
        try:
            from pymilvus import MilvusClient
        except ImportError as error:
            raise RuntimeError("Milvus support requires smartdata-platform[milvus]") from error
        # pymilvus carries TLS through connection kwargs: secure, ca_pem_path, client_pem_path,
        # client_key_path and server_name, all produced by the materializer.
        tls_options: dict[str, Any] = {}
        for name in ("secure", "ca_pem_path", "client_pem_path", "client_key_path", "server_name"):
            if self.connection.get(name) is not None:
                tls_options[name] = self.connection[name]
        client = MilvusClient(
            uri=self.connection.get("url", "http://localhost:19530"),
            token=self.connection.get("token"),
            db_name=self.connection.get("database", "default"),
            **tls_options,
        )
        try:
            yield client
        finally:
            client.close()

    def test_connection(self) -> None:
        with self._client() as client:
            client.list_collections()

    def scan_metadata(self) -> list[DatasetInfo]:
        with self._client() as client:
            return [
                DatasetInfo(
                    datasource_id=self.datasource_id,
                    name=name,
                    kind="collection",
                    fields=[
                        FieldInfo(
                            name=str(field.get("name")),
                            data_type=str(field.get("type", "unknown")),
                            primary_key=bool(field.get("is_primary", False)),
                        )
                        for field in client.describe_collection(name).get("fields", [])
                    ],
                )
                for name in sorted(client.list_collections())
            ]

    def _query(self, collection: str, filter_value: str, limit: int) -> list[dict[str, Any]]:
        with self._client() as client:
            rows = client.query(
                collection_name=collection,
                filter=filter_value,
                output_fields=["*"],
                limit=limit,
            )
        return [normalize_sample_row(row) for row in rows]

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        bounded = max(0, min(limit, 3))
        return [
            DatasetSample(
                datasource_id=self.datasource_id,
                dataset=dataset.name,
                rows=self._query(dataset.name, "", bounded),
            )
            for dataset in datasets
        ]

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        bounded = max(0, min(limit, 20))
        return {dataset.name: self._query(dataset.name, "", bounded) for dataset in datasets}

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        payload = parse_query_payload(query)
        collection = payload.get("collection")
        if not isinstance(collection, str) or not collection:
            raise ValueError("Milvus query requires collection")
        limit = bounded_limit(payload.get("limit"), max_rows)
        rows = self._query(collection, str(payload.get("filter", "")), limit + 1)
        truncated = len(rows) > limit
        rows = rows[:limit]
        columns = sorted({key for row in rows for key in row})
        return NormalizedResult(
            source=self.datasource_id,
            dataset=collection,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )
