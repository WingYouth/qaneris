"""TLSMaterializer unit tests (RS-CONN-01B).

Two properties are asserted as much as outcomes, because they are what the class exists for:

* **Lifetime.** Certificate files exist inside the ``with`` block and are gone after it - on the
  success path and on every failure path, including an exception raised by the adapter using the
  connection.
* **Containment.** No certificate body, private key or key password appears in the yielded adapter
  parameters or in a log record. A path inside the private directory may appear; the material may
  not.

Every certificate is generated in-process; nothing is committed and nothing can expire.
"""

from __future__ import annotations

import logging
import ssl
import tempfile
from pathlib import Path
from typing import Any

import pytest

from smartdata.common.errors import (
    TLSFeatureUnsupportedError,
    TLSMaterializationError,
)
from smartdata.connections.tls_materializer import TLSMaterializer
from tests.unit._tls_helpers import (
    CA_MARKER,
    CLIENT_CERT_MARKER,
    CLIENT_KEY_MARKER,
    ca_certificate,
    client_certificate,
    expired_certificate,
    key_pem,
    pem,
    private_key,
    resolved,
)

#: Drivers whose strategy produces file paths, one per file-based strategy.
FILE_DRIVERS = ["postgresql", "mongodb", "redis", "clickhouse", "influxdb", "milvus"]


def materializer() -> TLSMaterializer:
    return TLSMaterializer()


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _walk_strings(child)]
    if isinstance(value, list | tuple):
        return [item for child in value for item in _walk_strings(child)]
    return []


def _material_directory(parameters: dict[str, Any]) -> Path | None:
    """Find the directory holding this connection's material, wherever the strategy put it."""
    for value in _walk_strings(parameters):
        if value.endswith(("ca.pem", "client-cert.pem", "client-key.pem", "client-combined.pem")):
            return Path(value).parent
    return None


# ----------------------------------------------------------------------------------------------
# tls disabled
# ----------------------------------------------------------------------------------------------


def test_tls_disabled_yields_nothing_and_creates_no_directory() -> None:
    """A non-TLS connection must not gain a temp directory because this module exists."""
    before = set(Path(tempfile.gettempdir()).glob("smartdata-tls-*"))

    with materializer().materialize(resolved("postgresql", tls_enabled=False)) as parameters:
        assert parameters == {}
        assert set(Path(tempfile.gettempdir()).glob("smartdata-tls-*")) == before

    assert set(Path(tempfile.gettempdir()).glob("smartdata-tls-*")) == before


# ----------------------------------------------------------------------------------------------
# system trust and minimum version
# ----------------------------------------------------------------------------------------------


def test_system_trust_connection_builds_a_verifying_context() -> None:
    with materializer().materialize(resolved("neo4j")) as parameters:
        context = parameters["ssl_context"]
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert context.minimum_version is ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize(
    ("minimum", "expected"),
    [("1.2", ssl.TLSVersion.TLSv1_2), ("1.3", ssl.TLSVersion.TLSv1_3)],
)
def test_minimum_version_is_applied(minimum: str, expected: ssl.TLSVersion) -> None:
    with materializer().materialize(resolved("neo4j", minimum_version=minimum)) as parameters:
        assert parameters["ssl_context"].minimum_version is expected


def test_unverified_connection_turns_verification_off_explicitly() -> None:
    with materializer().materialize(resolved("neo4j", verify_server=False)) as parameters:
        context = parameters["ssl_context"]
        assert context.check_hostname is False
        assert context.verify_mode is ssl.CERT_NONE


# ----------------------------------------------------------------------------------------------
# custom CA and mTLS
# ----------------------------------------------------------------------------------------------


def test_custom_ca_is_loaded_into_the_context() -> None:
    certificate, _ = ca_certificate()

    with materializer().materialize(resolved("neo4j", ca=pem(certificate))) as parameters:
        context = parameters["ssl_context"]
        stats = context.cert_store_stats()
        assert stats["x509_ca"] >= 1


def test_mtls_loads_the_client_pair_into_the_context() -> None:
    key = private_key()
    certificate, _ = client_certificate(key)

    with materializer().materialize(
        resolved("cassandra", client_cert=pem(certificate), client_key=key_pem(key))
    ) as parameters:
        assert isinstance(parameters["ssl_context"], ssl.SSLContext)


