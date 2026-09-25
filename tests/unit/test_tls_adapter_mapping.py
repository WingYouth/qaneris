"""Adapter TLS mapping tests (RS-CONN-01B).

These tests never open a real database. Each one captures what the adapter would pass to its driver
constructor - by monkeypatching the constructor in the driver's module, or by intercepting the
httpx client - and asserts the TLS parameters the materializer produced actually reach it.

That is the contract in question: the materializer decides *what* the parameters are, and each
adapter's job is only to deliver them to its driver without re-deriving anything from the profile.
An adapter that read a certificate itself would show up here as a second, competing parameter.
"""

from __future__ import annotations

import ssl
from typing import Any, Self

import pytest

from qaneris.adapters.document.couchdb import CouchDBAdapter
from qaneris.adapters.document.mongodb import MongoDBAdapter
from qaneris.adapters.graph.neo4j import Neo4jAdapter
from qaneris.adapters.key_value.redis import RedisAdapter
from qaneris.adapters.relational.sqlalchemy import SQLAlchemyAdapter
from qaneris.adapters.search.engine import SearchAdapter
from qaneris.adapters.time_series.influxdb import InfluxDBAdapter
from qaneris.adapters.vector.milvus import MilvusAdapter
from qaneris.adapters.vector.qdrant import QdrantAdapter
from qaneris.adapters.vector.weaviate import WeaviateAdapter
from qaneris.adapters.wide_column.cassandra import CassandraAdapter
from tests.unit._tls_helpers import ca_certificate, client_certificate, key_pem, pem, private_key


class Capture:
    """A stand-in driver constructor that records its call and returns a harmless object."""

    def __init__(self, return_value: Any = None):
        self.calls: list[tuple[tuple, dict]] = []
        self.return_value = return_value if return_value is not None else _StandIn()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.return_value

    @property
    def kwargs(self) -> dict:
        return self.calls[-1][1]


class _StandIn:
    """Stands in for a client object whose methods the adapter would call."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def __getattr__(self, name: str):
        def call(*args: Any, **kwargs: Any) -> _StandIn:
            return self

        return call


@pytest.fixture
def stub_driver_modules(monkeypatch: pytest.MonkeyPatch):
    """Install empty stand-ins for driver libraries this repo does not depend on.

    Adapters import their driver inside the method they need it, so a module that is absent from
    the environment can be replaced by one carrying only the constructor a test cares about. That
    keeps these tests about the parameter mapping rather than about which optional extras happen to
    be installed here.
    """
    import sys
    import types

    def install(module_name: str, **attributes: Any) -> None:
        """Register a module (dotted names create their parents) with the given attributes."""
        parts = module_name.split(".")
        for depth in range(1, len(parts)):
            parent_name = ".".join(parts[:depth])
            if parent_name not in sys.modules:
                parent = types.ModuleType(parent_name)
                parent.__path__ = []  # mark it as a package
                monkeypatch.setitem(sys.modules, parent_name, parent)
        module = types.ModuleType(module_name)
        for name, value in attributes.items():
            setattr(module, name, value)
        if len(parts) > 1:
            setattr(sys.modules[".".join(parts[:-1])], parts[-1], module)
        monkeypatch.setitem(sys.modules, module_name, module)

    return install


def materialized(driver: str, **tls: Any) -> dict[str, Any]:
    """Run the real materializer, so adapters are tested against its actual output."""
    from qaneris.connections.tls_materializer import TLSMaterializer

    connection = _resolved(driver, **tls)
    with TLSMaterializer().materialize(connection) as parameters:
        # The dict is copied out while the material is still alive; the adapters under test never
        # touch the filesystem themselves.
        result = {**connection_secrets(driver), **parameters}
    return result


def connection_secrets(driver: str) -> dict[str, Any]:
    base: dict[str, Any] = {"driver": driver}
    if driver == "mongodb":
        base.update({"url": "mongodb://localhost:27017", "database": "company"})
    elif driver == "redis":
        base.update({"host": "localhost", "port": 6379, "database": 0})
    elif driver in {"couchdb", "weaviate", "qdrant"}:
        base.update({"url": "http://localhost:5984" if driver == "couchdb" else "http://localhost:8080"})
    elif driver == "milvus":
        base.update({"url": "http://localhost:19530", "database": "default"})
    elif driver == "influxdb":
        base.update({"url": "http://localhost:8086", "token": "t", "org": "o", "bucket": "b"})
    elif driver in {"elasticsearch", "opensearch"}:
        base.update({"url": "http://localhost:9200"})
    elif driver == "cassandra":
        base.update({"contact_points": ["127.0.0.1"], "port": 9042, "keyspace": "ks"})
    elif driver in {"neo4j"}:
        base.update({"url": "bolt://localhost:7687"})
    elif driver in {"postgresql", "clickhouse", "sqlserver", "mysql", "timescaledb"}:
        base.update(
            {
                "url": {
                    "postgresql": "postgresql+psycopg://readonly@localhost:5432/company",
                    "timescaledb": "postgresql+psycopg://readonly@localhost:5432/company",
                    "mysql": "mysql+pymysql://readonly@localhost:3306/company",
                    "sqlserver": "mssql+pyodbc://readonly@localhost/company",
                    "clickhouse": "clickhousedb://readonly@localhost:8123/company",
                }[driver]
            }
        )
    return base


def _resolved(driver: str, **tls: Any):
    from tests.unit._tls_helpers import resolved

    return resolved(driver, **tls)


def ca_pem() -> str:
    certificate, _ = ca_certificate()
    return pem(certificate)


def client_pair() -> tuple[str, str]:
    key = private_key()
    certificate, _ = client_certificate(key)
    return pem(certificate), key_pem(key)


# ----------------------------------------------------------------------------------------------
# SQLAlchemy (PostgreSQL, MySQL, SQL Server, ClickHouse)
# ----------------------------------------------------------------------------------------------


def test_sqlalchemy_adapter_passes_connect_args_to_create_engine(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def create_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _StandIn()

    monkeypatch.setattr("sqlalchemy.create_engine", create_engine)
    connection = materialized("postgresql", ca=ca_pem())
    adapter = SQLAlchemyAdapter("ds", connection)

    with adapter._connect():
        pass

    assert captured["connect_args"]["sslmode"] == "verify-full"
    assert captured["connect_args"]["sslrootcert"].endswith("ca.pem")
    assert captured["pool_pre_ping"] is True


def test_sqlalchemy_adapter_has_empty_connect_args_without_tls() -> None:
    adapter = SQLAlchemyAdapter("ds", {"driver": "sqlite", "url": "sqlite://"})
    assert adapter.connection.get("connect_args", {}) == {}


def test_mysql_receives_an_ssl_context_in_connect_args() -> None:
    connection = materialized("mysql")
    assert isinstance(connection["connect_args"]["ssl"], ssl.SSLContext)


def test_sqlserver_url_carries_encrypt_and_trust_settings() -> None:
    from urllib.parse import parse_qsl, urlsplit

    connection = materialized("sqlserver")
    query = dict(parse_qsl(urlsplit(connection["url"]).query))
    assert query["Encrypt"] == "yes"
    assert query["TrustServerCertificate"] == "no"


def test_sqlserver_verification_off_sets_trust_server_certificate() -> None:
    from urllib.parse import parse_qsl, urlsplit

    connection = materialized("sqlserver", verify_server=False)
    query = dict(parse_qsl(urlsplit(connection["url"]).query))
    assert query["TrustServerCertificate"] == "yes"


def test_sqlserver_server_name_becomes_host_name_in_certificate() -> None:
    from urllib.parse import parse_qsl, urlsplit

    connection = materialized("sqlserver", server_name="override.example")
    query = dict(parse_qsl(urlsplit(connection["url"]).query))
    assert query["HostNameInCertificate"] == "override.example"


def test_clickhouse_connect_args_carry_ca_and_client_material() -> None:
    certificate, key = client_pair()
    connection = materialized(
        "clickhouse", ca=ca_pem(), client_cert=certificate, client_key=key
    )
    connect_args = connection["connect_args"]
    assert connect_args["secure"] is True
    assert connect_args["ca_cert"].endswith("ca.pem")
    assert connect_args["client_cert"].endswith("client-cert.pem")
    assert connect_args["client_cert_key"].endswith("client-key.pem")
    assert connect_args["tls_mode"] == "mutual"


# ----------------------------------------------------------------------------------------------
# MongoDB
# ----------------------------------------------------------------------------------------------


def test_mongodb_adapter_forwards_tls_files(stub_driver_modules) -> None:
    captured = Capture()
    stub_driver_modules("pymongo", MongoClient=captured)
    certificate, key = client_pair()
    connection = materialized(
        "mongodb", ca=ca_pem(), client_cert=certificate, client_key=key
    )
    adapter = MongoDBAdapter("ds", connection)

    with adapter._client():
        pass

    kwargs = captured.calls[-1][1]
    assert kwargs["tls"] is True
    assert kwargs["tlsCAFile"].endswith("ca.pem")
    assert kwargs["tlsCertificateKeyFile"].endswith("client-combined.pem")


def test_mongodb_combined_file_holds_the_key_then_the_certificate(monkeypatch, tmp_path) -> None:
    """pymongo expects the private key first; the materializer must honour that order."""
    from qaneris.connections.tls_materializer import TLSMaterializer

    certificate, key = client_pair()
    connection = _resolved("mongodb", client_cert=certificate, client_key=key)

    with TLSMaterializer().materialize(connection) as parameters:
        from pathlib import Path

        combined = Path(parameters["tlsCertificateKeyFile"]).read_text(encoding="utf-8")

    assert combined.index("-----BEGIN PRIVATE KEY-----") < combined.index(
        "-----BEGIN CERTIFICATE-----"
    )


# ----------------------------------------------------------------------------------------------
# Redis
# ----------------------------------------------------------------------------------------------


def test_redis_adapter_forwards_tls_options_on_the_host_branch(stub_driver_modules) -> None:
    captured = Capture()
    stub_driver_modules("redis", Redis=captured)
    connection = materialized("redis", ca=ca_pem())
    adapter = RedisAdapter("ds", connection)

    adapter._client()

    kwargs = captured.calls[-1][1]
    assert kwargs["ssl"] is True
    assert kwargs["ssl_ca_certs"].endswith("ca.pem")
    assert kwargs["ssl_check_hostname"] is True
    assert kwargs["ssl_min_version"] is ssl.TLSVersion.TLSv1_2


def test_redis_adapter_forwards_tls_options_on_the_url_branch(stub_driver_modules) -> None:
    captured = Capture()
    stub_driver_modules("redis", Redis=_from_url_capture(captured))
    connection = materialized("redis", ca=ca_pem(), url="redis://localhost:6379/0")
    adapter = RedisAdapter("ds", connection)

    adapter._client()

    url, kwargs = captured.calls[-1][0][0], captured.calls[-1][1]
    # The materializer normalizes the scheme, so the URL branch is not quietly plaintext.
    assert url.startswith("rediss://")
    assert kwargs["ssl_ca_certs"].endswith("ca.pem")


def _from_url_capture(captured: Capture):
    """A Redis stand-in exposing ``from_url``, which is what the URL branch calls."""

    class _Redis:
        @staticmethod
        def from_url(*args: Any, **kwargs: Any) -> Any:
            return captured(*args, **kwargs)

    return _Redis


# ----------------------------------------------------------------------------------------------
# Cassandra, Neo4j, HTTP-based adapters
# ----------------------------------------------------------------------------------------------


def test_cassandra_adapter_passes_context_and_ssl_options(stub_driver_modules) -> None:
    captured = Capture()
    stub_driver_modules("cassandra.cluster", Cluster=captured)
    stub_driver_modules("cassandra.auth", PlainTextAuthProvider=lambda **_: None)
    connection = materialized("cassandra", ca=ca_pem(), server_name="override.example")
    adapter = CassandraAdapter("ds", connection)

    with adapter._session():
        pass

    kwargs = captured.calls[-1][1]
    assert isinstance(kwargs["ssl_context"], ssl.SSLContext)
    assert kwargs["ssl_options"] == {"server_hostname": "override.example"}


def test_neo4j_adapter_passes_an_ssl_context(monkeypatch) -> None:
    captured = Capture()
    monkeypatch.setattr("neo4j.GraphDatabase", _GraphDatabase(captured), raising=False)
    connection = materialized("neo4j", ca=ca_pem())
    adapter = Neo4jAdapter("ds", connection)

    with adapter._driver():
        pass

    assert isinstance(captured.calls[-1][1]["ssl_context"], ssl.SSLContext)


def test_neo4j_adapter_never_combines_a_plus_s_scheme_with_a_context(monkeypatch) -> None:
    captured = Capture()
    monkeypatch.setattr("neo4j.GraphDatabase", _GraphDatabase(captured), raising=False)
    connection = materialized("neo4j", ca=ca_pem(), url="neo4j+s://localhost:7687")
    adapter = Neo4jAdapter("ds", connection)

    with adapter._driver():
        pass

    uri = captured.calls[-1][0][0]
    assert uri.startswith("neo4j://")
    assert captured.calls[-1][1]["ssl_context"] is not None


class _GraphDatabase:
    def __init__(self, capture: Capture):
        self.capture = capture

    def driver(self, *args, **kwargs):
        self.capture(*args, **kwargs)
        return _StandIn()


def test_couchdb_adapter_verifies_with_the_ssl_context(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    import httpx

    original = httpx.Client

    def client(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    connection = materialized("couchdb", ca=ca_pem())
    adapter = CouchDBAdapter("ds", connection)

    with adapter._client():
        pass

    assert isinstance(captured["verify"], ssl.SSLContext)


def test_weaviate_adapter_verifies_with_the_ssl_context(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    import httpx

    original = httpx.Client

    def client(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    connection = materialized("weaviate", ca=ca_pem())
    adapter = WeaviateAdapter("ds", connection)

    with adapter._client():
        pass

    assert isinstance(captured["verify"], ssl.SSLContext)


# ----------------------------------------------------------------------------------------------
# Elasticsearch, OpenSearch, InfluxDB, Milvus, Qdrant
# ----------------------------------------------------------------------------------------------


def test_elasticsearch_adapter_passes_a_context_and_not_competing_parameters(monkeypatch) -> None:
    captured = Capture()
    monkeypatch.setattr("elasticsearch.Elasticsearch", captured, raising=False)
    connection = materialized("elasticsearch", ca=ca_pem())
    adapter = SearchAdapter("ds", connection)

    with adapter._client():
        pass

    kwargs = captured.calls[-1][1]
    assert isinstance(kwargs["ssl_context"], ssl.SSLContext)
    assert "ca_certs" not in kwargs and "client_cert" not in kwargs


def test_opensearch_adapter_passes_a_context_and_use_ssl(monkeypatch) -> None:
    captured = Capture()
    monkeypatch.setattr("opensearchpy.OpenSearch", captured, raising=False)
    connection = materialized("opensearch", ca=ca_pem())
    adapter = SearchAdapter("ds", connection)

    with adapter._client():
        pass

    kwargs = captured.calls[-1][1]
    assert isinstance(kwargs["ssl_context"], ssl.SSLContext)
    assert kwargs["use_ssl"] is True
    assert kwargs["verify_certs"] is True


def test_influxdb_adapter_passes_ca_and_cert_files(monkeypatch) -> None:
    captured = Capture()
    monkeypatch.setattr("influxdb_client.InfluxDBClient", captured, raising=False)
    certificate, key = client_pair()
    connection = materialized(
        "influxdb", ca=ca_pem(), client_cert=certificate, client_key=key
    )
    adapter = InfluxDBAdapter("ds", connection)

    with adapter._client():
        pass

    kwargs = captured.calls[-1][1]
    assert kwargs["verify_ssl"] is True
    assert kwargs["ssl_ca_cert"].endswith("ca.pem")
    assert kwargs["cert_file"].endswith("client-cert.pem")
    assert kwargs["cert_key_file"].endswith("client-key.pem")
    # The key was normalized to unencrypted PKCS#8, so no password is needed at runtime.
    assert kwargs["cert_key_password"] is None


def test_milvus_adapter_passes_pem_paths(monkeypatch) -> None:
    captured = Capture()
    monkeypatch.setattr("pymilvus.MilvusClient", captured, raising=False)
    certificate, key = client_pair()
    connection = materialized(
        "milvus", ca=ca_pem(), client_cert=certificate, client_key=key, server_name="override.example"
    )
    adapter = MilvusAdapter("ds", connection)

    with adapter._client():
        pass

    kwargs = captured.calls[-1][1]
    assert kwargs["secure"] is True
    assert kwargs["ca_pem_path"].endswith("ca.pem")
    assert kwargs["client_pem_path"].endswith("client-cert.pem")
    assert kwargs["client_key_path"].endswith("client-key.pem")
    assert kwargs["server_name"] == "override.example"


def test_qdrant_adapter_only_sets_https(stub_driver_modules) -> None:
    """Qdrant is system-trust-only: no context, no CA and no process-global trust mutation."""
    captured = Capture()
    stub_driver_modules("qdrant_client", QdrantClient=captured)
    connection = materialized("qdrant", url="http://localhost:6333")
    adapter = QdrantAdapter("ds", connection)

    with adapter._client():
        pass

    kwargs = captured.calls[-1][1]
    assert kwargs["https"] is True
    assert kwargs["url"].startswith("https://")
    assert "ssl_context" not in kwargs
    assert "ca_certs" not in kwargs


@pytest.mark.parametrize("driver", ["qdrant"])
def test_no_adapter_mutates_process_global_trust(driver: str, monkeypatch) -> None:
    """The materializer must never set a process-global trust variable."""
    import os

    from qaneris.connections.tls_materializer import TLSMaterializer

    names = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH")
    for name in names:
        monkeypatch.delenv(name, raising=False)

    with TLSMaterializer().materialize(_resolved(driver, url="http://localhost:6333")):
        pass

    for name in names:
        assert name not in os.environ
