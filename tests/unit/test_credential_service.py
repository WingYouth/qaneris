"""Unit tests for the credential service (RS-CRED-01A).

The service is the layer that decides whether material is valid, normalizes it, and refuses to
destroy a secret something still depends on. These tests cover those three jobs and the boundary
between them - including the one that matters most in a report: a marker that is written as a secret
must not appear in the catalog, in a log line, in public metadata or in an exception.

Certificates are generated in-process; nothing is committed as a fixture.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from qaneris.catalog import Catalog
from qaneris.common.errors import (
    CertificateKeyMismatchError,
    CertificateValidationError,
    ManagedSecretInUseError,
    ManagedSecretNotFoundError,
)
from qaneris.connections.credential_service import CredentialService
from qaneris.connections.managed_store import ManagedCredentialStore
from qaneris.contracts.connection import (
    AuthenticationConfig,
    AuthenticationMethod,
    ConnectionEndpoint,
    ConnectionProfile,
    SecretProviderKind,
    SecretReference,
    SecureDatasourceCreate,
    TLSConfig,
)
from qaneris.contracts.credentials import ManagedSecretKind
from qaneris.contracts.datasource import DatasourceKind

PASSWORD_MARKER = "UNIQUE_PASSWORD_MARKER"
TOKEN_MARKER = "UNIQUE_TOKEN_MARKER"
API_KEY_MARKER = "UNIQUE_API_KEY_MARKER"
KEY_PASSWORD_MARKER = "UNIQUE_KEY_PASSWORD_MARKER"


def certificate(*, ca: bool) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Qaneris Test CA" if ca else "client")])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if not ca:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
    return builder.sign(key, hashes.SHA256()), key


def certificate_pem(*, ca: bool) -> str:
    built, _ = certificate(ca=ca)
    return built.public_bytes(serialization.Encoding.PEM).decode("utf-8")


def key_pem(password: str | None = None) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encryption = (
        serialization.BestAvailableEncryption(password.encode()) if password else serialization.NoEncryption()
    )
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    ).decode("utf-8")


def client_pair(
    *, password: str | None = None, certificate_der: bool = False, key_der: bool = False
) -> tuple[str | bytes, str | bytes]:
    """A matching client certificate and its own private key, in PEM or DER form."""
    built, key = certificate(ca=False)
    if certificate_der:
        cert: str | bytes = built.public_bytes(serialization.Encoding.DER)
    else:
        cert = built.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    encryption = (
        serialization.BestAvailableEncryption(password.encode())
        if password
        else serialization.NoEncryption()
    )
    encoded = key.private_bytes(
        serialization.Encoding.DER if key_der else serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        encryption,
    )
    return cert, encoded if key_der else encoded.decode("utf-8")


def stored_secret_files(service: CredentialService) -> list[Path]:
    directory = service.store.store_dir
    return sorted(directory.iterdir()) if directory.exists() else []


@pytest.fixture
def service(tmp_path: Path) -> CredentialService:
    store = ManagedCredentialStore.from_environment(
        {
            "QANERIS_SECRET_STORE_DIR": str(tmp_path / "secrets"),
            "QANERIS_MASTER_KEY": ManagedCredentialStore.generate_master_key(),
        }
    )
    return CredentialService(store, Catalog(tmp_path / "catalog.db"))


def profile_referencing(
    *references: SecretReference,
) -> ConnectionProfile:
    """A profile whose password and TLS material point at the given managed secrets."""
    password = references[0] if references else None
    tls_references = references[1:]
    return ConnectionProfile(
        driver="postgresql",
        endpoint=ConnectionEndpoint(hosts=[{"host": "127.0.0.1", "port": 5432}]),
        authentication=AuthenticationConfig(
            method=AuthenticationMethod.PASSWORD, username="readonly", password=password
        ),
        tls=TLSConfig(
            enabled=True,
            ca_certificate=tls_references[0] if tls_references else None,
            # A client pair is only added when a third reference is supplied, because the TLS
            # contract requires the certificate and its key to be configured together.
            client_certificate=tls_references[1] if len(tls_references) > 2 else None,
            client_private_key=tls_references[2] if len(tls_references) > 2 else None,
        ),
    )


def managed(secret_id: str) -> SecretReference:
    return SecretReference(provider=SecretProviderKind.MANAGED, identifier=secret_id)


# --------------------------------------------------------------------------------------------
# creating every kind
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "marker"),
    [
        (ManagedSecretKind.PASSWORD, PASSWORD_MARKER),
        (ManagedSecretKind.TOKEN, TOKEN_MARKER),
        (ManagedSecretKind.API_KEY, API_KEY_MARKER),
        (ManagedSecretKind.CLIENT_PRIVATE_KEY_PASSWORD, KEY_PASSWORD_MARKER),
    ],
)
def test_a_plain_secret_is_stored_verbatim(
    service: CredentialService, kind: ManagedSecretKind, marker: str
) -> None:
    info = service.create_secret(kind, marker)

    assert info.kind is kind
    assert info.metadata == {}
    assert service.store.resolve(info.id) == marker


@pytest.mark.parametrize("kind", [ManagedSecretKind.PASSWORD, ManagedSecretKind.TOKEN, ManagedSecretKind.API_KEY])
def test_an_empty_plain_secret_is_refused(service: CredentialService, kind: ManagedSecretKind) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        service.create_secret(kind, "")


@pytest.mark.parametrize(
    ("kind", "is_ca"),
    [
        (ManagedSecretKind.CA_CERTIFICATE, True),
        (ManagedSecretKind.CLIENT_CERTIFICATE, False),
    ],
)
def test_a_certificate_is_normalized_before_it_is_stored(
    service: CredentialService, kind: ManagedSecretKind, is_ca: bool
) -> None:
    info = service.create_secret(kind, certificate_pem(ca=is_ca))

    stored = service.store.resolve(info.id)
    assert stored.startswith("-----BEGIN CERTIFICATE-----")
    assert info.metadata["subject"] == ("CN=Qaneris Test CA" if is_ca else "CN=client")
    assert "sha256_fingerprint" in info.metadata
    assert "-----BEGIN" not in json.dumps(info.metadata)


def test_an_unusable_certificate_is_refused_before_anything_is_written(
    service: CredentialService,
) -> None:
    with pytest.raises(CertificateValidationError):
        service.create_secret(ManagedSecretKind.CA_CERTIFICATE, "not a certificate")

    assert list(service.store.store_dir.iterdir()) == []


def test_a_private_key_is_stored_as_pkcs8_with_safe_metadata(
    service: CredentialService,
) -> None:
    encrypted = key_pem(KEY_PASSWORD_MARKER)

    info = service.create_secret(
        ManagedSecretKind.CLIENT_PRIVATE_KEY, encrypted, private_key_password=KEY_PASSWORD_MARKER
    )

    stored = service.store.resolve(info.id)
    assert stored.startswith("-----BEGIN PRIVATE KEY-----")
    assert "ENCRYPTED" not in stored
    assert info.metadata == {"key_type": "RSA", "key_size": 2048, "encrypted_input": True}
    assert KEY_PASSWORD_MARKER not in json.dumps(info.metadata)


def test_a_private_key_with_the_wrong_password_is_refused(service: CredentialService) -> None:
    encrypted = key_pem(KEY_PASSWORD_MARKER)

    with pytest.raises(CertificateValidationError):
        service.create_secret(
            ManagedSecretKind.CLIENT_PRIVATE_KEY, encrypted, private_key_password="wrong"
        )

    assert list(service.store.store_dir.iterdir()) == []


# --------------------------------------------------------------------------------------------
# inspection and deletion
# --------------------------------------------------------------------------------------------


def test_inspect_reports_the_public_view_only(service: CredentialService) -> None:
    info = service.create_secret(ManagedSecretKind.PASSWORD, PASSWORD_MARKER)

    inspected = service.inspect_secret(info.id)

    assert inspected == info
    assert PASSWORD_MARKER not in inspected.model_dump_json()


def test_a_missing_secret_has_stable_errors(service: CredentialService) -> None:
    missing = "sec_" + "0" * 32

    with pytest.raises(ManagedSecretNotFoundError) as inspected:
        service.inspect_secret(missing)
    with pytest.raises(ManagedSecretNotFoundError) as deleted:
        service.delete_secret(missing)

    assert inspected.value.code == "managed_secret_not_found"
    assert deleted.value.code == "managed_secret_not_found"


def test_an_unreferenced_secret_can_be_deleted(service: CredentialService) -> None:
    info = service.create_secret(ManagedSecretKind.TOKEN, TOKEN_MARKER)

    service.delete_secret(info.id)

    assert service.store.exists(info.id) is False


def test_a_referenced_secret_cannot_be_deleted(service: CredentialService) -> None:
    password = service.create_secret(ManagedSecretKind.PASSWORD, PASSWORD_MARKER)
    service.catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg",
            kind=DatasourceKind.RELATIONAL,
            connection_profile=profile_referencing(managed(password.id)),
        )
    )

    with pytest.raises(ManagedSecretInUseError) as raised:
        service.delete_secret(password.id)

    assert raised.value.code == "managed_secret_in_use"
    assert raised.value.status_code == 409
    assert service.store.exists(password.id) is True


def test_deleting_becomes_possible_once_the_reference_is_gone(
    service: CredentialService,
) -> None:
    """The guard is a reference count, not a permanent marker: it reflects the catalog as it is."""
    password = service.create_secret(ManagedSecretKind.PASSWORD, PASSWORD_MARKER)
    datasource = service.catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg",
            kind=DatasourceKind.RELATIONAL,
            connection_profile=profile_referencing(managed(password.id)),
        )
    )
    assert service.catalog.count_managed_secret_references(password.id) == 1

    with service.catalog._connect() as connection:
        connection.execute("DELETE FROM datasource WHERE id=?", (datasource.id,))

    service.delete_secret(password.id)

    assert service.store.exists(password.id) is False


def test_every_managed_reference_in_a_profile_is_counted(service: CredentialService) -> None:
    password = service.create_secret(ManagedSecretKind.PASSWORD, PASSWORD_MARKER)
    tls_material = service.create_secret(ManagedSecretKind.TOKEN, TOKEN_MARKER)
    service.catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg",
            kind=DatasourceKind.RELATIONAL,
            connection_profile=profile_referencing(
                managed(password.id),
                managed(tls_material.id),
                managed(tls_material.id),
                managed(tls_material.id),
            ),
        )
    )

    # One reference as an authentication secret, three as TLS material - all four count.
    assert service.catalog.count_managed_secret_references(password.id) == 1
    assert service.catalog.count_managed_secret_references(tls_material.id) == 3
    assert service.catalog.count_managed_secret_references("sec_" + "9" * 32) == 0


def test_a_reference_that_is_absent_from_the_catalog_does_not_block_a_delete(
    service: CredentialService,
) -> None:
    orphan = service.create_secret(ManagedSecretKind.PASSWORD, PASSWORD_MARKER)

    assert service.catalog.count_managed_secret_references(orphan.id) == 0
    service.delete_secret(orphan.id)


# --------------------------------------------------------------------------------------------
# the catalog only ever receives references
# --------------------------------------------------------------------------------------------


def test_the_catalog_stores_only_secret_references(service: CredentialService) -> None:
    password = service.create_secret(ManagedSecretKind.PASSWORD, PASSWORD_MARKER)
    token = service.create_secret(ManagedSecretKind.TOKEN, TOKEN_MARKER)

    datasource = service.catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg",
            kind=DatasourceKind.RELATIONAL,
            connection_profile=ConnectionProfile(
                driver="postgresql",
                endpoint=ConnectionEndpoint(hosts=[{"host": "127.0.0.1", "port": 5432}]),
                authentication=AuthenticationConfig(
                    method=AuthenticationMethod.PASSWORD,
                    username="readonly",
                    password=managed(password.id),
                    token=managed(token.id),
                ),
            ),
        )
    )

    stored = service.catalog.get_datasource(datasource.id)[1]
    serialized = json.dumps(stored)
    assert PASSWORD_MARKER not in serialized
    assert TOKEN_MARKER not in serialized
    assert password.id in serialized and token.id in serialized
    profile = service.catalog.get_connection_profile(datasource.id)
    assert profile is not None
    assert profile.authentication.password == managed(password.id)


def test_no_written_secret_leaks_into_the_catalog_logs_metadata_or_errors(
    service: CredentialService, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The one invariant that must hold no matter which layer is looking: the marker stays put."""
    password = service.create_secret(ManagedSecretKind.PASSWORD, PASSWORD_MARKER)
    certificate_secret = service.create_secret(
        ManagedSecretKind.CA_CERTIFICATE, certificate_pem(ca=True)
    )
    certificate_body = service.store.resolve(certificate_secret.id).strip()
    datasource = service.catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg",
            kind=DatasourceKind.RELATIONAL,
            connection_profile=profile_referencing(managed(password.id), managed(certificate_secret.id)),
        )
    )

    failures: list[Exception] = []
    with caplog.at_level(logging.DEBUG):
        try:
            service.delete_secret(password.id)
        except ManagedSecretInUseError as error:
            failures.append(error)
        try:
            service.delete_secret(certificate_secret.id)
        except ManagedSecretInUseError as error:
            failures.append(error)
        try:
            service.create_secret(ManagedSecretKind.CLIENT_PRIVATE_KEY, "not a key")
        except CertificateValidationError as error:
            failures.append(error)

    catalog_bytes = (tmp_path / "catalog.db").read_bytes()
    # The database really does contain this datasource, which is what makes the two absences below
    # meaningful rather than a check of an empty file.
    assert datasource.id.encode() in catalog_bytes
    assert PASSWORD_MARKER.encode() not in catalog_bytes
    assert b"-----BEGIN CERTIFICATE-----" not in catalog_bytes

    for record in caplog.records:
        assert PASSWORD_MARKER not in record.getMessage()
        assert "-----BEGIN" not in record.getMessage()

    for error in failures:
        assert PASSWORD_MARKER not in str(error)
        assert PASSWORD_MARKER not in repr(error)
        assert certificate_body not in str(error)
        assert "-----BEGIN" not in str(error)

    for info in (password, certificate_secret):
        assert PASSWORD_MARKER not in json.dumps(info.model_dump(mode="json"))
        assert certificate_body not in json.dumps(info.model_dump(mode="json"))

    # And the secret itself is still intact: a refused delete must not have removed anything.
    assert service.store.resolve(password.id) == PASSWORD_MARKER
    assert service.store.resolve(certificate_secret.id).strip() == certificate_body
    assert service.catalog.count_managed_secret_references(password.id) == 1


