"""Elasticsearch and OpenSearch adapter."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from qaneris.adapters.base import DataSourceAdapter
from qaneris.adapters.native_query import bounded_limit, parse_query_payload
from qaneris.adapters.native_values import normalize_sample_row
from qaneris.contracts import DatasetInfo, DatasetSample, FieldInfo, NormalizedResult


class SearchAdapter(DataSourceAdapter):
    def _driver(self) -> str:
        return str(self.connection.get("driver", ""))

    def _addresses(self) -> list[str]:
        if self.connection.get("url"):
            url = str(self.connection["url"])
            if (self.connection.get("tls") or self.connection.get("use_ssl")) and url.startswith("http://"):
                parsed = urlsplit(url)
                url = urlunsplit(parsed._replace(scheme="https"))
            return [url]

        hosts = self.connection.get("hosts")
        if not hosts:
            hosts = [
                {
                    "host": self.connection.get("host", "localhost"),
                    "port": self.connection.get("port", 9200),
                }
            ]
        scheme = "https" if self.connection.get("tls") or self.connection.get("use_ssl") else "http"
        addresses = []
        for item in hosts:
            host = str(item["host"])
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = item.get("port") or 9200
            addresses.append(f"{scheme}://{host}:{port}")
        return addresses

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

                addresses = self._addresses()
                tls_options: dict[str, Any] = {}
                if ssl_context is not None:
                    tls_options["ssl_context"] = ssl_context
                else:
                    tls_options["verify_certs"] = bool(self.connection.get("verify_certs", True))
                client = Elasticsearch(
                    addresses[0] if len(addresses) == 1 else addresses,
                    basic_auth=self._auth(),
                    api_key=self.connection.get("api_key"),
                    **tls_options,
                )
            elif driver == "opensearch":
                from opensearchpy import OpenSearch

                # OpenSearch ignores verify_certs when an SSLContext is supplied and warns
                # about the conflicting options. The context already enforces verification.
                tls_options: dict[str, Any] = (
                    {"ssl_context": ssl_context} if ssl_context is not None
                    else {"verify_certs": bool(self.connection.get("verify_certs", True))}
                )
                if self.connection.get("use_ssl") is not None:
                    tls_options["use_ssl"] = bool(self.connection["use_ssl"])
                if ssl_context is not None and self.connection.get("server_name"):
                    # opensearch-py omits ssl_assert_hostname from its urllib3 pool when an
                    # SSLContext is present. Supply that one pool option without weakening
                    # certificate-chain validation or the context's TLS version floor.
                    tls_options["ssl_assert_hostname"] = self.connection["server_name"]
                    tls_options["connection_class"] = self._opensearch_hostname_connection()
                client = OpenSearch(
                    hosts=self._addresses(),
                    http_auth=self._auth(),
                    **tls_options,
                )
            else:
                raise ValueError(f"Unsupported search driver: {driver}")
        except ImportError as error:
            raise RuntimeError(f"{driver} support requires its Qaneris optional extra") from error
        try:
            yield client
        finally:
            client.close()

    @staticmethod
    def _opensearch_hostname_connection():
        from opensearchpy.connection.http_urllib3 import Urllib3HttpConnection

        class VerifiedHostnameConnection(Urllib3HttpConnection):
            def __init__(self, *args, ssl_assert_hostname=None, **kwargs):
                self._certificate_hostname = ssl_assert_hostname
                super().__init__(*args, ssl_assert_hostname=ssl_assert_hostname, **kwargs)

            def _create_urllib3_pool(self) -> None:
                super()._create_urllib3_pool()
                if self._certificate_hostname and self.use_ssl:
                    self.pool.assert_hostname = self._certificate_hostname

        return VerifiedHostnameConnection

    def _auth(self) -> tuple[str, str] | None:
        if not self.connection.get("username"):
            return None
        return str(self.connection["username"]), str(self.connection.get("password", ""))

    def _mapping_index_pattern(self) -> str:
        return str(self.connection.get("index_pattern", "*"))

    def test_connection(self) -> None:
        try:
            with self._client() as client:
                client.indices.get_mapping(index=self._mapping_index_pattern())
        except Exception as error:
            if self._driver() == "opensearch":
                detail = str(error).casefold()
                if (
                    not (self.connection.get("tls") or self.connection.get("use_ssl"))
                    and ("remotedisconnected" in detail or "remote end closed connection" in detail)
                ):
                    raise ConnectionError(
                        "OpenSearch 服务器关闭了明文 HTTP 连接。请在 SSL / 证书中开启"
                        "“启用加密连接”（HTTPS）；不要求上传客户端证书。"
                    ) from error
                if "certificate verify failed" in detail or "certificate_verify_failed" in detail:
                    raise ConnectionError(
                        "OpenSearch HTTPS 证书验证失败。请上传服务端 CA 证书，"
                        "并确认连接地址包含在服务器证书的 DNS/IP 名称中。"
                    ) from error
            raise

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
