"""Certificate and private key validation (RS-CRED-01A).

The managed store encrypts whatever it is given; deciding whether a certificate is *usable* is a
different job, and it belongs here. This module is the only place that parses X.509 material, and it
always produces the same two things: a canonical PEM form to store, and a small metadata document
that describes the material without carrying a byte of it.

Every rejection is a ``CertificateValidationError`` whose message names the problem - expired, not
yet valid, wrong purpose, mismatched pair - and never the certificate body or the key bytes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

from qaneris.common.errors import (
    CertificateKeyMismatchError,
    CertificateValidationError,
)

#: Public key form compared to decide whether a certificate and a key are a pair.
_PUBLIC_KEY_ENCODING = serialization.Encoding.DER
_PUBLIC_KEY_FORMAT = serialization.PublicFormat.SubjectPublicKeyInfo

#: Every private key is stored in this canonical form.
_PEM_PRIVATE_KEY_ENCODING = serialization.Encoding.PEM
_PEM_PRIVATE_KEY_FORMAT = serialization.PrivateFormat.PKCS8


class CertificateValidator:
    """Normalizes CA certificates, client certificates and private keys."""

    # ----------------------------------------------------------------------------------------
    # certificates
    # ----------------------------------------------------------------------------------------

    def normalize_ca_certificate(self, value: str | bytes) -> tuple[str, dict[str, Any]]:
        """Normalize a trust anchor to PEM, refusing one that cannot serve as one.

        A pinned leaf certificate is a legitimate trust anchor, so a certificate without CA basic
        constraints is accepted. What is refused is the case that is certainly wrong: a certificate
        whose extended key usage marks it as nothing but a client identity, which no TLS stack will
        accept as a server trust anchor.
        """
        certificate = self._load_certificate(value)
        usage = self._extended_key_usage(certificate)
        if usage is not None and usage == {ExtendedKeyUsageOID.CLIENT_AUTH}:
            raise CertificateValidationError(
                "CA certificate cannot be a client-only identity certificate"
            )
        return self._certificate_pem(certificate), self._certificate_metadata(certificate)

    def normalize_client_certificate(self, value: str | bytes) -> tuple[str, dict[str, Any]]:
        """Normalize a client identity certificate to PEM, refusing a CA certificate."""
        certificate = self._load_certificate(value)
        constraints = self._basic_constraints(certificate)
        if constraints is not None and constraints.ca:
            raise CertificateValidationError("client certificate cannot be a CA certificate")
        return self._certificate_pem(certificate), self._certificate_metadata(certificate)

    def _load_certificate(self, value: str | bytes) -> x509.Certificate:
        """Parse PEM or DER, then require that the certificate is currently in force."""
        data = self._as_bytes(value)
        certificate = self._try(x509.load_pem_x509_certificate, data)
        if certificate is None:
            certificate = self._try(x509.load_der_x509_certificate, data)
        if certificate is None:
            raise CertificateValidationError("certificate is neither valid PEM nor valid DER")
        self._require_current_validity(certificate)
        return certificate

    @staticmethod
    def _try(loader: Callable[[bytes], Any], data: bytes) -> Any:
        try:
            return loader(data)
        except Exception:  # noqa: BLE001 - every parse failure is the same refusal
            return None

    @staticmethod
    def _require_current_validity(certificate: x509.Certificate) -> None:
        now = datetime.now(UTC)
        if now < certificate.not_valid_before_utc:
            raise CertificateValidationError("certificate is not valid yet")
        if now > certificate.not_valid_after_utc:
            raise CertificateValidationError("certificate has expired")

    @staticmethod
    def _extended_key_usage(
        certificate: x509.Certificate,
    ) -> set[x509.ObjectIdentifier] | None:
        try:
            extension = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        except x509.ExtensionNotFound:
            return None
        return set(extension.value)

    @staticmethod
    def _basic_constraints(certificate: x509.Certificate) -> x509.BasicConstraints | None:
        try:
            return certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
        except x509.ExtensionNotFound:
            return None

    @staticmethod
    def _certificate_pem(certificate: x509.Certificate) -> str:
        return certificate.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    @staticmethod
    def _certificate_metadata(certificate: x509.Certificate) -> dict[str, Any]:
        """Descriptive facts only - never the body, and nothing derived from the private key."""
        return {
            "subject": certificate.subject.rfc4514_string(),
            "issuer": certificate.issuer.rfc4514_string(),
            "serial_number": str(certificate.serial_number),
            "not_valid_before": certificate.not_valid_before_utc.isoformat(),
            "not_valid_after": certificate.not_valid_after_utc.isoformat(),
            "sha256_fingerprint": certificate.fingerprint(hashes.SHA256()).hex(),
        }

    # ----------------------------------------------------------------------------------------
    # private keys
    # ----------------------------------------------------------------------------------------

    def normalize_private_key(
        self,
        value: str | bytes,
        *,
        password: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Normalize a private key to unencrypted PKCS#8 PEM, verifying any password.

        A PKCS#8 or TraditionalOpenSSL key in PEM or DER is accepted. An encrypted key with a
        missing or wrong password is refused here rather than stored and discovered at connection
        time: nothing is kept "to be validated later". The result is unencrypted because the managed
        store encrypts the document at rest, and whether the *input* was encrypted is recorded in
        the metadata instead.
        """
        data = self._as_bytes(value)
        secret = password.encode("utf-8") if password is not None else None
        encrypted_input = not self._opens_without_password(data)
        key = self._open_private_key(data, secret)
        if key is None and secret is not None:
            # A password was supplied for a key that does not need one; the key itself is still
            # valid, so it is accepted rather than refused for an irrelevant argument.
            key = self._open_private_key(data, None)
        if key is None:
            raise CertificateValidationError(
                "private key is neither valid PEM nor valid DER; if it is encrypted, its password "
                "is missing or wrong"
            )
        normalized = key.private_bytes(
            _PEM_PRIVATE_KEY_ENCODING, _PEM_PRIVATE_KEY_FORMAT, serialization.NoEncryption()
        ).decode("utf-8")
        return normalized, self._private_key_metadata(key, encrypted_input=encrypted_input)

    def _open_private_key(self, data: bytes, secret: bytes | None) -> Any:
        """Load a PEM or DER private key, returning ``None`` when neither form is readable."""
        key = self._load_with_password(serialization.load_pem_private_key, data, secret)
        if key is None:
            key = self._load_with_password(serialization.load_der_private_key, data, secret)
        return key

    @staticmethod
    def _load_with_password(loader: Callable[..., Any], data: bytes, secret: bytes | None) -> Any:
        # A wrong password, a missing password and a malformed key are one refusal here: the caller
        # learns that the key could not be read, never which of the three it was.
        try:
            return loader(data, password=secret)
        except Exception:  # noqa: BLE001 - every reason is the same refusal
            return None

    def _opens_without_password(self, data: bytes) -> bool:
        """Report whether the input is an unencrypted private key, without trusting any marker."""
        return self._open_private_key(data, None) is not None

    @staticmethod
    def _private_key_metadata(key: Any, *, encrypted_input: bool) -> dict[str, Any]:
        """Key type, size and whether the input was encrypted - never the key itself."""
        if isinstance(key, rsa.RSAPrivateKey):
            key_type = "RSA"
        elif isinstance(key, ec.EllipticCurvePrivateKey):
            key_type = "EC"
        elif isinstance(key, ed25519.Ed25519PrivateKey):
            key_type = "Ed25519"
        elif isinstance(key, ed448.Ed448PrivateKey):
            key_type = "Ed448"
        else:  # pragma: no cover - every key cryptography can load is named above
            key_type = type(key).__name__
        return {
            "key_type": key_type,
            "key_size": getattr(key, "key_size", None),
            "encrypted_input": encrypted_input,
        }

    # ----------------------------------------------------------------------------------------
    # pair
    # ----------------------------------------------------------------------------------------

    def validate_client_pair(
        self,
        certificate_pem: str,
        private_key_pem: str,
        *,
        private_key_password: str | None = None,
    ) -> None:
        """Verify that a client certificate and a private key are the same key pair.

        The two are compared by public key, so an obviously wrong combination is refused here
        instead of surfacing as a handshake failure against a production database.
        """
        certificate = self._load_certificate(certificate_pem)
        secret = private_key_password.encode("utf-8") if private_key_password else None
        key = self._open_private_key(self._as_bytes(private_key_pem), secret)
        if key is None:
            raise CertificateValidationError(
                "private key could not be read while checking the client pair"
            )
        if self._public_key_bytes(certificate.public_key()) != self._public_key_bytes(
            key.public_key()
        ):
            raise CertificateKeyMismatchError(
                "client certificate and private key are not the same key pair"
            )

    @staticmethod
    def _public_key_bytes(key: Any) -> bytes:
        return key.public_bytes(_PUBLIC_KEY_ENCODING, _PUBLIC_KEY_FORMAT)

    @staticmethod
    def _as_bytes(value: str | bytes) -> bytes:
        if isinstance(value, bytes):
            return value
        return value.encode("utf-8")
