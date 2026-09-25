"""Certificate material and connection fixtures shared by the TLS tests (RS-CONN-01B).

Every certificate and key is generated inside the test process. Nothing is committed, so there is
no fixture that can expire and turn a healthy suite red, and nothing in the repository to leak.

The helpers mirror ``tests/unit/test_certificate_validator.py``: a self-signed certificate for one
purpose, PEM/DER encoders and a key encoder that can apply a password.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from qaneris.contracts.connection import (
    ConnectionEndpoint,
    ConnectionProfile,
    DeploymentMode,
    ResolvedConnection,
    SecretProviderKind,
    SecretReference,
    TLSConfig,
)

#: Distinct high-entropy markers so a leak test can assert one specific value never escaped.
CA_MARKER = "UNIQUE_TEST_CA_CERTIFICATE_MARKER"
CLIENT_CERT_MARKER = "UNIQUE_TEST_CLIENT_CERTIFICATE_MARKER"
CLIENT_KEY_MARKER = "UNIQUE_TEST_CLIENT_PRIVATE_KEY_MARKER"


def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def certificate(
    *,
    ca: bool,
    extended_usage: list[x509.ObjectIdentifier] | None = None,
    not_before: dt.datetime | None = None,
    not_after: dt.datetime | None = None,
    key: Any | None = None,
) -> tuple[x509.Certificate, Any]:
    issuer_key = key or private_key()
    now = dt.datetime.now(dt.UTC)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Qaneris Test CA" if ca else "client")]
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(issuer_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - dt.timedelta(days=1))
        .not_valid_after(not_after or now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if extended_usage:
        builder = builder.add_extension(x509.ExtendedKeyUsage(extended_usage), critical=False)
    return builder.sign(issuer_key, hashes.SHA256()), issuer_key


def ca_certificate() -> tuple[x509.Certificate, Any]:
    return certificate(ca=True)


def client_certificate(
    key: Any | None = None,
) -> tuple[x509.Certificate, Any]:
    """A client certificate, optionally bound to an existing key so a pair can be produced."""
    return certificate(ca=False, extended_usage=[ExtendedKeyUsageOID.CLIENT_AUTH], key=key)


def expired_certificate() -> tuple[x509.Certificate, Any]:
    now = dt.datetime.now(dt.UTC)
    return certificate(
        ca=True,
        not_before=now - dt.timedelta(days=30),
        not_after=now - dt.timedelta(days=1),
    )


def pem(value: Any) -> str:
    return value.public_bytes(serialization.Encoding.PEM).decode("utf-8")


def key_pem(key: Any, *, password: bytes | None = None) -> str:
    encryption = (
        serialization.BestAvailableEncryption(password)
        if password is not None
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    ).decode("utf-8")


def resolved(
    driver: str,
    *,
    tls_enabled: bool = True,
    verify_server: bool = True,
    server_name: str | None = None,
    minimum_version: str = "1.2",
    ca: str | None = None,
    client_cert: str | None = None,
    client_key: str | None = None,
    client_key_password: str | None = None,
    hosts: list[dict[str, Any]] | None = None,
    url: str | None = None,
    path: str | None = None,
    database: str | None = None,
    secrets: dict[str, str] | None = None,
    options: dict[str, Any] | None = None,
) -> ResolvedConnection:
    """A resolved connection built directly, so materializer tests need no secret provider."""
    material: dict[str, str] = {}
    if ca is not None:
        material["ca_certificate"] = ca
    if client_cert is not None:
        material["client_certificate"] = client_cert
    if client_key is not None:
        material["client_private_key"] = client_key
    if client_key_password is not None:
        material["client_private_key_password"] = client_key_password
    return ResolvedConnection(
        driver=driver,
        deployment_mode=DeploymentMode.LOCAL,
        endpoint=ConnectionEndpoint(
            hosts=[{"host": "database.example", "port": 5432}] if hosts is None else hosts,
            url=url,
            path=path,
            database=database,
        ),
        username="readonly",
        secrets=secrets or {},
        tls_enabled=tls_enabled,
        verify_server=verify_server,
        server_name=server_name,
        minimum_tls_version=minimum_version,
        tls_material=material,
        options=options or {},
    )


def secure_profile(
    driver: str,
    *,
    tls_enabled: bool = True,
    verify_server: bool = True,
    server_name: str | None = None,
    minimum_version: str = "1.2",
    ca: SecretReference | None = None,
    client_cert: SecretReference | None = None,
    client_key: SecretReference | None = None,
    url: str | None = None,
    path: str | None = None,
    hosts: list[dict[str, Any]] | None = None,
) -> ConnectionProfile:
    """A stored profile, used to prove the provider wires resolve → materialize together."""
    return ConnectionProfile(
        driver=driver,
        endpoint=ConnectionEndpoint(
            hosts=[{"host": "database.example", "port": 5432}] if hosts is None else hosts,
            url=url,
            path=path,
        ),
        authentication={"method": "none", "username": "readonly"},
        tls=TLSConfig(
            enabled=tls_enabled,
            verify_server=verify_server,
            server_name=server_name,
            minimum_version=minimum_version,  # type: ignore[arg-type]
            ca_certificate=ca,
            client_certificate=client_cert,
            client_private_key=client_key,
        ),
    )


def environment_reference(name: str) -> SecretReference:
    return SecretReference(provider=SecretProviderKind.ENVIRONMENT, identifier=name)
