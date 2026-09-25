"""The Application Service credential contract (RS-CRED-01B).

Product interfaces depend on these four methods and nothing else: they delegate to the
``CredentialService`` exactly once per call and expose no other credential path. These tests pin that
shape with a recording double, so a future refactor cannot quietly grow a second entry point - or
start reaching into the catalog or the store from the Application layer.
"""

from __future__ import annotations

from typing import Any

from qaneris.application.service import QanerisService
from qaneris.contracts.credentials import ManagedSecretInfo, ManagedSecretKind


class RecordingCredentials:
    """A credential-service double that records the calls the Application layer makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def create_secret(self, kind: Any, plaintext: Any, **kwargs: Any) -> ManagedSecretInfo:
        self.calls.append(("create_secret", (kind, plaintext), kwargs))
        return ManagedSecretInfo(
            id="sec_" + "1" * 32,
            kind=kind,
            created_at="2026-01-01T00:00:00+00:00",  # type: ignore[arg-type]
            metadata={},
        )

    def create_client_identity(
        self, certificate: Any, private_key: Any, **kwargs: Any
    ) -> tuple[ManagedSecretInfo, ManagedSecretInfo]:
        self.calls.append(("create_client_identity", (certificate, private_key), kwargs))
        return (
            ManagedSecretInfo(
                id="sec_" + "2" * 32,
                kind=ManagedSecretKind.CLIENT_CERTIFICATE,
                created_at="2026-01-01T00:00:00+00:00",  # type: ignore[arg-type]
                metadata={},
            ),
            ManagedSecretInfo(
                id="sec_" + "3" * 32,
                kind=ManagedSecretKind.CLIENT_PRIVATE_KEY,
                created_at="2026-01-01T00:00:00+00:00",  # type: ignore[arg-type]
                metadata={},
            ),
        )

    def inspect_secret(self, secret_id: str) -> ManagedSecretInfo:
        self.calls.append(("inspect_secret", (secret_id,), {}))
        return ManagedSecretInfo(
            id=secret_id,
            kind=ManagedSecretKind.PASSWORD,
            created_at="2026-01-01T00:00:00+00:00",  # type: ignore[arg-type]
            metadata={},
        )

    def delete_secret(self, secret_id: str) -> None:
        self.calls.append(("delete_secret", (secret_id,), {}))


def service_with(credentials: RecordingCredentials, tmp_path) -> QanerisService:
    from qaneris.catalog import Catalog

    return QanerisService(Catalog(tmp_path / "catalog.db"), credential_service=credentials)


def test_create_managed_secret_delegates_exactly_once(tmp_path) -> None:
    credentials = RecordingCredentials()
    service = service_with(credentials, tmp_path)

    info = service.create_managed_secret(
        ManagedSecretKind.CLIENT_PRIVATE_KEY, "key", private_key_password="pw"
    )

    assert info.id == "sec_" + "1" * 32
    assert credentials.calls == [
        (
            "create_secret",
            (ManagedSecretKind.CLIENT_PRIVATE_KEY, "key"),
            {"private_key_password": "pw"},
        )
    ]


def test_create_managed_client_identity_delegates_exactly_once(tmp_path) -> None:
    credentials = RecordingCredentials()
    service = service_with(credentials, tmp_path)

    certificate, key = service.create_managed_client_identity("cert", "key", private_key_password="pw")

    assert certificate.kind is ManagedSecretKind.CLIENT_CERTIFICATE
    assert key.kind is ManagedSecretKind.CLIENT_PRIVATE_KEY
    assert credentials.calls == [
        ("create_client_identity", ("cert", "key"), {"private_key_password": "pw"})
    ]


def test_inspect_managed_secret_delegates_exactly_once(tmp_path) -> None:
    credentials = RecordingCredentials()
    service = service_with(credentials, tmp_path)

    info = service.inspect_managed_secret("sec_x")

    assert info.id == "sec_x"
    assert credentials.calls == [("inspect_secret", ("sec_x",), {})]


def test_delete_managed_secret_delegates_exactly_once(tmp_path) -> None:
    credentials = RecordingCredentials()
    service = service_with(credentials, tmp_path)

    service.delete_managed_secret("sec_x")

    assert credentials.calls == [("delete_secret", ("sec_x",), {})]
