"""Elasticsearch and OpenSearch adapter."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from smartdata.adapters.base import DataSourceAdapter
from smartdata.adapters.native_query import bounded_limit, parse_query_payload
from smartdata.adapters.native_values import normalize_sample_row
from smartdata.contracts import DatasetInfo, DatasetSample, FieldInfo, NormalizedResult


class SearchAdapter(DataSourceAdapter):
    def _driver(self) -> str:
        return str(self.connection.get("driver", ""))

    @contextmanager
    def _client(self) -> Iterator[Any]:
        driver = self._driver()
        try:
            # An SSLContext already carries the CA, the client pair and the verification policy, so
            # it is passed alone - never alongside ca_certs/client_cert/client_key, which would be
            # a second, competing TLS configuration.
            ssl_context = self.connection.get("ssl_context")
            if driver == "elasticsearch":
                from elasticsearch import Elasticsearch

                tls_options: dict[str, Any] = {}
                if ssl_context is not None:
                    tls_options["ssl_context"] = ssl_context
                else:
                    tls_options["verify_certs"] = bool(self.connection.get("verify_certs", True))
                client = Elasticsearch(
                    self.connection.get("url", "http://localhost:9200"),
                    basic_auth=self._auth(),
                    api_key=self.connection.get("api_key"),
                    **tls_options,
                )
            elif driver == "opensearch":
                from opensearchpy import OpenSearch

                tls_options = {"verify_certs": bool(self.connection.get("verify_certs", True))}
                if ssl_context is not None:
                    tls_options["ssl_context"] = ssl_context
                if self.connection.get("use_ssl") is not None:
                    tls_options["use_ssl"] = bool(self.connection["use_ssl"])
                client = OpenSearch(
                    hosts=[self.connection.get("url", "http://localhost:9200")],
                    http_auth=self._auth(),
                    **tls_options,
                )
            else:
                raise ValueError(f"Unsupported search driver: {driver}")
        except ImportError as error:
            raise RuntimeError(f"{driver} support requires its SmartData optional extra") from error
        try:
            yield client
        finally:
            client.close()

    def _auth(self) -> tuple[str, str] | None:
        if not self.connection.get("username"):
            return None
        return str(self.connection["username"]), str(self.connection.get("password", ""))

    def _mapping_index_pattern(self) -> str:
        return str(self.connection.get("index_pattern", "*"))

    def test_connection(self) -> None:
        with self._client() as client:
            client.indices.get_mapping(index=self._mapping_index_pattern())

    @staticmethod
    def _flatten_properties(properties: dict[str, Any], prefix: str = "") -> list[FieldInfo]:
        fields: list[FieldInfo] = []
        for name, definition in properties.items():
            path = f"{prefix}.{name}" if prefix else name
            fields.append(FieldInfo(name=path, data_type=str(definition.get("type", "object"))))
            nested = definition.get("properties")
            if isinstance(nested, dict):
                fields.extend(SearchAdapter._flatten_properties(nested, path))
        return fields

    def scan_metadata(self) -> list[DatasetInfo]:
        with self._client() as client:
            mappings = client.indices.get_mapping(index=self._mapping_index_pattern())
        return [
            DatasetInfo(
                datasource_id=self.datasource_id,
                name=index,
                kind="index",
                fields=self._flatten_properties(body.get("mappings", {}).get("properties", {})),
            )
            for index, body in sorted(mappings.items())
            if not index.startswith(".")
        ]

    def _search(self, index: str, body: dict[str, Any], size: int) -> list[dict[str, Any]]:
        body = dict(body)
        body["size"] = size
        with self._client() as client:
            response = client.search(index=index, body=body)
        hits = response.get("hits", {}).get("hits", [])
        return [
            normalize_sample_row({"_id": hit.get("_id"), **hit.get("_source", {})}) for hit in hits
        ]

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        bounded = max(0, min(limit, 3))
        return [
            DatasetSample(
                datasource_id=self.datasource_id,
                dataset=dataset.name,
                rows=self._search(dataset.name, {"query": {"match_all": {}}}, bounded),
            )
            for dataset in datasets
        ]

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        bounded = max(0, min(limit, 20))
        return {
            dataset.name: self._search(dataset.name, {"query": {"match_all": {}}}, bounded)
            for dataset in datasets
        }

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        payload = parse_query_payload(query)
        index = payload.pop("index", None)
        if not isinstance(index, str) or not index:
            raise ValueError("Search query requires index")
        limit = bounded_limit(payload.get("size"), max_rows)
        rows = self._search(index, payload, limit + 1)
        truncated = len(rows) > limit
        rows = rows[:limit]
        columns = sorted({key for row in rows for key in row})
        return NormalizedResult(
            source=self.datasource_id,
            dataset=index,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )
