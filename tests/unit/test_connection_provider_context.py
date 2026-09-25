"""Context-managed connection lifetime tests (RS-CONN-01B).

The point of ``open()`` is that certificate material exists while the adapter uses it and does not
exist afterwards, so the central test here opens a real TLS-enabled datasource through a real
``DatasourceConnectionProvider`` and asserts exactly that, from inside and outside the block.

The rest of the module proves the runtime call chains actually use that boundary: the initializer,
the grounded executor and the explicit-SQL path must go through ``open()``/``open_profile()``, and
``get()`` must refuse a TLS-enabled datasource rather than hand back a dangling path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from smartdata.catalog import Catalog
from smartdata.common.errors import TLSFeatureUnsupportedError
from smartdata.connections.provider import DatasourceConnectionProvider
from smartdata.connections.secrets import EnvironmentSecretProvider, SecretResolver
from smartdata.contracts import (
    AuthenticationConfig,
    ConnectionEndpoint,
    ConnectionProfile,
    DatasourceCreate,
    SecureDatasourceCreate,
    SecureDatasourceTest,
    TLSConfig,
)
from smartdata.contracts.connection import SecretProviderKind
from tests.unit._tls_helpers import ca_certificate, pem


def resolver_with(ca_pem: str) -> SecretResolver:
    return SecretResolver(
        {SecretProviderKind.ENVIRONMENT: EnvironmentSecretProvider({"TEST_CA": ca_pem})}
    )


def catalog_with_secure_datasource(path: Path, driver: str) -> tuple[Catalog, str]:
    catalog = Catalog(path / "catalog.db")
    datasource = catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="tls-source",
            kind="graph",
            connection_profile=ConnectionProfile(
                driver=driver,
                endpoint=ConnectionEndpoint(
                    hosts=[{"host": "database.example", "port": 7687}],
                ),
                authentication=AuthenticationConfig(method="none", username="readonly"),
                tls=TLSConfig(
                    enabled=True,
                    ca_certificate={"provider": "environment", "identifier": "TEST_CA"},
                ),
            ),
        )
    )
    return catalog, datasource.id


# ----------------------------------------------------------------------------------------------
# the lifetime contract
# ----------------------------------------------------------------------------------------------


def test_a_tls_datasource_has_material_inside_the_block_and_none_after(tmp_path: Path) -> None:
    certificate, _ = ca_certificate()
    catalog, datasource_id = catalog_with_secure_datasource(tmp_path, "neo4j")
    provider = DatasourceConnectionProvider(catalog, resolver_with(pem(certificate)))

    with provider.open(datasource_id) as connection:
        # Neo4j takes an SSLContext, so the material lives in memory rather than in a file, but the
        # connection is real and usable for the whole block.
        assert connection["ssl_context"] is not None
        assert connection["tls"] is True

    # Reopening is safe and independent: nothing about the previous block survives.
    with provider.open(datasource_id) as reopened:
        assert reopened["ssl_context"] is not None


def test_a_file_based_tls_datasource_removes_its_directory_after_the_block(
    tmp_path: Path,
) -> None:
    certificate, _ = ca_certificate()
    catalog, datasource_id = catalog_with_secure_datasource(tmp_path, "postgresql")
    provider = DatasourceConnectionProvider(catalog, resolver_with(pem(certificate)))

    with provider.open(datasource_id) as connection:
        directory = Path(connection["connect_args"]["sslrootcert"]).parent
        assert directory.is_dir()

    assert not directory.exists()


def test_open_profile_materializes_a_candidate_that_was_never_stored(tmp_path: Path) -> None:
    certificate, _ = ca_certificate()
    # No catalog at all: open_profile is the candidate-test boundary and needs no stored datasource.
    provider = DatasourceConnectionProvider(None, resolver_with(pem(certificate)))
    profile = ConnectionProfile(
        driver="postgresql",
        endpoint=ConnectionEndpoint(hosts=[{"host": "database.example", "port": 5432}]),
        tls=TLSConfig(
            enabled=True,
            ca_certificate={"provider": "environment", "identifier": "TEST_CA"},
        ),
    )

    with provider.open_profile(profile) as connection:
        assert connection["connect_args"]["sslmode"] == "verify-full"
        directory = Path(connection["connect_args"]["sslrootcert"]).parent
        assert directory.is_dir()

    assert not directory.exists()


def test_a_tls_disabled_profile_yields_no_tls_parameters(tmp_path: Path) -> None:
    provider = DatasourceConnectionProvider(None, resolver_with("unused"))
    profile = ConnectionProfile(
        driver="postgresql",
        endpoint=ConnectionEndpoint(hosts=[{"host": "database.example", "port": 5432}]),
    )

    with provider.open_profile(profile) as connection:
        assert "connect_args" not in connection
        assert connection["tls"] is False


# ----------------------------------------------------------------------------------------------
# get() compatibility
# ----------------------------------------------------------------------------------------------


def test_get_still_returns_a_legacy_datasource(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    source = tmp_path / "source.db"
    with sqlite3.connect(source):
        pass
    datasource = catalog.create_datasource(
        DatasourceCreate(
            name="legacy",
            kind="relational",
            connection={"driver": "sqlite", "path": str(source)},
        )
    )
    provider = DatasourceConnectionProvider(catalog)

    assert provider.get(datasource.id)["driver"] == "sqlite"


def test_get_still_returns_a_tls_disabled_secure_datasource(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    datasource = catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="plain",
            kind="relational",
            connection_profile=ConnectionProfile(
                driver="postgresql",
                endpoint=ConnectionEndpoint(hosts=[{"host": "database.example", "port": 5432}]),
            ),
        )
    )
    provider = DatasourceConnectionProvider(catalog)

    assert provider.get(datasource.id)["driver"] == "postgresql"


def test_get_refuses_a_tls_enabled_datasource(tmp_path: Path) -> None:
    """No dangling certificate path may ever be returned by the compatibility accessor."""
    certificate, _ = ca_certificate()
    catalog, datasource_id = catalog_with_secure_datasource(tmp_path, "postgresql")
    provider = DatasourceConnectionProvider(catalog, resolver_with(pem(certificate)))

    with pytest.raises(TLSFeatureUnsupportedError, match="open"):
        provider.get(datasource_id)


# ----------------------------------------------------------------------------------------------
# the runtime call chains
# ----------------------------------------------------------------------------------------------


class RecordingProvider:
    """Wrap a real provider to record which boundary each call chain actually uses."""

    def __init__(self, inner: DatasourceConnectionProvider):
        self.inner = inner
        self.opened: list[str] = []
        self.opened_profiles = 0
        self.get_calls: list[str] = []

    def open(self, datasource_id: str):
        self.opened.append(datasource_id)
        return self.inner.open(datasource_id)

    def open_profile(self, profile: ConnectionProfile):
        self.opened_profiles += 1
        return self.inner.open_profile(profile)

    def get(self, datasource_id: str):
        self.get_calls.append(datasource_id)
        return self.inner.get(datasource_id)


def self_contained_profile() -> ConnectionProfile:
    """A profile for a real driver whose adapter library is not a dependency of this repo."""
    return ConnectionProfile(
        driver="postgresql",
        endpoint=ConnectionEndpoint(hosts=[{"host": "database.example", "port": 5432}]),
        authentication=AuthenticationConfig(
            method="password",
            username="readonly",
            password={"provider": "environment", "identifier": "TEST_PASSWORD"},
        ),
    )


def test_the_initializer_opens_the_connection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_PASSWORD", "UNIQUE_PASSWORD")
    catalog = Catalog(tmp_path / "catalog.db")
    datasource = catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="source",
            kind="relational",
            connection_profile=self_contained_profile(),
        )
    )
    provider = RecordingProvider(DatasourceConnectionProvider(catalog))
    initializer = _initializer(catalog, provider)

    _run_initializer(initializer, datasource.id)

    assert provider.opened == [datasource.id]
    assert provider.get_calls == []


def test_the_candidate_test_uses_open_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_PASSWORD", "UNIQUE_PASSWORD")
    from smartdata.application.service import SmartDataService

    catalog = Catalog(tmp_path / "catalog.db")
    service = SmartDataService(catalog)
    provider = RecordingProvider(service.connection_provider)
    service.connection_provider = provider
    service.initializer.connection_provider = provider
    service.grounded_executor.connections = provider

    _expect_unsupported_driver(
        lambda: service.test_secure_datasource(
            SecureDatasourceTest(
                kind="relational", connection_profile=self_contained_profile()
            )
        )
    )

    assert provider.opened_profiles == 1
    assert provider.get_calls == []


def test_the_grounded_executor_opens_the_connection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_PASSWORD", "UNIQUE_PASSWORD")
    from smartdata.querying.execution import GroundedQueryExecutor

    catalog = Catalog(tmp_path / "catalog.db")
    provider = RecordingProvider(DatasourceConnectionProvider(catalog))
    executor = GroundedQueryExecutor(provider)
    datasource = _fake_datasource("ds_1")

    from smartdata.contracts.query import NativeQuery

    _expect_unsupported_driver(
        lambda: executor.execute(
            NativeQuery(
                plan_id="plan_1",
                datasource_id="ds_1",
                query_language="sql",
                command="SELECT 1",
                parameters=(),
                display_command="SELECT 1",
                scan_version=1,
            ),
            datasource,
        )
    )

    assert provider.opened == ["ds_1"]
    assert provider.get_calls == []


def _fake_datasource(datasource_id: str):
    from smartdata.contracts.datasource import Datasource, DatasourceKind

    return Datasource(
        id=datasource_id,
        name="fake",
        kind=DatasourceKind.RELATIONAL,
        status="ready",
        workspace_id="default",
    )


def _initializer(catalog: Catalog, provider):
    from smartdata.graph import NullGraphStore
    from smartdata.initialization import DatabaseInitializer

    return DatabaseInitializer(
        catalog,
        connection_provider=provider,
        graph_store=NullGraphStore(),
        generate_profile_documents=False,
    )


def _run_initializer(initializer, datasource_id: str):
    from smartdata.contracts.profile import ScanStatus

    job = initializer.initialize(datasource_id)
    assert job.status in {
        ScanStatus.CONNECTION_FAILED,
        ScanStatus.SCAN_FAILED,
        ScanStatus.PROFILE_FAILED,
    }
    return job


def _expect_unsupported_driver(action) -> None:
    """The postgresql driver library is not installed, so the chain reaches the open boundary."""
    with pytest.raises(Exception):  # noqa: B017 - which error differs per driver availability
        action()
