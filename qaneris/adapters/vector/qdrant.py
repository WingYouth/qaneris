"""Qdrant vector adapter."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from qaneris.adapters.base import DataSourceAdapter
from qaneris.adapters.native_query import bounded_limit, parse_query_payload
from qaneris.adapters.native_values import normalize_sample_row
from qaneris.contracts import DatasetInfo, DatasetSample, FieldInfo, NormalizedResult


class QdrantAdapter(DataSourceAdapter):
    @contextmanager
    def _client(self) -> Iterator[Any]:
        try:
            from qdrant_client import QdrantClient
        except ImportError as error:
            raise RuntimeError("Qdrant support requires qaneris-platform[qdrant]") from error
        # Qdrant is HTTPS with the system trust store only: no custom CA, no mTLS. The materializer
        # has already refused those requests, and normalizes the URL to https when TLS is on, so
        # nothing here relies on a process-global trust override.
        options = {
            "url": self.connection.get("url"),
            "host": self.connection.get("host"),
            "port": self.connection.get("port"),
            "api_key": self.connection.get("api_key"),
            "timeout": float(self.connection.get("timeout", 10)),
        }
        if self.connection.get("https"):
            options["https"] = True
        client = QdrantClient(**options)
        try:
            yield client
        finally:
            client.close()

    def test_connection(self) -> None:
        with self._client() as client:
            client.get_collections()

    def scan_metadata(self) -> list[DatasetInfo]:
        with self._client() as client:
            collections = client.get_collections().collections
            datasets = []
            for collection in collections:
                info = client.get_collection(collection.name)
                schema = getattr(info, "payload_schema", {}) or {}
                datasets.append(
                    DatasetInfo(
                        datasource_id=self.datasource_id,
                        name=collection.name,
                        kind="collection",
                        fields=[
                            FieldInfo(name=name, data_type=str(definition))
                            for name, definition in sorted(schema.items())
                        ],
                    )
                )
            return datasets

    def _scroll(
        self, collection: str, limit: int, scroll_filter: Any = None
    ) -> list[dict[str, Any]]:
        with self._client() as client:
            points, _ = client.scroll(
                collection_name=collection,
                scroll_filter=scroll_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        return [normalize_sample_row({"id": point.id, **(point.payload or {})}) for point in points]

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        bounded = max(0, min(limit, 3))
        return [
            DatasetSample(
                datasource_id=self.datasource_id,
                dataset=dataset.name,
                rows=self._scroll(dataset.name, bounded),
            )
            for dataset in datasets
        ]

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        bounded = max(0, min(limit, 20))
        return {dataset.name: self._scroll(dataset.name, bounded) for dataset in datasets}

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        payload = parse_query_payload(query)
        collection = payload.get("collection")
        if not isinstance(collection, str) or not collection:
            raise ValueError("Qdrant query requires collection")
        limit = bounded_limit(payload.get("limit"), max_rows)
        rows = self._scroll(collection, limit + 1, payload.get("filter"))
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
