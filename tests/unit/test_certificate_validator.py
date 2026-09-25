"""Unit tests for the certificate and private key validator (RS-CRED-01A).

Every certificate and key used here is generated inside the test process. No real certificate, key
or fixture file is committed, so there is nothing in the repository to leak and nothing that can
expire and turn a healthy suite red.

The suite is about refusal as much as acceptance: an expired certificate, a client identity offered
as a trust anchor, an encrypted key without its password and a mismatched pair must all be refused
with a message that says why and never carries the material itself.
"""

from __future__ import annotations

import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from smartdata.common.errors import (
    CertificateKeyMismatchError,
    CertificateValidationError,
)
from smartdata.connections.certificate_validator import CertificateValidator

CLIENT_PASSWORD = "UNIQUE_KEY_PASSWORD_MARKER"


def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def certificate(
    *,
    ca: bool,
    extended_usage: list[x509.ObjectIdentifier] | None = None,
    not_before: dt.datetime | None = None,
    not_after: dt.datetime | None = None,
    key: object | None = None,
) -> tuple[x509.Certificate, object]:
    """Build a self-signed certificate for one purpose, valid by default."""
    issuer_key = key or private_key()
    now = dt.datetime.now(dt.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SmartData Test CA" if ca else "client")])
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
        builder = builder.add_extension(
            x509.ExtendedKeyUsage(extended_usage), critical=False
        )
    return builder.sign(issuer_key, hashes.SHA256()), issuer_key


def client_certificate() -> tuple[x509.Certificate, object]:
    return certificate(ca=False, extended_usage=[ExtendedKeyUsageOID.CLIENT_AUTH])


def pem(value: object) -> str:
    return value.public_bytes(serialization.Encoding.PEM).decode("utf-8")


def der(value: object) -> bytes:
    return value.public_bytes(serialization.Encoding.DER)


def key_pem(
    key: object,
    *,
    password: bytes | None = None,
    traditional: bool = False,
) -> str:
    form = (
        serialization.PrivateFormat.TraditionalOpenSSL
        if traditional
        else serialization.PrivateFormat.PKCS8
    )
    encryption = (
        serialization.BestAvailableEncryption(password)
        if password is not None
        else serialization.NoEncryption()
    )
    return key.private_bytes(serialization.Encoding.PEM, form, encryption).decode("utf-8")


@pytest.fixture
def validator() -> CertificateValidator:
    return CertificateValidator()


# --------------------------------------------------------------------------------------------
# certificates
# --------------------------------------------------------------------------------------------


def test_a_valid_pem_ca_certificate_is_normalized(validator: CertificateValidator) -> None:
    ca, _ = certificate(ca=True)

    normalized, metadata = validator.normalize_ca_certificate(pem(ca))

    assert normalized.startswith("-----BEGIN CERTIFICATE-----")
    assert normalized.endswith("-----END CERTIFICATE-----\n")
    assert metadata["subject"] == "CN=SmartData Test CA"
    assert metadata["issuer"] == "CN=SmartData Test CA"
    assert metadata["serial_number"] == str(ca.serial_number)
    assert metadata["sha256_fingerprint"] == ca.fingerprint(hashes.SHA256()).hex()
    assert metadata["not_valid_after"].startswith(str(ca.not_valid_after_utc.year))


def test_a_der_ca_certificate_normalizes_to_the_same_pem(validator: CertificateValidator) -> None:
    ca, _ = certificate(ca=True)

    from_der, _ = validator.normalize_ca_certificate(der(ca))
    from_pem, _ = validator.normalize_ca_certificate(pem(ca))

    assert from_der == from_pem


def test_a_valid_client_certificate_is_normalized(validator: CertificateValidator) -> None:
    client, _ = client_certificate()

    normalized, metadata = validator.normalize_client_certificate(der(client))

    assert normalized.startswith("-----BEGIN CERTIFICATE-----")
    assert metadata["subject"] == "CN=client"


def test_an_expired_certificate_is_rejected(validator: CertificateValidator) -> None:
    now = dt.datetime.now(dt.UTC)
    expired, _ = certificate(
        ca=True, not_before=now - dt.timedelta(days=30), not_after=now - dt.timedelta(days=1)
    )

    with pytest.raises(CertificateValidationError, match="expired"):
        validator.normalize_ca_certificate(pem(expired))


def test_a_certificate_that_is_not_valid_yet_is_rejected(validator: CertificateValidator) -> None:
    now = dt.datetime.now(dt.UTC)
    future, _ = certificate(
        ca=True, not_before=now + dt.timedelta(days=1), not_after=now + dt.timedelta(days=30)
    )

    with pytest.raises(CertificateValidationError, match="not valid yet"):
        validator.normalize_ca_certificate(pem(future))


@pytest.mark.parametrize(
    "value",
    [
        "not a certificate at all",
        "-----BEGIN CERTIFICATE-----\nbm90IGEgY2VydA==\n-----END CERTIFICATE-----\n",
        b"\x30\x82\x01\x00not-really-der",
        "",
    ],
)
def test_unparseable_certificates_are_rejected(
    validator: CertificateValidator, value: str | bytes
) -> None:
    with pytest.raises(CertificateValidationError) as raised:
        validator.normalize_ca_certificate(value)

    assert "-----BEGIN" not in str(raised.value)
    assert raised.value.code == "certificate_validation_failed"


def test_a_client_only_identity_cannot_be_a_trust_anchor(validator: CertificateValidator) -> None:
    client, _ = client_certificate()

    with pytest.raises(CertificateValidationError, match="client-only"):
        validator.normalize_ca_certificate(pem(client))


def test_a_ca_certificate_cannot_be_a_client_identity(validator: CertificateValidator) -> None:
    ca, _ = certificate(ca=True)

    with pytest.raises(CertificateValidationError, match="CA certificate"):
        validator.normalize_client_certificate(pem(ca))


def test_a_pinned_leaf_certificate_is_still_a_usable_trust_anchor(
    validator: CertificateValidator,
) -> None:
    """Pinning a server certificate is a normal practice; it must not be refused as a CA."""
    leaf, _ = certificate(ca=False, extended_usage=[ExtendedKeyUsageOID.SERVER_AUTH])

    normalized, _ = validator.normalize_ca_certificate(pem(leaf))

    assert normalized.startswith("-----BEGIN CERTIFICATE-----")


# --------------------------------------------------------------------------------------------
# private keys
# --------------------------------------------------------------------------------------------


def test_an_unencrypted_private_key_is_normalized_to_pkcs8(validator: CertificateValidator) -> None:
    key = private_key()

    normalized, metadata = validator.normalize_private_key(key_pem(key))

    assert normalized.startswith("-----BEGIN PRIVATE KEY-----")
    assert "ENCRYPTED" not in normalized
    assert metadata == {"key_type": "RSA", "key_size": 2048, "encrypted_input": False}


def test_a_traditional_openssl_key_is_normalized_to_pkcs8(validator: CertificateValidator) -> None:
    key = private_key()

    normalized, _ = validator.normalize_private_key(key_pem(key, traditional=True))

    assert normalized.startswith("-----BEGIN PRIVATE KEY-----")


def test_a_der_private_key_is_normalized_to_pem(validator: CertificateValidator) -> None:
    key = private_key()
    encoded = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    normalized, _ = validator.normalize_private_key(encoded)

    assert normalized.startswith("-----BEGIN PRIVATE KEY-----")


def test_an_elliptic_curve_key_is_reported_by_curve(validator: CertificateValidator) -> None:
    key = ec.generate_private_key(ec.SECP256R1())

    normalized, metadata = validator.normalize_private_key(key_pem(key))

    assert normalized.startswith("-----BEGIN PRIVATE KEY-----")
    assert metadata["key_type"] == "EC"
    assert metadata["key_size"] == 256


def test_an_encrypted_private_key_is_accepted_with_its_password(
    validator: CertificateValidator,
) -> None:
    key = private_key()

    normalized, metadata = validator.normalize_private_key(
        key_pem(key, password=CLIENT_PASSWORD.encode()), password=CLIENT_PASSWORD
    )

    assert normalized.startswith("-----BEGIN PRIVATE KEY-----")
    assert "ENCRYPTED" not in normalized
    assert metadata["encrypted_input"] is True


def test_an_encrypted_private_key_with_a_wrong_password_is_rejected(
    validator: CertificateValidator,
) -> None:
    encrypted = key_pem(private_key(), password=CLIENT_PASSWORD.encode())

    with pytest.raises(CertificateValidationError) as raised:
        validator.normalize_private_key(encrypted, password="not-the-password")

    assert CLIENT_PASSWORD not in str(raised.value)
    assert "-----BEGIN" not in str(raised.value)


def test_an_encrypted_private_key_without_a_password_is_rejected(
    validator: CertificateValidator,
) -> None:
    encrypted = key_pem(private_key(), password=CLIENT_PASSWORD.encode())

    with pytest.raises(CertificateValidationError) as raised:
        validator.normalize_private_key(encrypted)

    assert "password" in str(raised.value)
    assert CLIENT_PASSWORD not in str(raised.value)


@pytest.mark.parametrize("value", ["", "not a key", "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n"])
def test_unparseable_private_keys_are_rejected(
    validator: CertificateValidator, value: str
) -> None:
    with pytest.raises(CertificateValidationError) as raised:
        validator.normalize_private_key(value)

    assert "nope" not in str(raised.value)


def test_a_key_does_not_expose_itself_through_metadata(validator: CertificateValidator) -> None:
    key = private_key()

    _, metadata = validator.normalize_private_key(key_pem(key, password=CLIENT_PASSWORD.encode()), password=CLIENT_PASSWORD)

    assert set(metadata) == {"key_type", "key_size", "encrypted_input"}
    assert "-----BEGIN" not in str(metadata)
    assert CLIENT_PASSWORD not in str(metadata)


# --------------------------------------------------------------------------------------------
# pair matching
# --------------------------------------------------------------------------------------------


def test_a_matching_certificate_and_key_pass(validator: CertificateValidator) -> None:
    client, key = client_certificate()
    normalized, _ = validator.normalize_private_key(key_pem(key))

    validator.validate_client_pair(pem(client), normalized)  # no exception is the assertion


def test_a_matching_encrypted_key_passes_with_its_password(
    validator: CertificateValidator,
) -> None:
    client, key = client_certificate()
    encrypted = key_pem(key, password=CLIENT_PASSWORD.encode())

    validator.validate_client_pair(pem(client), encrypted, private_key_password=CLIENT_PASSWORD)


def test_a_mismatched_pair_is_refused(validator: CertificateValidator) -> None:
    client, _ = client_certificate()
    _, other_key = client_certificate()
    other_normalized, _ = validator.normalize_private_key(key_pem(other_key))

    with pytest.raises(CertificateKeyMismatchError) as raised:
        validator.validate_client_pair(pem(client), other_normalized)

    assert raised.value.code == "certificate_key_mismatch"
    assert raised.value.status_code == 400
    assert "-----BEGIN" not in str(raised.value)


def test_an_unreadable_key_in_a_pair_check_is_reported_as_a_validation_failure(
    validator: CertificateValidator,
) -> None:
    client, _ = client_certificate()

    with pytest.raises(CertificateValidationError):
        validator.validate_client_pair(pem(client), "not a key")