def test_postgres_custom_ca_becomes_a_file_path_not_a_pem_body() -> None:
    certificate, _ = ca_certificate()

    with materializer().materialize(resolved("postgresql", ca=pem(certificate))) as parameters:
        connect_args = parameters["connect_args"]
        assert connect_args["sslmode"] == "verify-full"
        assert connect_args["sslrootcert"].endswith("ca.pem")
        assert "-----BEGIN CERTIFICATE-----" not in str(connect_args)


def test_postgres_without_verification_uses_require() -> None:
    with materializer().materialize(
        resolved("postgresql", verify_server=False)
    ) as parameters:
        assert parameters["connect_args"]["sslmode"] == "require"


def test_postgres_minimum_version_is_expressed_for_psycopg() -> None:
    with materializer().materialize(
        resolved("postgresql", minimum_version="1.3")
    ) as parameters:
        assert parameters["connect_args"]["ssl_min_protocol_version"] == "TLSv1.3"


# ----------------------------------------------------------------------------------------------
# rejection
# ----------------------------------------------------------------------------------------------


def test_bad_certificate_is_rejected() -> None:
    with (
        pytest.raises(TLSMaterializationError),
        materializer().materialize(resolved("postgresql", ca="not a certificate")),
    ):
        pass


def test_expired_certificate_is_rejected() -> None:
    certificate, _ = expired_certificate()

    with (
        pytest.raises(TLSMaterializationError, match="expired"),
        materializer().materialize(resolved("postgresql", ca=pem(certificate))),
    ):
        pass


def test_mismatched_client_pair_is_rejected_before_the_driver_runs() -> None:
    certificate, _ = client_certificate(private_key())
    other_key = private_key()

    with (
        pytest.raises(TLSMaterializationError, match="same key pair"),
        materializer().materialize(
            resolved("cassandra", client_cert=pem(certificate), client_key=key_pem(other_key))
        ),
    ):
        pass


def test_encrypted_key_without_password_is_rejected() -> None:
    key = private_key()
    certificate, _ = client_certificate(key)

    with pytest.raises(TLSMaterializationError), materializer().materialize(
        resolved(
            "cassandra",
            client_cert=pem(certificate),
            client_key=key_pem(key, password=b"UNIQUE_KEY_PASSWORD"),
        )
    ):
        pass


def test_unsupported_custom_ca_fails_closed() -> None:
    certificate, _ = ca_certificate()

    with (
        pytest.raises(TLSFeatureUnsupportedError, match="custom CA"),
        materializer().materialize(resolved("sqlserver", ca=pem(certificate))),
    ):
        pass


def test_unsupported_mtls_fails_closed() -> None:
    key = private_key()
    certificate, _ = client_certificate(key)

    with (
        pytest.raises(TLSFeatureUnsupportedError, match="client certificate"),
        materializer().materialize(
            resolved("qdrant", client_cert=pem(certificate), client_key=key_pem(key))
        ),
    ):
        pass


def test_unsupported_tls13_fails_closed() -> None:
    with (
        pytest.raises(TLSFeatureUnsupportedError, match="TLS 1.3"),
        materializer().materialize(resolved("mongodb", minimum_version="1.3")),
    ):
        pass


def test_server_name_override_on_a_driver_without_support_fails_closed() -> None:
    with (
        pytest.raises(TLSFeatureUnsupportedError, match="server name"),
        materializer().materialize(resolved("postgresql", server_name="other.example")),
    ):
        pass


def test_server_name_equal_to_the_endpoint_host_is_accepted() -> None:
    """A driver without override support still accepts the endpoint's own hostname."""
    with materializer().materialize(
        resolved("postgresql", server_name="database.example")
    ) as parameters:
        assert parameters["connect_args"]["sslmode"] == "verify-full"


def test_driver_outside_the_matrix_is_rejected() -> None:
    with (
        pytest.raises(TLSFeatureUnsupportedError, match="matrix"),
        materializer().materialize(resolved("hbase")),
    ):
        pass


# ----------------------------------------------------------------------------------------------
# file permissions and lifetime
# ----------------------------------------------------------------------------------------------


def test_files_are_written_inside_a_private_directory() -> None:
    certificate, _ = ca_certificate()

    with materializer().materialize(resolved("postgresql", ca=pem(certificate))) as parameters:
        directory = Path(parameters["connect_args"]["sslrootcert"]).parent
        assert directory.name.startswith("smartdata-tls-")
        assert directory.stat().st_mode & 0o777 == 0o700
        for child in directory.iterdir():
            assert child.stat().st_mode & 0o777 == 0o600


