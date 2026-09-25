"""Unit tests for managed secret resolution (RS-CRED-01A).

Adding a provider to ``SecretResolver`` is the moment an existing deployment can break: if the
managed store were built eagerly, every environment/file-only installation would start failing the
day this feature landed. These tests pin the opposite behaviour - the new provider is inert until a
managed reference is actually resolved, and then it either works or fails closed with a
configuration error.

The regression half matters just as much: the environment and file providers must behave exactly as
they did before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qaneris.common.errors import (
    CredentialStoreConfigurationError,
    ManagedSecretNotFoundError,
)
from qaneris.connections.managed_store import ManagedCredentialStore
from qaneris.connections.secrets import (
    EnvironmentSecretProvider,
    FileSecretProvider,
    ManagedSecretProvider,
    SecretResolver,
)
from qaneris.contracts.connection import (
    AuthenticationConfig,
    AuthenticationMethod,
    ConnectionEndpoint,
    ConnectionProfile,
    SecretProviderKind,
    SecretReference,
    TLSConfig,
)
from qaneris.contracts.credentials import ManagedSecretKind

PASSWORD_MARKER = "UNIQUE_PASSWORD_MARKER"
TOKEN_MARKER = "UNIQUE_TOKEN_MARKER"
CA_MARKER = "UNIQUE_CA_MARKER"

MASTER_KEY_ENV = "QANERIS_MASTER_KEY"
STORE_DIR_ENV = "QANERIS_SECRET_STORE_DIR"


def environment(tmp_path: Path) -> dict[str, str]:
    return {
        STORE_DIR_ENV: str(tmp_path / "secrets"),
        MASTER_KEY_ENV: ManagedCredentialStore.generate_master_key(),
    }


def managed(secret_id: str) -> SecretReference:
    return SecretReference(provider=SecretProviderKind.MANAGED, identifier=secret_id)


def profile(
    *,
    password: SecretReference | None = None,
    ca_certificate: SecretReference | None = None,
) -> ConnectionProfile:
    return ConnectionProfile(
        driver="postgresql",
        endpoint=ConnectionEndpoint(hosts=[{"host": "127.0.0.1", "port": 5432}]),
        authentication=AuthenticationConfig(
            # The contract requires a reference exactly when the method needs one.
            method=(
                AuthenticationMethod.PASSWORD
                if password is not None
                else AuthenticationMethod.NONE
            ),
            username="readonly",
            password=password,
        ),
        tls=TLSConfig(enabled=bool(ca_certificate), ca_certificate=ca_certificate),
    )


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ManagedCredentialStore:
    """A configured managed store, with its environment visible to the resolver."""
    values = environment(tmp_path)
    monkeypatch.setenv(STORE_DIR_ENV, values[STORE_DIR_ENV])
    monkeypatch.setenv(MASTER_KEY_ENV, values[MASTER_KEY_ENV])
    return ManagedCredentialStore.from_environment()


# --------------------------------------------------------------------------------------------
# the providers that already existed
# --------------------------------------------------------------------------------------------


def test_the_environment_provider_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QANERIS_TEST_ENV_SECRET", PASSWORD_MARKER)

    resolved = SecretResolver().resolve(
        SecretReference(provider=SecretProviderKind.ENVIRONMENT, identifier="QANERIS_TEST_ENV_SECRET")
    )

    assert resolved == PASSWORD_MARKER
    assert EnvironmentSecretProvider({"A": "b"}).resolve("A") == "b"


def test_a_missing_environment_secret_still_reports_its_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QANERIS_TEST_ABSENT", raising=False)

    with pytest.raises(ValueError, match="environment secret is not configured"):
        SecretResolver().resolve(
            SecretReference(provider=SecretProviderKind.ENVIRONMENT, identifier="QANERIS_TEST_ABSENT")
        )


def test_the_file_provider_is_unchanged(tmp_path: Path) -> None:
    secret_file = tmp_path / "password.txt"
    secret_file.write_text(TOKEN_MARKER, encoding="utf-8")

    resolved = SecretResolver().resolve(
        SecretReference(provider=SecretProviderKind.FILE, identifier=str(secret_file))
    )

    assert resolved == TOKEN_MARKER
    assert FileSecretProvider().resolve(str(secret_file)) == TOKEN_MARKER


def test_a_missing_file_still_reports_its_own_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="secret file does not exist"):
        SecretResolver().resolve(
            SecretReference(provider=SecretProviderKind.FILE, identifier=str(tmp_path / "absent.txt"))
        )


def test_an_injected_provider_map_is_still_honoured() -> None:
    class Fake:
        def resolve(self, identifier: str) -> str:
            return f"fake:{identifier}"

    resolver = SecretResolver({SecretProviderKind.ENVIRONMENT: Fake()})

    assert resolver.resolve(
        SecretReference(provider=SecretProviderKind.ENVIRONMENT, identifier="x")
    ) == "fake:x"
    with pytest.raises(ValueError, match="secret provider is not available"):
        resolver.resolve(managed("sec_" + "0" * 32))


# --------------------------------------------------------------------------------------------
# the managed provider
# --------------------------------------------------------------------------------------------


def test_the_managed_provider_resolves_a_stored_secret(
    configured: ManagedCredentialStore,
) -> None:
    info = configured.create(ManagedSecretKind.PASSWORD, PASSWORD_MARKER)

    assert ManagedSecretProvider(configured).resolve(info.id) == PASSWORD_MARKER
    assert SecretResolver().resolve(managed(info.id)) == PASSWORD_MARKER


def test_materializing_a_profile_resolves_a_managed_password(
    configured: ManagedCredentialStore,
) -> None:
    info = configured.create(ManagedSecretKind.PASSWORD, PASSWORD_MARKER)

    resolved = SecretResolver().materialize(profile(password=managed(info.id)))

    assert resolved.secrets["password"] == PASSWORD_MARKER
    assert resolved.username == "readonly"
    assert resolved.tls_material == {}


def test_materializing_a_profile_resolves_managed_tls_material(
    configured: ManagedCredentialStore,
) -> None:
    info = configured.create(ManagedSecretKind.CA_CERTIFICATE, CA_MARKER)

    resolved = SecretResolver().materialize(profile(ca_certificate=managed(info.id)))

    assert resolved.tls_material["ca_certificate"] == CA_MARKER
    assert resolved.tls_enabled is True


def test_materializing_mixes_managed_and_environment_references(
    configured: ManagedCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QANERIS_TEST_ENV_SECRET", TOKEN_MARKER)
    info = configured.create(ManagedSecretKind.PASSWORD, PASSWORD_MARKER)
    connection = ConnectionProfile(
        driver="postgresql",
        endpoint=ConnectionEndpoint(hosts=[{"host": "127.0.0.1", "port": 5432}]),
        authentication=AuthenticationConfig(
            method=AuthenticationMethod.PASSWORD, username="readonly", password=managed(info.id)
        ),
        tls=TLSConfig(
            enabled=True,
            ca_certificate=SecretReference(
                provider=SecretProviderKind.ENVIRONMENT, identifier="QANERIS_TEST_ENV_SECRET"
            ),
        ),
    )

    resolved = SecretResolver().materialize(connection)

    assert resolved.secrets["password"] == PASSWORD_MARKER
    assert resolved.tls_material["ca_certificate"] == TOKEN_MARKER


# --------------------------------------------------------------------------------------------
# an unconfigured store must not break anything else
# --------------------------------------------------------------------------------------------


def test_an_unconfigured_managed_store_does_not_break_environment_or_file_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a master key, a deployment that never uses managed secrets keeps working."""
    monkeypatch.delenv(STORE_DIR_ENV, raising=False)
    monkeypatch.delenv(MASTER_KEY_ENV, raising=False)
    monkeypatch.setenv("QANERIS_TEST_ENV_SECRET", PASSWORD_MARKER)
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text(TOKEN_MARKER, encoding="utf-8")

    resolver = SecretResolver()

    assert resolver.resolve(
        SecretReference(provider=SecretProviderKind.ENVIRONMENT, identifier="QANERIS_TEST_ENV_SECRET")
    ) == PASSWORD_MARKER
    assert resolver.resolve(
        SecretReference(provider=SecretProviderKind.FILE, identifier=str(secret_file))
    ) == TOKEN_MARKER


