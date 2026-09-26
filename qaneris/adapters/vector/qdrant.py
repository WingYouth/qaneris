"""Qdrant vector adapter."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

from qaneris.adapters.base import DataSourceAdapter
from qaneris.adapters.native_query import bounded_limit, parse_query_payload
from qaneris.adapters.native_values import normalize_sample_row
from qaneris.contracts import DatasetInfo, DatasetSample, FieldInfo, NormalizedResult


class QdrantAdapter(DataSourceAdapter):
    def _uses_https(self) -> bool:
        url = self.connection.get("url")
        if url and urlsplit(str(url)).scheme:
            return urlsplit(str(url)).scheme == "https"
        return bool(self.connection.get("tls") or self.connection.get("https"))

    @contextmanager
    def _client(self) -> Iterator[Any]:
        try:
            from qdrant_client import QdrantClient
        except ImportError as error:
            raise RuntimeError("Qdrant support requires qaneris-platform[qdrant]") from error
        # Qdrant's SDK assumes HTTPS whenever an API key is present unless https is passed
        # explicitly. Honor the selected transport, but require explicit consent before sending
        # a key over plaintext HTTP.
        https = self._uses_https()
        if (
            self.connection.get("api_key")
            and not https
            and not self.connection.get("allow_insecure_api_key_http")
        ):
            raise ValueError(
                "Qdrant API Key 将通过明文 HTTP 传输。请启用 HTTPS，"
                "或明确允许在 HTTP 上传输 API Key。"
            )
        options = {
            name: self.connection[name]
            for name in ("url", "host", "port", "api_key", "verify")
            if self.connection.get(name) is not None
        }
        options["timeout"] = float(self.connection.get("timeout", 10))
        options["https"] = https
        client = QdrantClient(**options)
        try:
            yield client
        finally:
            client.close()

    def test_connection(self) -> None:
        try:
            with self._client() as client:
                client.get_collections()
        except Exception as error:
            if self._uses_https() and "wrong_version_number" in str(error).casefold():
                raise ConnectionError(
                    "Qdrant 目标端口未提供 HTTPS。请在 Qdrant 服务端启用 HTTPS，"
                    "或连接提供 HTTPS 的反向代理地址和端口。"
                ) from error
            raise

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