# --------------------------------------------------------------------------------------------
# text and certificate material are different types on purpose
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        ManagedSecretKind.PASSWORD,
        ManagedSecretKind.TOKEN,
        ManagedSecretKind.API_KEY,
        ManagedSecretKind.CLIENT_PRIVATE_KEY_PASSWORD,
    ],
)
def test_a_text_secret_given_bytes_is_refused_rather_than_decoded(
    service: CredentialService, kind: ManagedSecretKind
) -> None:
    """A password is text; encoding-guessing it is how a secret silently becomes another secret."""
    with pytest.raises(ValueError, match="is text"):
        service.create_secret(kind, PASSWORD_MARKER.encode())

    assert stored_secret_files(service) == []


@pytest.mark.parametrize(
    "kind",
    [
        ManagedSecretKind.CA_CERTIFICATE,
        ManagedSecretKind.CLIENT_CERTIFICATE,
        ManagedSecretKind.CLIENT_PRIVATE_KEY,
    ],
)
def test_certificate_material_may_be_der(service: CredentialService, kind: ManagedSecretKind) -> None:
    """DER is bytes, and the validator already accepts it; nothing forces a UTF-8 decode first."""
    if kind is ManagedSecretKind.CA_CERTIFICATE:
        value: str | bytes = certificate(ca=True)[0].public_bytes(serialization.Encoding.DER)
    elif kind is ManagedSecretKind.CLIENT_CERTIFICATE:
        value = certificate(ca=False)[0].public_bytes(serialization.Encoding.DER)
    else:
        value = client_pair(key_der=True)[1]

    info = service.create_secret(kind, value)

    # Stored canonical PEM regardless of the input encoding.
    assert service.store.resolve(info.id).startswith("-----BEGIN")


