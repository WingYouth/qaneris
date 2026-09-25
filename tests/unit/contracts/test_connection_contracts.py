from pathlib import Path

import pytest
from pydantic import ValidationError

from smartdata.connections.secrets import (
    EnvironmentSecretProvider,
    FileSecretProvider,
    SecretResolver,
)
from smartdata.contracts import (
    AuthenticationConfig,
    AuthenticationMethod,
    ConnectionEndpoint,
    ConnectionProfile,
    SecretProviderKind,
    SecretReference,
    TLSConfig,
)


def test_connection_url_rejects_embedded_credentials() -> None:
    with pytest.raises(ValidationError, match="cannot contain credentials"):
        ConnectionEndpoint(url="postgresql://user:password@database.example/company")


def test_authentication_requires_matching_secret_reference() -> None:
    with pytest.raises(ValidationError, match="requires its secret reference"):
        AuthenticationConfig(method=AuthenticationMethod.PASSWORD, username="readonly")


def test_tls_requires_client_certificate_and_key_pair() -> None:
    with pytest.raises(ValidationError, match="configured together"):
        TLSConfig(
            enabled=True,
            client_certificate=SecretReference(
                provider=SecretProviderKind.ENVIRONMENT,
                identifier="CLIENT_CERTIFICATE",
            ),
        )


def test_secret_resolver_materializes_without_changing_profile() -> None:
    profile = ConnectionProfile(
        driver="postgresql",
        deployment_mode="cloud",
        endpoint=ConnectionEndpoint(
            hosts=[{"host": "database.example", "port": 5432}], database="company"
        ),
        authentication=AuthenticationConfig(
            method="password",
            username="readonly",
            password=SecretReference(provider="environment", identifier="DATABASE_PASSWORD"),
        ),
        tls=TLSConfig(enabled=True),
    )
    resolver = SecretResolver(
        {SecretProviderKind.ENVIRONMENT: EnvironmentSecretProvider({"DATABASE_PASSWORD": "secret"})}
    )

    resolved = resolver.materialize(profile)

    assert resolved.secrets == {"password": "secret"}
    assert "secret" not in profile.model_dump_json()
    assert profile.authentication.password.identifier == "DATABASE_PASSWORD"


def test_file_secret_provider_reads_certificate_material(tmp_path: Path) -> None:
    certificate = tmp_path / "ca.pem"
    certificate.write_text("certificate-data", encoding="utf-8")

    assert FileSecretProvider().resolve(str(certificate)) == "certificate-data"
