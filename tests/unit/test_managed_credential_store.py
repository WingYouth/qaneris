"""Unit tests for the managed credential store (RS-CRED-01A).

The store is the only component that writes secret material to disk, so these tests are about
exactly that: what ends up in the file, what an attacker gets from a wrong key, and what happens
when a write is interrupted. Everything else - whether a secret may be deleted, whether a
certificate is usable - belongs to the service and validator suites.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from smartdata.common.artifacts import project_root
from smartdata.common.errors import (
    CredentialStoreConfigurationError,
    ManagedSecretIntegrityError,
    ManagedSecretNotFoundError,
)
from smartdata.connections import managed_store
from smartdata.connections.managed_store import ManagedCredentialStore
from smartdata.contracts.credentials import ManagedSecretInfo, ManagedSecretKind

PASSWORD = "UNIQUE_PASSWORD_MARKER"
TOKEN = "UNIQUE_TOKEN_MARKER"
MASTER_KEY_ENV = "SMARTDATA_MASTER_KEY"
STORE_DIR_ENV = "SMARTDATA_SECRET_STORE_DIR"


def environment(tmp_path: Path, **changes: str) -> dict[str, str]:
    """A complete, valid configuration whose store directory lives outside the repository."""
    values = {
        STORE_DIR_ENV: str(tmp_path / "secrets"),
        MASTER_KEY_ENV: ManagedCredentialStore.generate_master_key(),
    }
    values.update(changes)
    return values


def store_for(tmp_path: Path) -> ManagedCredentialStore:
    return ManagedCredentialStore.from_environment(environment(tmp_path))


def document(store: ManagedCredentialStore, secret_id: str) -> dict:
    return json.loads((store.store_dir / f"{secret_id}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------
# creation
# --------------------------------------------------------------------------------------------


def test_create_returns_a_managed_identifier_without_the_value(tmp_path: Path) -> None:
    store = store_for(tmp_path)

    info = store.create(ManagedSecretKind.PASSWORD, PASSWORD, metadata={"usage": "orders"})

    assert isinstance(info, ManagedSecretInfo)
    assert info.id.startswith("sec_")
    assert len(info.id) == len("sec_") + 32
    assert int(info.id[4:], 16) >= 0  # the identifier is the hex form of a UUID4
    assert info.kind is ManagedSecretKind.PASSWORD
    assert info.metadata == {"usage": "orders"}
    assert PASSWORD not in info.model_dump_json()


def test_resolve_returns_the_exact_plaintext(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    plaintext = "  p@ss with spaces and 中文  "

    info = store.create(ManagedSecretKind.TOKEN, plaintext)

    assert store.resolve(info.id) == plaintext


def test_the_stored_document_never_contains_the_plaintext(tmp_path: Path) -> None:
    store = store_for(tmp_path)

    info = store.create(ManagedSecretKind.API_KEY, "sk-UNIQUE_API_KEY_MARKER")

    raw = (store.store_dir / f"{info.id}.json").read_bytes()
    assert b"UNIQUE_API_KEY_MARKER" not in raw
    stored = document(store, info.id)
    assert stored["algorithm"] == "AES-256-GCM"
    assert stored["version"] == 1
    assert stored["kind"] == "api_key"
    assert set(stored) == {
        "version",
        "id",
        "kind",
        "algorithm",
        "nonce",
        "ciphertext",
        "created_at",
        "metadata",
    }


def test_inspect_reports_metadata_without_decrypting(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    info = store.create(
        ManagedSecretKind.CLIENT_CERTIFICATE, "certificate-body", metadata={"subject": "CN=client"}
    )

    inspected = store.inspect(info.id)

    assert inspected.id == info.id
    assert inspected.kind is ManagedSecretKind.CLIENT_CERTIFICATE
    assert inspected.metadata == {"subject": "CN=client"}
    assert inspected.created_at == info.created_at
    assert "certificate-body" not in inspected.model_dump_json()


def test_exists_reports_storage_state(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    info = store.create(ManagedSecretKind.PASSWORD, PASSWORD)

    assert store.exists(info.id) is True
    assert store.exists("sec_" + "0" * 32) is False
    assert store.exists("../../etc/passwd") is False


# --------------------------------------------------------------------------------------------
# encryption properties
# --------------------------------------------------------------------------------------------


def test_every_document_gets_its_own_nonce(tmp_path: Path) -> None:
    store = store_for(tmp_path)

    first = store.create(ManagedSecretKind.PASSWORD, PASSWORD)
    second = store.create(ManagedSecretKind.PASSWORD, PASSWORD)

    first_document = document(store, first.id)
    second_document = document(store, second.id)
    assert first_document["nonce"] != second_document["nonce"]
    # The same plaintext must not produce the same ciphertext either.
    assert first_document["ciphertext"] != second_document["ciphertext"]


def test_a_document_is_bound_to_its_own_identifier_and_kind(tmp_path: Path) -> None:
    """The AAD makes a ciphertext unusable anywhere but where it was written."""
    store = store_for(tmp_path)
    info = store.create(ManagedSecretKind.PASSWORD, PASSWORD)
    stored = document(store, info.id)
    # Move the document to another, equally valid identifier: it must no longer authenticate.
    other_id = "sec_" + "a" * 32
    stored["id"] = other_id
    (store.store_dir / f"{other_id}.json").write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(ManagedSecretIntegrityError):
        store.resolve(other_id)


def test_wrong_master_key_fails_closed(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    info = store.create(ManagedSecretKind.PASSWORD, PASSWORD)
    other_key = os.urandom(32)

    with pytest.raises(ManagedSecretIntegrityError):
        ManagedCredentialStore(store.store_dir, other_key).resolve(info.id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ciphertext", base64.urlsafe_b64encode(os.urandom(48)).decode()),
        ("ciphertext", "not-base64!!"),
        ("nonce", base64.urlsafe_b64encode(os.urandom(8)).decode()),
        ("nonce", "short"),
    ],
)
def test_corrupt_material_fails_closed(tmp_path: Path, field: str, value: str) -> None:
    store = store_for(tmp_path)
    info = store.create(ManagedSecretKind.PASSWORD, PASSWORD)
    stored = document(store, info.id)
    stored[field] = value
    (store.store_dir / f"{info.id}.json").write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(ManagedSecretIntegrityError):
        store.resolve(info.id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 99),
        ("algorithm", "AES-128-CBC"),
        ("id", "sec_" + "b" * 32),
        ("kind", "not-a-kind"),
    ],
)
def test_a_document_that_does_not_match_the_format_is_refused(
    tmp_path: Path, field: str, value: object
) -> None:
    store = store_for(tmp_path)
    info = store.create(ManagedSecretKind.PASSWORD, PASSWORD)
    stored = document(store, info.id)
    stored[field] = value
    (store.store_dir / f"{info.id}.json").write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(ManagedSecretIntegrityError):
        store.resolve(info.id)


def test_a_document_with_an_unusable_creation_time_cannot_be_inspected(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    info = store.create(ManagedSecretKind.PASSWORD, PASSWORD)
    stored = document(store, info.id)
    stored["created_at"] = "not-a-timestamp"
    (store.store_dir / f"{info.id}.json").write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(ManagedSecretIntegrityError):
        store.inspect(info.id)


def test_tampered_metadata_cannot_smuggle_material_through_inspect(tmp_path: Path) -> None:
    """The metadata contract is re-checked on read, so a hand-edited file is not trusted."""
    store = store_for(tmp_path)
    info = store.create(ManagedSecretKind.PASSWORD, PASSWORD)
    stored = document(store, info.id)
    stored["metadata"] = {"password": PASSWORD}
    (store.store_dir / f"{info.id}.json").write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(ValidationError):
        store.inspect(info.id)


def test_a_truncated_document_is_not_partially_parsed(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    info = store.create(ManagedSecretKind.PASSWORD, PASSWORD)
    (store.store_dir / f"{info.id}.json").write_text('{"version": 1, "id":', encoding="utf-8")

    with pytest.raises(ManagedSecretIntegrityError):
        store.resolve(info.id)


# --------------------------------------------------------------------------------------------
# configuration is mandatory
# --------------------------------------------------------------------------------------------


def test_missing_configuration_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CredentialStoreConfigurationError):
        ManagedCredentialStore.from_environment({})
    with pytest.raises(CredentialStoreConfigurationError):
        ManagedCredentialStore.from_environment({STORE_DIR_ENV: str(tmp_path / "secrets")})
    with pytest.raises(CredentialStoreConfigurationError):
        ManagedCredentialStore.from_environment(
            {MASTER_KEY_ENV: ManagedCredentialStore.generate_master_key()}
        )


def test_the_store_directory_must_be_outside_the_repository(tmp_path: Path) -> None:
    inside = environment(tmp_path, **{STORE_DIR_ENV: str(project_root() / "secrets")})

    with pytest.raises(CredentialStoreConfigurationError):
        ManagedCredentialStore.from_environment(inside)


@pytest.mark.parametrize(
    "master_key",
    [
        "not-base64!!",
        base64.urlsafe_b64encode(os.urandom(16)).decode(),  # too short
        base64.urlsafe_b64encode(os.urandom(64)).decode(),  # too long
        base64.urlsafe_b64encode(b"").decode(),  # empty
    ],
)
def test_an_unusable_master_key_is_refused(tmp_path: Path, master_key: str) -> None:
    with pytest.raises(CredentialStoreConfigurationError):
        ManagedCredentialStore.from_environment(environment(tmp_path, **{MASTER_KEY_ENV: master_key}))


def test_a_configuration_error_never_echoes_the_master_key(tmp_path: Path) -> None:
    secret_key = base64.urlsafe_b64encode(os.urandom(16)).decode()

    with pytest.raises(CredentialStoreConfigurationError) as raised:
        ManagedCredentialStore.from_environment(
            environment(tmp_path, **{MASTER_KEY_ENV: secret_key})
        )

    assert secret_key not in str(raised.value)
    assert MASTER_KEY_ENV in str(raised.value)


def test_the_documented_generator_returns_a_usable_key() -> None:
    generated = ManagedCredentialStore.generate_master_key()

    assert len(base64.urlsafe_b64decode(generated)) == 32


# --------------------------------------------------------------------------------------------
# identifiers and paths
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret_id",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "sec_../../escape",
        "sec_short",
        "sec_" + "g" * 32,
        "",
        ".",
        "..",
        "sec_7f913c964fd04ce4a0cfbc31ed88f855.json",
    ],
)
def test_an_identifier_that_is_not_a_managed_secret_is_rejected(
    tmp_path: Path, secret_id: str
) -> None:
    store = store_for(tmp_path)

    with pytest.raises(ManagedSecretNotFoundError):
        store.resolve(secret_id)
    with pytest.raises(ManagedSecretNotFoundError):
        store.delete(secret_id)


def test_a_traversal_identifier_never_touches_anything_outside_the_store(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(ManagedSecretNotFoundError):
        store.delete("../outside.json")

    assert outside.exists()


def test_a_missing_secret_reports_the_stable_code(tmp_path: Path) -> None:
    store = store_for(tmp_path)

    with pytest.raises(ManagedSecretNotFoundError) as raised:
        store.resolve("sec_" + "0" * 32)

    assert raised.value.code == "managed_secret_not_found"
    assert raised.value.status_code == 404


def test_a_rejected_identifier_is_not_echoed_back(tmp_path: Path) -> None:
    """The error must not become an oracle for what the store directory contains."""
    store = store_for(tmp_path)

    with pytest.raises(ManagedSecretNotFoundError) as raised:
        store.resolve("../../etc/passwd")

    assert "passwd" not in str(raised.value)


# --------------------------------------------------------------------------------------------
# deletion
# --------------------------------------------------------------------------------------------


def test_delete_removes_exactly_one_file(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    keeper = store.create(ManagedSecretKind.PASSWORD, PASSWORD)
    doomed = store.create(ManagedSecretKind.TOKEN, TOKEN)

    store.delete(doomed.id)

    assert store.exists(doomed.id) is False
    assert store.exists(keeper.id) is True
    assert store.resolve(keeper.id) == PASSWORD
    assert sorted(path.name for path in store.store_dir.iterdir()) == [f"{keeper.id}.json"]


def test_delete_of_a_missing_secret_reports_not_found(tmp_path: Path) -> None:
    store = store_for(tmp_path)

    with pytest.raises(ManagedSecretNotFoundError):
        store.delete("sec_" + "0" * 32)


# --------------------------------------------------------------------------------------------
# on-disk hygiene
# --------------------------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_permissions_are_restrictive(tmp_path: Path) -> None:
    store = store_for(tmp_path)

    info = store.create(ManagedSecretKind.PASSWORD, PASSWORD)

    assert stat.S_IMODE(store.store_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.store_dir / f"{info.id}.json").stat().st_mode) == 0o600


def test_the_target_only_appears_after_a_complete_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value goes to a temporary file first and is moved into place in one step."""
    store = store_for(tmp_path)
    real_replace = os.replace
    target_present_before_move: list[bool] = []

    def recording_replace(source: str, destination: str) -> None:
        target_present_before_move.append(Path(destination).exists())
        real_replace(source, destination)

    monkeypatch.setattr(managed_store.os, "replace", recording_replace)

    info = store.create(ManagedSecretKind.PASSWORD, PASSWORD)

    assert target_present_before_move == [False]
    assert store.resolve(info.id) == PASSWORD


