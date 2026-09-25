"""The credential service: validation, normalization and reference-protected lifecycle.

This is the layer a product interface is allowed to call. It answers one question the store cannot -
"is anything still using this secret?" - and one the store should not have to - "is this material
actually a usable certificate or key?" - while keeping the store itself a small, auditable
encryption component.

It deliberately stops there. It does not test a connection, does not touch a datasource and does not
materialize TLS for a driver: those belong to secure datasource management (RS-CONN-01), which needs
a live database to do its job.
"""

from __future__ import annotations

import logging

from smartdata.catalog import Catalog
from smartdata.common.errors import (
    ManagedSecretInUseError,
    ManagedSecretNotFoundError,
)
from smartdata.connections.certificate_validator import CertificateValidator
from smartdata.connections.managed_store import ManagedCredentialStore
from smartdata.contracts.credentials import (
    ManagedSecretInfo,
    ManagedSecretKind,
)

logger = logging.getLogger(__name__)


class CredentialService:
    """The only entry point for creating, inspecting and deleting managed secrets."""

    def __init__(
        self,
        store: ManagedCredentialStore,
        catalog: Catalog,
        certificate_validator: CertificateValidator | None = None,
    ):
        self.store = store
        self.catalog = catalog
        self.certificate_validator = certificate_validator or CertificateValidator()

    def create_secret(
        self,
        kind: ManagedSecretKind,
        plaintext: str | bytes,
        *,
        private_key_password: str | None = None,
    ) -> ManagedSecretInfo:
        """Validate, normalize and store one secret; return its public view.

        Certificates and private keys are normalized before they reach the store, so what is stored
        is always canonical PEM and the metadata is always derived from parsed material rather than
        taken on trust. ``private_key_password`` is only meaningful for
        ``CLIENT_PRIVATE_KEY``, where a wrong password is a refusal rather than something to be
        discovered later.

        ``bytes`` is accepted for certificate kinds only, because a certificate or key may be DER
        rather than PEM and a DER body is not text. A text kind given ``bytes`` is refused rather
        than decoded: guessing an encoding for a password or token is how a secret silently becomes
        a different secret.
        """
        if kind is ManagedSecretKind.CA_CERTIFICATE:
            normalized, metadata = self.certificate_validator.normalize_ca_certificate(plaintext)
            return self.store.create(kind, normalized, metadata=metadata)
        if kind is ManagedSecretKind.CLIENT_CERTIFICATE:
            normalized, metadata = self.certificate_validator.normalize_client_certificate(plaintext)
            return self.store.create(kind, normalized, metadata=metadata)
        if kind is ManagedSecretKind.CLIENT_PRIVATE_KEY:
            normalized, metadata = self.certificate_validator.normalize_private_key(
                plaintext, password=private_key_password
            )
            return self.store.create(kind, normalized, metadata=metadata)
        if not isinstance(plaintext, str):
            # ValueError, not TypeError: an unusable request argument is reported by the API as
            # ``invalid_request``, and every sibling refusal in this method already does the same.
            raise ValueError(  # noqa: TRY004
                f"managed secret of kind {kind.value} is text; bytes are only accepted for "
                "certificate and private key material"
            )
        if not plaintext:
            raise ValueError(f"managed secret of kind {kind.value} cannot be empty")
        return self.store.create(kind, plaintext)

    def create_client_identity(
        self,
        certificate: str | bytes,
        private_key: str | bytes,
        *,
        private_key_password: str | None = None,
    ) -> tuple[ManagedSecretInfo, ManagedSecretInfo]:
        """Validate an mTLS client certificate and key as a pair, then store both.

        The order is the contract: normalize the certificate, normalize the key (decrypting it with
        ``private_key_password`` if it is encrypted), prove the two are the same key pair, and only
        then write anything. Storing first and checking afterwards would leave an unusable secret
        behind on every mismatch.

        The pair is also atomic from the caller's point of view even though the store writes one
        file per secret: if the second write fails, the first secret is removed again, so a failed
        import never leaves an orphan credential behind.

        Returns ``(client_certificate, client_private_key)`` in that fixed order. The decrypt
        password is transient - the stored key is unencrypted PKCS#8, so nothing needs it later.
        """
        certificate_pem, certificate_metadata = self.certificate_validator.normalize_client_certificate(
            certificate
        )
        private_key_pem, private_key_metadata = self.certificate_validator.normalize_private_key(
            private_key, password=private_key_password
        )
        self.certificate_validator.validate_client_pair(certificate_pem, private_key_pem)

        stored_certificate = self.store.create(
            ManagedSecretKind.CLIENT_CERTIFICATE,
            certificate_pem,
            metadata=certificate_metadata,
        )
        try:
            stored_private_key = self.store.create(
                ManagedSecretKind.CLIENT_PRIVATE_KEY,
                private_key_pem,
                metadata=private_key_metadata,
            )
        except Exception:
            self._roll_back_identity(stored_certificate)
            raise
        return stored_certificate, stored_private_key

    def _roll_back_identity(self, stored: ManagedSecretInfo) -> None:
        """Undo the first half of a failed pair write.

        The secret was never returned to a caller and no connection profile can name it yet, so it
        is removed without the reference check a normal delete performs. If even that fails, the
        original creation error is still what the caller sees - the rollback is best effort - and
        the log line carries the identifier and the error type only, never any material.
        """
        try:
            self.store.delete(stored.id)
        except Exception as error:  # noqa: BLE001 - rollback must not mask the creation error
            logger.warning(
                "managed secret rollback failed: secret_id=%s error=%s",
                stored.id,
                type(error).__name__,
            )

    def inspect_secret(self, secret_id: str) -> ManagedSecretInfo:
        """Return the public view of one secret without decrypting it."""
        return self.store.inspect(secret_id)

    def delete_secret(self, secret_id: str) -> None:
        """Delete one secret, but only while no stored connection profile still references it.

        Existence is checked first so a call for an unknown secret reports that plainly, and the
        reference count comes from the catalog's parsed profiles rather than from a text search, so
        a reference can never be missed because of formatting.
        """
        if not self.store.exists(secret_id):
            raise ManagedSecretNotFoundError("managed secret does not exist")
        references = self.catalog.count_managed_secret_references(secret_id)
        if references:
            raise ManagedSecretInUseError(
                f"managed secret {secret_id} is still referenced by {references} connection profile"
                f"{'s' if references > 1 else ''}"
            )
        self.store.delete(secret_id)
