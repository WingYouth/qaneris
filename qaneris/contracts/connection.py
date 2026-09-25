from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

from qaneris.contracts.datasource import DatasourceKind

_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "private_key",
    "service_account",
    "client_private_key",
    "client_certificate",
    "ca_certificate",
}


def contains_inline_secret(value: Any, key: str = "") -> bool:
    if key.lower() in _SENSITIVE_KEYS and value not in (None, ""):
        return True
    if isinstance(value, str) and "://" in value:
        try:
            if urlsplit(value).password is not None:
                return True
        except ValueError:
            # Malformed locators are validated by the adapter; do not mistake them for safe URLs.
            return key.lower() in {"url", "uri", "connection_string"}
    if isinstance(value, dict):
        return any(contains_inline_secret(child, str(name)) for name, child in value.items())
    if isinstance(value, list):
        return any(contains_inline_secret(child, key) for child in value)
    return False


class DeploymentMode(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"
    MANAGED = "managed"


class SecretProviderKind(StrEnum):
    ENVIRONMENT = "environment"
    FILE = "file"
    #: A secret held in the encrypted managed credential store, addressed as ``sec_<uuid4 hex>``.
    MANAGED = "managed"


class AuthenticationMethod(StrEnum):
    NONE = "none"
    PASSWORD = "password"
    TOKEN = "token"
    API_KEY = "api_key"
    CLIENT_CERTIFICATE = "client_certificate"
    SERVICE_ACCOUNT = "service_account"
    PRIVATE_KEY = "private_key"
    CLOUD_IDENTITY = "cloud_identity"


class SecretReference(BaseModel):
    provider: SecretProviderKind
    identifier: str = Field(min_length=1)


class HostPort(BaseModel):
    host: str = Field(min_length=1)
    port: int | None = Field(default=None, ge=1, le=65_535)


class ConnectionEndpoint(BaseModel):
    hosts: list[HostPort] = Field(default_factory=list)
    path: str | None = None
    url: str | None = None
    database: str | None = None
    namespace: str | None = None
    project: str | None = None
    account: str | None = None

    @model_validator(mode="after")
    def validate_locator(self) -> ConnectionEndpoint:
        if not self.hosts and not self.path and not self.url:
            raise ValueError("connection endpoint requires hosts, path, or url")
        if self.url:
            parsed = urlsplit(self.url)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("connection url cannot contain credentials")
        return self


class AuthenticationConfig(BaseModel):
    method: AuthenticationMethod = AuthenticationMethod.NONE
    username: str | None = None
    password: SecretReference | None = None
    token: SecretReference | None = None
    api_key: SecretReference | None = None
    private_key: SecretReference | None = None
    service_account: SecretReference | None = None

    @model_validator(mode="after")
    def validate_required_secret(self) -> AuthenticationConfig:
        required = {
            AuthenticationMethod.PASSWORD: self.password,
            AuthenticationMethod.TOKEN: self.token,
            AuthenticationMethod.API_KEY: self.api_key,
            AuthenticationMethod.PRIVATE_KEY: self.private_key,
            AuthenticationMethod.SERVICE_ACCOUNT: self.service_account,
        }
        if self.method in required and required[self.method] is None:
            raise ValueError(
                f"authentication method {self.method.value} requires its secret reference"
            )
        return self


class TLSConfig(BaseModel):
    enabled: bool = False
    verify_server: bool = True
    server_name: str | None = None
    #: The floor the driver is asked to enforce. TLS 1.0/1.1 are not offered at all - they are
    #: refused at the contract boundary rather than carried down and rejected later.
    minimum_version: Literal["1.2", "1.3"] = "1.2"
    ca_certificate: SecretReference | None = None
    client_certificate: SecretReference | None = None
    client_private_key: SecretReference | None = None
    client_private_key_password: SecretReference | None = None

    @model_validator(mode="after")
    def validate_client_pair(self) -> TLSConfig:
        if (self.client_certificate is None) != (self.client_private_key is None):
            raise ValueError("client certificate and private key must be configured together")
        return self

    @model_validator(mode="after")
    def validate_material_requires_tls(self) -> TLSConfig:
        """Refuse certificate material on a profile that does not enable TLS.

        Material configured while TLS is off can only mean the profile is wrong: either the author
        believed TLS was on, or the material is dead weight the runtime will never use. Both are
        refused rather than accepted and quietly ignored.
        """
        if self.enabled:
            return self
        configured = [
            name
            for name, value in (
                ("ca_certificate", self.ca_certificate),
                ("client_certificate", self.client_certificate),
                ("client_private_key", self.client_private_key),
                ("client_private_key_password", self.client_private_key_password),
            )
            if value is not None
        ]
        if configured:
            raise ValueError(
                f"tls material requires tls enabled: {', '.join(configured)}"
            )
        return self

    @model_validator(mode="after")
    def validate_key_password_requires_pair(self) -> TLSConfig:
        """A private-key password is meaningless without the client certificate and key it unlocks."""
        if self.client_private_key_password is not None and self.client_certificate is None:
            raise ValueError(
                "client private key password requires a client certificate and private key"
            )
        return self


class ConnectionProfile(BaseModel):
    driver: str = Field(min_length=1)
    deployment_mode: DeploymentMode = DeploymentMode.LOCAL
    endpoint: ConnectionEndpoint
    authentication: AuthenticationConfig = Field(default_factory=AuthenticationConfig)
    tls: TLSConfig = Field(default_factory=TLSConfig)
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_secrets_in_options(self) -> ConnectionProfile:
        sensitive = {
            "password",
            "passwd",
            "token",
            "api_key",
            "apikey",
            "secret",
            "private_key",
            "service_account",
        }

        def contains_sensitive_key(value: Any) -> bool:
            if isinstance(value, dict):
                return any(
                    str(name).lower() in sensitive or contains_sensitive_key(child)
                    for name, child in value.items()
                )
            if isinstance(value, list):
                return any(contains_sensitive_key(item) for item in value)
            return False

        if contains_sensitive_key(self.options):
            raise ValueError("connection options cannot contain inline secrets")
        return self


class SecureDatasourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: DatasourceKind
    connection_profile: ConnectionProfile
    workspace_id: str = "default"


class SecureDatasourceTest(BaseModel):
    """A candidate connection to test without creating or changing any datasource.

    This is the ``Test`` half of the ``Test → Save → Scan`` product chain: it carries the same
    structured profile a create would persist, and nothing else, so the test boundary cannot touch
    the catalog even by accident.
    """

    kind: DatasourceKind
    connection_profile: ConnectionProfile


class SecureDatasourceUpdate(BaseModel):
    """The new connection for an existing secure datasource.

    ``datasource_id``, ``workspace_id`` and ``kind`` are deliberately absent: in RS-CONN-01A they
    are immutable, and omitting them is what keeps an update from being able to retarget a
    datasource. Renaming is not part of this contract either.
    """

    connection_profile: ConnectionProfile


class SecureDatasourceTestResult(BaseModel):
    """The safe public outcome of one candidate connection test.

    Only three non-sensitive facts are reported: whether the connection worked, which driver was
    exercised and whether TLS was enabled. The resolved connection, adapter kwargs and every
    credential value stay inside the service - a caller can act on this result but cannot learn a
    secret from it.
    """

    ok: bool = True
    driver: str
    tls_enabled: bool


class ResolvedConnection(BaseModel):
    driver: str
    deployment_mode: DeploymentMode
    endpoint: ConnectionEndpoint
    username: str | None = None
    secrets: dict[str, str] = Field(default_factory=dict)
    tls_enabled: bool = False
    verify_server: bool = True
    server_name: str | None = None
    minimum_tls_version: str = "1.2"
    tls_material: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