def test_a_failed_write_leaves_no_partial_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_for(tmp_path)
    monkeypatch.setattr(os, "fsync", Mock(side_effect=OSError("device full")))

    with pytest.raises(OSError):
        store.create(ManagedSecretKind.PASSWORD, PASSWORD)

    assert list(store.store_dir.iterdir()) == []


def test_the_store_creates_its_directory_on_first_use(tmp_path: Path) -> None:
    configured = tmp_path / "not-yet"
    store = ManagedCredentialStore.from_environment(
        environment(tmp_path, **{STORE_DIR_ENV: str(configured)}), create=False
    )
    assert not configured.exists()

    store.create(ManagedSecretKind.PASSWORD, PASSWORD)

    assert configured.is_dir()


def test_metadata_cannot_carry_secret_material(tmp_path: Path) -> None:
    """The metadata contract is a boundary of its own, not a convention callers may ignore."""
    store = store_for(tmp_path)

    with pytest.raises(ValidationError, match="password"):
        store.create(ManagedSecretKind.PASSWORD, PASSWORD, metadata={"password": PASSWORD})

    assert list(store.store_dir.iterdir()) == []


@pytest.mark.parametrize(
    "metadata",
    [
        {"value": PASSWORD},
        {"token": TOKEN},
        {"nested": {"client_private_key": "anything"}},
        {"note": "-----BEGIN PRIVATE KEY-----"},
    ],
)
def test_metadata_that_could_carry_material_is_refused(
    tmp_path: Path, metadata: dict[str, object]
) -> None:
    store = store_for(tmp_path)

    with pytest.raises(ValidationError):
        store.create(ManagedSecretKind.PASSWORD, PASSWORD, metadata=metadata)
