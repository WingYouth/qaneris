"""Driver TLS matrix tests (RS-CONN-01B).

The matrix is an implementation specification, so the tests restate it independently: each of the
16 Roadshow drivers is asserted individually for its strategy, its four capability flags and its
materializer behavior. A driver silently dropped from the matrix, or a flag flipped without a
deliberate specification change, fails here.

The capability checks are behavioral rather than a re-read of the same dict: they ask the
materializer what it actually does with a request the flag claims to allow or refuse.
"""

from __future__ import annotations

import pytest

from qaneris.common.errors import TLSFeatureUnsupportedError
from qaneris.connections.tls_materializer import TLSMaterializer
from qaneris.connections.tls_matrix import TLS_DRIVER_MATRIX, TLSStrategy
from tests.unit._tls_helpers import (
    ca_certificate,
    client_certificate,
    key_pem,
    pem,
    private_key,
    resolved,
)

#: The 16 Roadshow target drivers and their fixed specification, restated from the card.
ROADSHOW_MATRIX = {
    "postgresql": ("postgres_files", True, True, False, True),
    "timescaledb": ("postgres_files", True, True, False, True),
    "mysql": ("ssl_context", True, True, False, True),
    "sqlserver": ("sqlserver_odbc", False, False, True, False),
    "mongodb": ("mongodb_files", True, True, False, False),
    "redis": ("redis_files", True, True, False, True),
    "couchdb": ("ssl_context", True, True, False, True),
    "cassandra": ("ssl_context", True, True, True, True),
    "clickhouse": ("clickhouse_files", True, True, True, False),
    "elasticsearch": ("ssl_context", True, True, False, True),
    "opensearch": ("ssl_context", True, True, True, True),
    "influxdb": ("influxdb_files", True, True, False, False),
    "neo4j": ("ssl_context", True, True, False, True),
    "milvus": ("milvus_files", True, True, True, False),
    "qdrant": ("system_trust_only", False, False, False, False),
    "weaviate": ("ssl_context", True, True, False, True),
}


def materializer() -> TLSMaterializer:
    return TLSMaterializer()


def test_the_matrix_covers_exactly_the_roadshow_drivers() -> None:
    assert set(TLS_DRIVER_MATRIX) == set(ROADSHOW_MATRIX)
    assert len(TLS_DRIVER_MATRIX) == 16


@pytest.mark.parametrize("driver", sorted(ROADSHOW_MATRIX))
def test_each_driver_selects_its_documented_strategy(driver: str) -> None:
    spec = TLS_DRIVER_MATRIX[driver]
    assert spec.strategy is TLSStrategy(ROADSHOW_MATRIX[driver][0])


@pytest.mark.parametrize("driver", sorted(ROADSHOW_MATRIX))
def test_each_driver_reports_the_documented_capabilities(driver: str) -> None:
    spec = TLS_DRIVER_MATRIX[driver]
    _, custom_ca, mtls, server_name, tls13 = ROADSHOW_MATRIX[driver]
    assert spec.custom_ca is custom_ca
    assert spec.mtls is mtls
    assert spec.server_name_override is server_name
    assert spec.tls13_control is tls13


@pytest.mark.parametrize("driver", sorted(ROADSHOW_MATRIX))
def test_custom_ca_behavior_matches_the_flag(driver: str) -> None:
    certificate, _ = ca_certificate()
    supported = ROADSHOW_MATRIX[driver][1]

    if supported:
        with materializer().materialize(resolved(driver, ca=pem(certificate))) as parameters:
            assert parameters
    else:
        with (
            pytest.raises(TLSFeatureUnsupportedError),
            materializer().materialize(resolved(driver, ca=pem(certificate))),
        ):
            pass


@pytest.mark.parametrize("driver", sorted(ROADSHOW_MATRIX))
def test_mtls_behavior_matches_the_flag(driver: str) -> None:
    key = private_key()
    certificate, _ = client_certificate(key)
    supported = ROADSHOW_MATRIX[driver][2]

    if supported:
        with materializer().materialize(
            resolved(driver, client_cert=pem(certificate), client_key=key_pem(key))
        ) as parameters:
            assert parameters
    else:
        with (
            pytest.raises(TLSFeatureUnsupportedError),
            materializer().materialize(
                resolved(driver, client_cert=pem(certificate), client_key=key_pem(key))
            ),
        ):
            pass


@pytest.mark.parametrize("driver", sorted(ROADSHOW_MATRIX))
def test_server_name_override_behavior_matches_the_flag(driver: str) -> None:
    supported = ROADSHOW_MATRIX[driver][3]

    if supported:
        with materializer().materialize(resolved(driver, server_name="other.example")):
            pass
    else:
        with (
            pytest.raises(TLSFeatureUnsupportedError),
            materializer().materialize(resolved(driver, server_name="other.example")),
        ):
            pass


@pytest.mark.parametrize("driver", sorted(ROADSHOW_MATRIX))
def test_tls13_behavior_matches_the_flag(driver: str) -> None:
    supported = ROADSHOW_MATRIX[driver][4]

    if supported:
        with materializer().materialize(resolved(driver, minimum_version="1.3")):
            pass
    else:
        with (
            pytest.raises(TLSFeatureUnsupportedError),
            materializer().materialize(resolved(driver, minimum_version="1.3")),
        ):
            pass


@pytest.mark.parametrize("driver", sorted(ROADSHOW_MATRIX))
def test_tls12_is_always_accepted(driver: str) -> None:
    """Every driver in the matrix can be asked for the baseline floor, even without 1.3 control."""
    with materializer().materialize(resolved(driver, minimum_version="1.2")):
        pass


def test_sqlserver_is_server_tls_only() -> None:
    spec = TLS_DRIVER_MATRIX["sqlserver"]
    assert spec.strategy is TLSStrategy.SQLSERVER_ODBC
    assert not spec.custom_ca and not spec.mtls and not spec.tls13_control
    assert spec.server_name_override


def test_qdrant_is_system_trust_only() -> None:
    spec = TLS_DRIVER_MATRIX["qdrant"]
    assert spec.strategy is TLSStrategy.SYSTEM_TRUST_ONLY
    assert not spec.custom_ca and not spec.mtls
    assert not spec.server_name_override and not spec.tls13_control


def test_timescaledb_shares_the_postgres_strategy() -> None:
    """TimescaleDB is PostgreSQL underneath, so the two must not drift apart."""
    assert (
        TLS_DRIVER_MATRIX["timescaledb"].strategy
        is TLS_DRIVER_MATRIX["postgresql"].strategy
        is TLSStrategy.POSTGRES_FILES
    )