def test_paths_exist_inside_the_block_and_are_gone_after() -> None:
    certificate, _ = ca_certificate()

    with materializer().materialize(resolved("postgresql", ca=pem(certificate))) as parameters:
        directory = Path(parameters["connect_args"]["sslrootcert"]).parent
        assert directory.is_dir()
        assert (directory / "ca.pem").is_file()

    assert not directory.exists()


def test_an_adapter_exception_still_cleans_up() -> None:
    certificate, _ = ca_certificate()
    directory: Path | None = None

    with (
        pytest.raises(RuntimeError, match="adapter exploded"),
        materializer().materialize(resolved("postgresql", ca=pem(certificate))) as parameters,
    ):
        directory = Path(parameters["connect_args"]["sslrootcert"]).parent
        raise RuntimeError("adapter exploded")

    assert directory is not None
    assert not directory.exists()


def test_a_failure_after_the_yield_still_cleans_up() -> None:
    """A connection failure raised by the driver inside the block must not leak a directory."""
    key = private_key()
    certificate, _ = client_certificate(key)
    directory: Path | None = None

    with pytest.raises(ConnectionError), materializer().materialize(
        resolved("postgresql", client_cert=pem(certificate), client_key=key_pem(key))
    ) as parameters:
        directory = Path(parameters["connect_args"]["sslcert"]).parent
        raise ConnectionError("driver refused the connection")

    assert directory is not None
    assert not directory.exists()


@pytest.mark.parametrize("driver", FILE_DRIVERS)
def test_every_file_based_driver_cleans_up(driver: str) -> None:
    certificate, _ = ca_certificate()
    directory: Path | None = None

    with materializer().materialize(resolved(driver, ca=pem(certificate))) as parameters:
        directory = _material_directory(parameters)
        assert directory is not None and directory.is_dir()

    assert directory is not None
    assert not directory.exists()


# ----------------------------------------------------------------------------------------------
# containment
# ----------------------------------------------------------------------------------------------


def test_no_raw_pem_appears_in_the_adapter_parameters() -> None:
    key = private_key()
    certificate, _ = client_certificate(key)
    ca, _ = ca_certificate()

    with materializer().materialize(
        resolved(
            "postgresql",
            ca=pem(ca),
            client_cert=pem(certificate),
            client_key=key_pem(key),
        )
    ) as parameters:
        rendered = str(parameters)
        assert "-----BEGIN CERTIFICATE-----" not in rendered
        assert "-----BEGIN PRIVATE KEY-----" not in rendered
        assert "-----BEGIN RSA PRIVATE KEY-----" not in rendered


def test_no_raw_pem_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    key = private_key()
    certificate, _ = client_certificate(key)
    ca, _ = ca_certificate()

    with caplog.at_level(logging.DEBUG), materializer().materialize(
        resolved(
            "postgresql",
            ca=pem(ca),
            client_cert=pem(certificate),
            client_key=key_pem(key),
        )
    ):
        pass

    for record in caplog.records:
        rendered = record.getMessage()
        assert "-----BEGIN CERTIFICATE-----" not in rendered
        assert "-----BEGIN PRIVATE KEY-----" not in rendered


def test_markers_never_appear_in_the_adapter_parameters() -> None:
    """The generated material carries the markers only when they are installed as content."""
    key = private_key()
    certificate, _ = client_certificate(key)

    with materializer().materialize(
        resolved("postgresql", client_cert=pem(certificate), client_key=key_pem(key))
    ) as parameters:
        rendered = str(parameters)
        assert CLIENT_CERT_MARKER not in rendered
        assert CLIENT_KEY_MARKER not in rendered
        assert CA_MARKER not in rendered


def test_a_rejected_certificate_error_carries_no_material() -> None:
    key = private_key()
    certificate, _ = client_certificate(private_key())

    with pytest.raises(TLSMaterializationError) as raised, materializer().materialize(
        resolved("cassandra", client_cert=pem(certificate), client_key=key_pem(key))
    ):
        pass

    message = str(raised.value)
    assert "-----BEGIN CERTIFICATE-----" not in message
    assert "-----BEGIN PRIVATE KEY-----" not in message