def test_a_secret_keeps_its_surrounding_whitespace(service: CredentialService) -> None:
    info = service.create_secret(ManagedSecretKind.PASSWORD, " secret ")

    assert service.store.resolve(info.id) == " secret "


# --------------------------------------------------------------------------------------------
# client identities: validated as a pair, stored all-or-nothing
# --------------------------------------------------------------------------------------------


def test_a_matching_client_pair_is_stored_as_two_secrets(service: CredentialService) -> None:
    certificate_pem_text, private_key_pem_text = client_pair()

    stored_certificate, stored_key = service.create_client_identity(
        certificate_pem_text, private_key_pem_text
    )

    assert stored_certificate.kind is ManagedSecretKind.CLIENT_CERTIFICATE
    assert stored_key.kind is ManagedSecretKind.CLIENT_PRIVATE_KEY
    assert stored_certificate.id != stored_key.id
    assert service.store.resolve(stored_certificate.id).startswith("-----BEGIN CERTIFICATE-----")
    assert service.store.resolve(stored_key.id).startswith("-----BEGIN PRIVATE KEY-----")
    assert stored_certificate.metadata["subject"] == "CN=client"


def test_der_client_material_is_accepted(service: CredentialService) -> None:
    certificate_bytes, private_key_bytes = client_pair(certificate_der=True, key_der=True)

    stored_certificate, stored_key = service.create_client_identity(
        certificate_bytes, private_key_bytes
    )

    assert service.store.resolve(stored_certificate.id).startswith("-----BEGIN CERTIFICATE-----")
    assert service.store.resolve(stored_key.id).startswith("-----BEGIN PRIVATE KEY-----")