def test_an_unconfigured_managed_store_fails_only_for_a_managed_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(STORE_DIR_ENV, raising=False)
    monkeypatch.delenv(MASTER_KEY_ENV, raising=False)
    monkeypatch.setenv("QANERIS_TEST_ENV_SECRET", PASSWORD_MARKER)
    resolver = SecretResolver()

    with pytest.raises(CredentialStoreConfigurationError) as raised:
        resolver.resolve(managed("sec_" + "0" * 32))

    assert raised.value.code == "credential_store_configuration_error"
    # The very same resolver still serves the other providers.
    assert resolver.resolve(
        SecretReference(provider=SecretProviderKind.ENVIRONMENT, identifier="QANERIS_TEST_ENV_SECRET")
    ) == PASSWORD_MARKER


def test_a_partially_configured_store_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STORE_DIR_ENV, str(tmp_path / "secrets"))
    monkeypatch.delenv(MASTER_KEY_ENV, raising=False)

    with pytest.raises(CredentialStoreConfigurationError):
        SecretResolver().resolve(managed("sec_" + "0" * 32))


def test_resolving_a_managed_reference_for_a_missing_secret_reports_not_found(
    configured: ManagedCredentialStore,
) -> None:
    with pytest.raises(ManagedSecretNotFoundError):
        SecretResolver().resolve(managed("sec_" + "0" * 32))


def test_constructing_a_resolver_never_builds_the_managed_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness for the new provider is lazy by construction, so startup cannot fail on it."""
    monkeypatch.delenv(STORE_DIR_ENV, raising=False)
    monkeypatch.delenv(MASTER_KEY_ENV, raising=False)

    resolver = SecretResolver()

    assert set(resolver.providers) == {
        SecretProviderKind.ENVIRONMENT,
        SecretProviderKind.FILE,
        SecretProviderKind.MANAGED,
    }
    # No store was created and no configuration was read while the resolver was built.
    assert resolver.providers[SecretProviderKind.MANAGED]._store is None
