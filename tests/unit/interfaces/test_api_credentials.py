"""HTTP credential and certificate boundary tests (RS-CRED-01B).

These tests cover the interface layer: the four product entry points, their request validation,
their bounded multipart reading, and the one thing that matters most in a report - a password, a
token, a private key or a certificate body must never come back out of the API, in any response, any
error message, any log line or inside the catalog.

The real Core runs behind these routes. Every certificate and key is generated in-process, so
nothing is committed as a fixture and nothing can expire into a red suite.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient

from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
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
)
from qaneris.contracts.datasource import DatasourceKind
from qaneris.interfaces.api import credentials as boundary
from qaneris.interfaces.api.app import create_app

PASSWORD_MARKER = "UNIQUE_PASSWORD_MARKER"
TOKEN_MARKER = "UNIQUE_TOKEN_MARKER"
API_KEY_MARKER = "UNIQUE_API_KEY_MARKER"
PRIVATE_KEY_PASSWORD_MARKER = "UNIQUE_PRIVATE_KEY_PASSWORD_MARKER"

CREDENTIALS = "/api/credentials"
CA_ENDPOINT = "/api/certificates/ca"
IDENTITY_ENDPOINT = "/api/certificates/client-identity"

LIMIT = boundary.CREDENTIAL_FILE_MAX_BYTES


# --------------------------------------------------------------------------------------------
# certificate material
# --------------------------------------------------------------------------------------------


def certificate(
    *,
    ca: bool,
    not_before: dt.datetime | None = None,
    not_after: dt.datetime | None = None,
    key: rsa.RSAPrivateKey | None = None,
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    issuer = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Qaneris Test CA" if ca else "client")]
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(issuer.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - dt.timedelta(days=1))
        .not_valid_after(not_after or now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if not ca:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
    return builder.sign(issuer, hashes.SHA256()), issuer


def certificate_bytes(
    *,
    ca: bool,
    encoding: serialization.Encoding = serialization.Encoding.PEM,
    expired: bool = False,
    key: rsa.RSAPrivateKey | None = None,
) -> bytes:
    now = dt.datetime.now(dt.UTC)
    built, _ = certificate(
        ca=ca,
        not_before=now - dt.timedelta(days=30) if expired else None,
        not_after=now - dt.timedelta(days=1) if expired else None,
        key=key,
    )
    return built.public_bytes(encoding)


def private_key_bytes(
    key: rsa.RSAPrivateKey | None = None,
    *,
    password: str | None = None,
    encoding: serialization.Encoding = serialization.Encoding.PEM,
) -> bytes:
    material = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encryption = (
        serialization.BestAvailableEncryption(password.encode())
        if password
        else serialization.NoEncryption()
    )
    return material.private_bytes(
        encoding, serialization.PrivateFormat.PKCS8, encryption
    )


def client_pair(*, password: str | None = None) -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        certificate_bytes(ca=False, key=key),
        private_key_bytes(key, password=password),
    )


def client_certificate_pem() -> str:
    return certificate_bytes(ca=False).decode("utf-8")


def ca_certificate_pem() -> str:
    return certificate_bytes(ca=True).decode("utf-8")


# --------------------------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------------------------


def build_client(tmp_path: Path) -> tuple[TestClient, QanerisService]:
    """An app composed like production, on its own catalog, with a test-only managed store."""
    store = ManagedCredentialStore.from_environment(
        {
            "QANERIS_SECRET_STORE_DIR": str(tmp_path / "secrets"),
            "QANERIS_MASTER_KEY": ManagedCredentialStore.generate_master_key(),
        }
    )
    catalog = Catalog(tmp_path / "catalog.db")
    service = QanerisService(catalog, credential_service=CredentialService(store, catalog))
    app = create_app(database_path=str(tmp_path / "catalog.db"))
    app.state.service = service
    return TestClient(app, raise_server_exceptions=False), service


def store_files(service: QanerisService) -> list[Path]:
    directory = service._credentials().store.store_dir
    return sorted(directory.iterdir()) if directory.exists() else []


def create_text_secret(client: TestClient, kind: str, value: str) -> httpx.Response:
    return client.post(CREDENTIALS, json={"kind": kind, "value": value})


# --------------------------------------------------------------------------------------------
# text secrets
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "marker"),
    [
        ("password", PASSWORD_MARKER),
        ("token", TOKEN_MARKER),
        ("api_key", API_KEY_MARKER),
        ("client_private_key_password", PRIVATE_KEY_PASSWORD_MARKER),
    ],
)
def test_a_text_secret_is_stored_and_returns_only_public_fields(
    tmp_path: Path, kind: str, marker: str
) -> None:
    client, service = build_client(tmp_path)

    response = create_text_secret(client, kind, marker)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"secret_id", "kind", "created_at", "metadata"}
    assert body["kind"] == kind
    assert body["secret_id"].startswith("sec_")
    assert marker not in response.text
    assert service._credentials().store.resolve(body["secret_id"]) == marker


def test_a_text_secret_keeps_surrounding_whitespace(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)

    response = create_text_secret(client, "password", " secret ")

    assert response.status_code == 201
    assert service._credentials().store.resolve(response.json()["secret_id"]) == " secret "


@pytest.mark.parametrize(
    "kind", ["ca_certificate", "client_certificate", "client_private_key"]
)
def test_certificate_kinds_are_refused_on_the_json_endpoint(tmp_path: Path, kind: str) -> None:
    client, service = build_client(tmp_path)

    response = create_text_secret(client, kind, "-----BEGIN CERTIFICATE-----\n")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert store_files(service) == []


def test_an_unknown_kind_is_a_validation_error(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)

    response = create_text_secret(client, "not_a_kind", "x")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_an_empty_text_secret_is_refused(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)

    response = create_text_secret(client, "password", "")

    assert response.status_code == 422
    assert store_files(service) == []


def test_an_oversized_text_secret_is_refused_by_validation(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)

    response = create_text_secret(client, "password", "x" * 65_537)

    assert response.status_code == 422
    assert store_files(service) == []


# --------------------------------------------------------------------------------------------
# inspect and delete
# --------------------------------------------------------------------------------------------


def test_inspect_returns_the_public_record(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    created = create_text_secret(client, "token", TOKEN_MARKER).json()

    response = client.get(f"{CREDENTIALS}/{created['secret_id']}")

    assert response.status_code == 200
    assert response.json() == created
    assert TOKEN_MARKER not in response.text


def test_inspect_of_an_unknown_secret_is_404(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)

    response = client.get(f"{CREDENTIALS}/sec_{'0' * 32}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "managed_secret_not_found"


def test_delete_returns_204_and_removes_the_secret(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)
    created = create_text_secret(client, "api_key", API_KEY_MARKER).json()

    response = client.delete(f"{CREDENTIALS}/{created['secret_id']}")

    assert response.status_code == 204
    assert response.content == b""
    assert service._credentials().store.exists(created["secret_id"]) is False


def test_inspect_and_delete_work_for_certificate_kinds_too(tmp_path: Path) -> None:
    """One identifier space: GET/DELETE treat every kind - text or certificate - the same way."""
    client, _ = build_client(tmp_path)
    created = upload_ca(client, certificate_bytes(ca=True)).json()

    inspected = client.get(f"{CREDENTIALS}/{created['secret_id']}")
    deleted = client.delete(f"{CREDENTIALS}/{created['secret_id']}")

    assert inspected.status_code == 200
    assert inspected.json()["kind"] == "ca_certificate"
    assert deleted.status_code == 204


def test_inspect_and_delete_work_for_a_client_identity(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    certificate_data, key_data = client_pair()
    identity = upload_identity(client, certificate_data, key_data).json()

    for part in ("client_certificate", "client_private_key"):
        secret_id = identity[part]["secret_id"]
        assert client.get(f"{CREDENTIALS}/{secret_id}").json()["kind"] == identity[part]["kind"]
        assert client.delete(f"{CREDENTIALS}/{secret_id}").status_code == 204


def test_delete_of_an_unknown_secret_is_404(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)

    response = client.delete(f"{CREDENTIALS}/sec_{'0' * 32}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "managed_secret_not_found"


def test_delete_of_a_referenced_secret_is_409(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)
    created = create_text_secret(client, "password", PASSWORD_MARKER).json()
    service.catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg",
            kind=DatasourceKind.RELATIONAL,
            connection_profile=ConnectionProfile(
                driver="postgresql",
                endpoint=ConnectionEndpoint(hosts=[{"host": "127.0.0.1", "port": 5432}]),
                authentication=AuthenticationConfig(
                    method=AuthenticationMethod.PASSWORD,
                    username="readonly",
                    password=SecretReference(
                        provider=SecretProviderKind.MANAGED,
                        identifier=created["secret_id"],
                    ),
                ),
            ),
        )
    )

    response = client.delete(f"{CREDENTIALS}/{created['secret_id']}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "managed_secret_in_use"
    assert service._credentials().store.exists(created["secret_id"]) is True


# --------------------------------------------------------------------------------------------
# CA certificate upload
# --------------------------------------------------------------------------------------------


def upload_ca(client: TestClient, data: bytes, filename: str = "ca.pem") -> httpx.Response:
    return client.post(
        CA_ENDPOINT, files={"file": (filename, data, "application/octet-stream")}
    )


def test_a_pem_ca_certificate_is_accepted(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)

    response = upload_ca(client, certificate_bytes(ca=True))

    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "ca_certificate"
    assert body["metadata"]["subject"] == "CN=Qaneris Test CA"
    stored = service._credentials().store.resolve(body["secret_id"])
    assert stored.startswith("-----BEGIN CERTIFICATE-----")


def test_a_der_ca_certificate_is_accepted(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)

    response = upload_ca(
        client, certificate_bytes(ca=True, encoding=serialization.Encoding.DER), filename="ca.der"
    )

    assert response.status_code == 201
    assert response.json()["kind"] == "ca_certificate"


def test_an_expired_ca_certificate_is_refused(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)

    response = upload_ca(client, certificate_bytes(ca=True, expired=True))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "certificate_validation_failed"
    assert store_files(service) == []


def test_a_client_certificate_uploaded_as_a_ca_is_refused(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)

    response = upload_ca(client, certificate_bytes(ca=False))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "certificate_validation_failed"


def test_invalid_bytes_are_refused(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)

    response = upload_ca(client, b"not a certificate at all")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "certificate_validation_failed"


def test_an_oversized_ca_upload_is_413(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)

    response = upload_ca(client, b"x" * (LIMIT + 1))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "credential_upload_too_large"
    assert store_files(service) == []


def test_an_empty_ca_upload_is_a_request_error(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)

    response = upload_ca(client, b"")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert store_files(service) == []


def test_a_missing_file_part_is_a_validation_error(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)

    response = client.post(CA_ENDPOINT, files={"other": ("x.pem", b"x", "text/plain")})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_extra_form_fields_provide_no_validation_bypass(tmp_path: Path) -> None:
    """A request cannot ask to skip validation, store raw bytes or accept an expired certificate."""
    client, service = build_client(tmp_path)

    response = client.post(
        CA_ENDPOINT,
        files={"file": ("ca.pem", b"not a certificate", "text/plain")},
        data={
            "skip_validation": "true",
            "store_raw": "true",
            "no_normalize": "true",
            "allow_expired": "true",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "certificate_validation_failed"
    assert store_files(service) == []


# --------------------------------------------------------------------------------------------
# client identity upload
# --------------------------------------------------------------------------------------------


def upload_identity(
    client: TestClient,
    certificate_data: bytes | None,
    key_data: bytes | None,
    *,
    password: str | None = None,
) -> httpx.Response:
    files = {}
    if certificate_data is not None:
        files["certificate"] = ("client.pem", certificate_data, "application/octet-stream")
    if key_data is not None:
        files["private_key"] = ("client.key", key_data, "application/octet-stream")
    data = {"private_key_password": password} if password is not None else {}
    return client.post(IDENTITY_ENDPOINT, files=files, data=data)


def test_a_matching_client_identity_is_stored_as_two_secrets(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)
    certificate_data, key_data = client_pair()

    response = upload_identity(client, certificate_data, key_data)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"client_certificate", "client_private_key"}
    assert body["client_certificate"]["kind"] == "client_certificate"
    assert body["client_private_key"]["kind"] == "client_private_key"
    assert body["client_certificate"]["secret_id"] != body["client_private_key"]["secret_id"]
    certificate_body = certificate_data.decode().strip()
    assert certificate_body not in response.text
    assert "-----BEGIN PRIVATE KEY-----" not in response.text
    assert service._credentials().store.resolve(
        body["client_certificate"]["secret_id"]
    ).startswith("-----BEGIN CERTIFICATE-----")


def test_der_client_material_is_accepted(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate_data = certificate_bytes(ca=False, encoding=serialization.Encoding.DER, key=key)
    key_data = private_key_bytes(key, encoding=serialization.Encoding.DER)

    response = upload_identity(client, certificate_data, key_data)

    assert response.status_code == 201


def test_an_encrypted_key_is_accepted_with_the_right_password(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)
    certificate_data, key_data = client_pair(password=PRIVATE_KEY_PASSWORD_MARKER)

    response = upload_identity(client, certificate_data, key_data, password=PRIVATE_KEY_PASSWORD_MARKER)

    assert response.status_code == 201
    body = response.json()
    # The decrypt password is transient: it is not stored, not returned and not a secret of its own.
    assert PRIVATE_KEY_PASSWORD_MARKER not in response.text
    assert "ENCRYPTED" not in service._credentials().store.resolve(
        body["client_private_key"]["secret_id"]
    )
    assert len(store_files(service)) == 2


def test_a_wrong_decrypt_password_stores_nothing(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)
    certificate_data, key_data = client_pair(password=PRIVATE_KEY_PASSWORD_MARKER)

    response = upload_identity(client, certificate_data, key_data, password="wrong")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "certificate_validation_failed"
    assert store_files(service) == []


def test_a_mismatched_pair_is_refused_and_stores_nothing(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)
    certificate_data, _ = client_pair()
    _, other_key = client_pair()

    response = upload_identity(client, certificate_data, other_key)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "certificate_key_mismatch"
    assert store_files(service) == []


def test_a_missing_certificate_part_is_a_validation_error(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    _, key_data = client_pair()

    response = upload_identity(client, None, key_data)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_a_missing_key_part_is_a_validation_error(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    certificate_data, _ = client_pair()

    response = upload_identity(client, certificate_data, None)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_an_oversized_certificate_part_is_413(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)
    _, key_data = client_pair()

    response = upload_identity(client, b"x" * (LIMIT + 1), key_data)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "credential_upload_too_large"
    assert store_files(service) == []


def test_an_oversized_key_part_is_413(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)
    certificate_data, _ = client_pair()

    response = upload_identity(client, certificate_data, b"x" * (LIMIT + 1))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "credential_upload_too_large"
    assert store_files(service) == []


def test_an_empty_certificate_part_is_a_request_error(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)
    _, key_data = client_pair()

    response = upload_identity(client, b"", key_data)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert store_files(service) == []


def test_an_empty_password_field_means_no_password(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    certificate_data, key_data = client_pair()

    response = upload_identity(client, certificate_data, key_data, password="")

    assert response.status_code == 201


# --------------------------------------------------------------------------------------------
# the response carries nothing internal
# --------------------------------------------------------------------------------------------


def test_no_response_ever_names_a_filename_or_a_server_path(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    certificate_data, key_data = client_pair()

    responses = [
        create_text_secret(client, "password", PASSWORD_MARKER),
        upload_ca(client, certificate_bytes(ca=True), filename="server-side-ca.pem"),
        upload_identity(client, certificate_data, key_data),
    ]

    body = "".join(response.text for response in responses)
    assert "ca.pem" not in body
    assert "client.key" not in body
    assert str(tmp_path) not in body
    assert "secret" not in body.replace("secret_id", "")


# --------------------------------------------------------------------------------------------
# nothing leaks, nothing is created besides the secret
# --------------------------------------------------------------------------------------------


def test_a_credential_upload_does_not_touch_the_catalog(tmp_path: Path, caplog) -> None:
    """Uploading a credential is not creating a datasource: the catalog stays empty."""
    client, service = build_client(tmp_path)

    with caplog.at_level(logging.DEBUG):
        create_text_secret(client, "password", PASSWORD_MARKER)
        upload_ca(client, certificate_bytes(ca=True))
        certificate_data, key_data = client_pair()
        upload_identity(client, certificate_data, key_data)

    assert service.catalog.list_datasources() == []
    assert service.catalog.list_datasets() == []
    catalog_bytes = (tmp_path / "catalog.db").read_bytes()
    assert PASSWORD_MARKER.encode() not in catalog_bytes
    assert b"-----BEGIN CERTIFICATE-----" not in catalog_bytes


def test_no_secret_material_reaches_a_response_a_log_or_an_error(tmp_path: Path, caplog) -> None:
    """The invariant: a marker written as a secret appears in the store and nowhere else."""
    client, service = build_client(tmp_path)

    with caplog.at_level(logging.DEBUG):
        password = create_text_secret(client, "password", PASSWORD_MARKER)
        rejected = create_text_secret(client, "token", "")
        bad_ca = upload_ca(client, b"not a certificate")
        certificate_data, key_data = client_pair(password=PRIVATE_KEY_PASSWORD_MARKER)
        stored = upload_identity(
            client, certificate_data, key_data, password="wrong-password"
        )

    assert password.status_code == 201
    assert rejected.status_code == 422
    assert bad_ca.status_code == 400
    assert stored.status_code == 400

    for response in (password, rejected, bad_ca, stored):
        assert PASSWORD_MARKER not in response.text
        assert PRIVATE_KEY_PASSWORD_MARKER not in response.text
        assert "-----BEGIN" not in response.text

    for record in caplog.records:
        message = record.getMessage()
        assert PASSWORD_MARKER not in message
        assert PRIVATE_KEY_PASSWORD_MARKER not in message
        # No PEM armouring of any kind: not a certificate body, not a private key body.
        assert "-----BEGIN" not in message
        assert "PRIVATE KEY" not in message

    # The one secret that was accepted carries its marker in its own record and nowhere else.
    inspected = client.get(f"{CREDENTIALS}/{password.json()['secret_id']}")
    assert PASSWORD_MARKER not in inspected.text
    assert inspected.json()["metadata"] == {}
    assert service._credentials().store.resolve(password.json()["secret_id"]) == PASSWORD_MARKER


def test_the_catalog_stores_only_a_reference_never_the_value(tmp_path: Path) -> None:
    client, service = build_client(tmp_path)
    created = create_text_secret(client, "password", PASSWORD_MARKER).json()
    service.catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg",
            kind=DatasourceKind.RELATIONAL,
            connection_profile=ConnectionProfile(
                driver="postgresql",
                endpoint=ConnectionEndpoint(hosts=[{"host": "127.0.0.1", "port": 5432}]),
                authentication=AuthenticationConfig(
                    method=AuthenticationMethod.PASSWORD,
                    username="readonly",
                    password=SecretReference(
                        provider=SecretProviderKind.MANAGED,
                        identifier=created["secret_id"],
                    ),
                ),
            ),
        )
    )

    _, document = service.catalog.get_datasource(
        service.catalog.list_datasources()[0].id
    )
    serialized = json.dumps(document)
    assert PASSWORD_MARKER not in serialized
    assert created["secret_id"] in serialized


# --------------------------------------------------------------------------------------------
# boundary hygiene
# --------------------------------------------------------------------------------------------


def test_the_boundary_reaches_nothing_but_the_application_service() -> None:
    """The module may not touch the store, the validator, the catalog or the resolver directly."""
    import inspect

    source = inspect.getsource(boundary)
    for name in (
        "ManagedCredentialStore",
        "CertificateValidator",
        "SecretResolver",
        "Catalog(",
        "QanerisService(",
        "count_managed_secret_references",
    ):
        assert name not in source, name
    assert "create_managed_secret" in source
    assert "create_managed_client_identity" in source


def test_declared_oversize_is_refused_before_the_form_is_parsed(tmp_path: Path) -> None:
    """A body that declares itself too large never reaches the multipart parser or a temp file."""
    client, service = build_client(tmp_path)

    response = client.post(
        CA_ENDPOINT,
        content=b"x" * 16,
        headers={
            "Content-Type": "multipart/form-data; boundary=xyz",
            "Content-Length": str(LIMIT * 4),
        },
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "credential_upload_too_large"
    assert store_files(service) == []


def test_the_credential_store_configuration_error_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server without a configured store fails closed rather than inventing a key."""
    monkeypatch.delenv("QANERIS_SECRET_STORE_DIR", raising=False)
    monkeypatch.delenv("QANERIS_MASTER_KEY", raising=False)
    app = create_app(database_path=str(tmp_path / "catalog.db"))
    client = TestClient(app, raise_server_exceptions=False)

    response = create_text_secret(client, "password", PASSWORD_MARKER)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "credential_store_configuration_error"
    assert PASSWORD_MARKER not in response.text


def test_the_validation_error_handler_does_not_echo_the_body(tmp_path: Path) -> None:
    """A rejected secret must not come back in the error: the handler uses ``msg``, not ``input``."""
    client, _ = build_client(tmp_path)

    response = create_text_secret(client, "password", "")

    assert response.status_code == 422
    assert "input" not in response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_there_is_no_credential_list_endpoint(tmp_path: Path) -> None:
    """Enumeration is deliberately absent: the store has no product-level listing contract."""
    client, _ = build_client(tmp_path)
    create_text_secret(client, "password", PASSWORD_MARKER)

    response = client.get(CREDENTIALS)

    # No GET route exists on the collection, so nothing is enumerable even by accident.
    assert response.status_code == 405
    assert PASSWORD_MARKER not in response.text