def test_an_encrypted_key_is_decrypted_with_the_supplied_password(
    service: CredentialService,
) -> None:
    certificate_pem_text, private_key_pem_text = client_pair(password=KEY_PASSWORD_MARKER)

    stored_certificate, stored_key = service.create_client_identity(
        certificate_pem_text, private_key_pem_text, private_key_password=KEY_PASSWORD_MARKER
    )

    # The stored key is unencrypted PKCS#8: the decrypt password needed nothing kept.
    assert "ENCRYPTED" not in service.store.resolve(stored_key.id)
    assert stored_key.metadata["encrypted_input"] is True
    assert KEY_PASSWORD_MARKER not in json.dumps(stored_key.metadata)
    assert service.store.exists(stored_certificate.id) is True


def test_a_wrong_decrypt_password_stores_nothing(service: CredentialService) -> None:
    certificate_pem_text, private_key_pem_text = client_pair(password=KEY_PASSWORD_MARKER)

    with pytest.raises(CertificateValidationError):
        service.create_client_identity(
            certificate_pem_text, private_key_pem_text, private_key_password="wrong"
        )

    assert stored_secret_files(service) == []


def test_a_mismatched_pair_is_refused_before_anything_is_stored(
    service: CredentialService,
) -> None:
    """Certificate A with private key B is caught here, not discovered as a handshake failure."""
    certificate_pem_text, _ = client_pair()
    _, other_key_pem = client_pair()

    with pytest.raises(CertificateKeyMismatchError) as raised:
        service.create_client_identity(certificate_pem_text, other_key_pem)

    assert raised.value.code == "certificate_key_mismatch"
    assert stored_secret_files(service) == []


