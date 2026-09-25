from __future__ import annotations

from pathlib import Path
from typing import Protocol

from qaneris.connections.managed_store import ManagedCredentialStore
from qaneris.contracts.connection import (
    ConnectionProfile,
    ResolvedConnection,
    SecretProviderKind,
    SecretReference,
)


class SecretProvider(Protocol):
    def resolve(self, identifier: str) -> str: ...


class EnvironmentSecretProvider:
    def __init__(self, environment: dict[str, str] | None = None):
        import os

        self.environment = environment if environment is not None else os.environ

    def resolve(self, identifier: str) -> str:
        try:
            return self.environment[identifier]
        except KeyError as error:
            raise ValueError(f"environment secret is not configured: {identifier}") from error


class FileSecretProvider:
    def resolve(self, identifier: str) -> str:
        path = Path(identifier).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"secret file does not exist: {path}")
        return path.read_text(encoding="utf-8")


class ManagedSecretProvider:
    """Resolves a managed reference through the encrypted credential store.

    The store is built on first use rather than at construction. A deployment that only uses
    environment and file references must keep starting without ``QANERIS_MASTER_KEY`` and
    ``QANERIS_SECRET_STORE_DIR`` configured, so the configuration is required exactly when a
    managed reference is actually resolved - and then a missing one fails closed with
    ``credential_store_configuration_error`` instead of falling back to anything weaker.
    """

    def __init__(self, store: ManagedCredentialStore | None = None):
        self._store = store

    @property
    def store(self) -> ManagedCredentialStore:
        if self._store is None:
            self._store = ManagedCredentialStore.from_environment()
        return self._store

    def resolve(self, identifier: str) -> str:
        return self.store.resolve(identifier)


class SecretResolver:
    def __init__(self, providers: dict[SecretProviderKind, SecretProvider] | None = None):
        self.providers = providers or {
            SecretProviderKind.ENVIRONMENT: EnvironmentSecretProvider(),
            SecretProviderKind.FILE: FileSecretProvider(),
            SecretProviderKind.MANAGED: ManagedSecretProvider(),
        }

    def resolve(self, reference: SecretReference) -> str:
        try:
            provider = self.providers[reference.provider]
        except KeyError as error:
            raise ValueError(
                f"secret provider is not available: {reference.provider.value}"
            ) from error
        return provider.resolve(reference.identifier)

    def materialize(self, profile: ConnectionProfile) -> ResolvedConnection:
        authentication = profile.authentication
        secrets = {
            name: self.resolve(reference)
            for name, reference in {
                "password": authentication.password,
                "token": authentication.token,
                "api_key": authentication.api_key,
                "private_key": authentication.private_key,
                "service_account": authentication.service_account,
            }.items()
            if reference is not None
        }
        tls_material = {
            name: self.resolve(reference)
            for name, reference in {
                "ca_certificate": profile.tls.ca_certificate,
                "client_certificate": profile.tls.client_certificate,
                "client_private_key": profile.tls.client_private_key,
                "client_private_key_password": profile.tls.client_private_key_password,
            }.items()
            if reference is not None
        }
        return ResolvedConnection(
            driver=profile.driver,
            deployment_mode=profile.deployment_mode,
            endpoint=profile.endpoint,
            username=authentication.username,
            secrets=secrets,
            tls_enabled=profile.tls.enabled,
            verify_server=profile.tls.verify_server,
            server_name=profile.tls.server_name,
            minimum_tls_version=profile.tls.minimum_version,
            tls_material=tls_material,
            options=profile.options,
        )