def test_a_failed_second_store_rolls_back_the_first(
    service: CredentialService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pair behaves all-or-nothing even though the store writes one file per secret."""
    certificate_pem_text, private_key_pem_text = client_pair()
    original_create = service.store.create
    calls = {"count": 0}

    def failing_second(kind, plaintext, *, metadata=None):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated store failure")
        return original_create(kind, plaintext, metadata=metadata)

    monkeypatch.setattr(service.store, "create", failing_second)

    with pytest.raises(OSError):
        service.create_client_identity(certificate_pem_text, private_key_pem_text)

    # The certificate written first is gone again: no orphan credential is left behind.
    assert calls["count"] == 2
    assert [path for path in stored_secret_files(service) if not path.name.endswith(".tmp")] == []


def test_a_failed_rollback_keeps_the_creation_error_and_logs_no_material(
    service: CredentialService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Even the double-failure path stays quiet: only an id and an error type are ever logged."""
    certificate_pem_text, private_key_pem_text = client_pair()
    certificate_body = certificate_pem_text.strip()
    original_create = service.store.create
    calls = {"count": 0}

    def failing_second(kind, plaintext, *, metadata=None):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated store failure")
        return original_create(kind, plaintext, metadata=metadata)

    def failing_delete(secret_id):
        raise OSError("simulated rollback failure")

    monkeypatch.setattr(service.store, "create", failing_second)
    monkeypatch.setattr(service.store, "delete", failing_delete)

    with caplog.at_level(logging.DEBUG), pytest.raises(OSError, match="simulated store failure"):
        service.create_client_identity(
            certificate_pem_text, private_key_pem_text, private_key_password=None
        )

    for record in caplog.records:
        message = record.getMessage()
        assert certificate_body not in message
        assert "-----BEGIN" not in message
        assert "PRIVATE KEY" not in message
    assert any("rollback failed" in record.getMessage() for record in caplog.records)
