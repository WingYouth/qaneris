"""Contracts for the managed credential store (RS-CRED-01A) and its product boundary (RS-CRED-01B).

The store keeps one encrypted file per secret and publishes only what a caller may see: an opaque
``sec_<uuid>`` identifier, the kind, the creation time and a small, deliberately non-sensitive
metadata document. Secret material never appears here - not the plaintext, not the ciphertext, not
the nonce, and not the master key - and the contract enforces that boundary itself rather than
trusting every caller to remember it.

``ManagedCredentialPublic`` is the product projection of the same facts: it renames the internal
``id`` to the product's ``secret_id`` so no interface ever publishes a store field by accident, and
``public_credential()`` is the single place that projection is made.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Key names that would turn metadata into a secret channel.
_FORBIDDEN_METADATA_KEYS = {
    "api_key",
    "certificate",
    "ciphertext",
    "client_certificate",
    "client_private_key",
    "credential",
    "master_key",
    "nonce",
    "passphrase",
    "passwd",
    "password",
    "plaintext",
    "private_key",
    "secret",
    "token",
    "value",
}

#: Value shapes that are secret material whatever the key is called.
_FORBIDDEN_METADATA_VALUE_MARKERS = ("-----BEGIN", "PRIVATE KEY")


def _normalized_key(value: object) -> str:
    return str(value).strip().casefold().replace("-", "_")


class ManagedSecretKind(StrEnum):
    """The kinds a managed secret may have. The kind is authenticated as part of the AAD."""

    PASSWORD = "password"
    TOKEN = "token"
    API_KEY = "api_key"
    CA_CERTIFICATE = "ca_certificate"
    CLIENT_CERTIFICATE = "client_certificate"
    CLIENT_PRIVATE_KEY = "client_private_key"
    CLIENT_PRIVATE_KEY_PASSWORD = "client_private_key_password"


class ManagedSecretInfo(BaseModel):
    """The public view of one managed secret.

    ``metadata`` carries descriptive facts only - a certificate's subject and fingerprint, a key's
    type and size. It is validated here so a caller cannot publish secret material through it, and
    so a tampered store document cannot be read back as an ``ManagedSecretInfo``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: ManagedSecretKind
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def reject_secret_material(cls, value: dict[str, Any]) -> dict[str, Any]:
        def visit(item: Any, path: str) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    name = _normalized_key(key)
                    if name in _FORBIDDEN_METADATA_KEYS:
                        raise ValueError(
                            f"managed secret metadata cannot carry '{key}'"
                        )
                    visit(child, f"{path}.{name}")
                return
            if isinstance(item, (list, tuple)):
                for child in item:
                    visit(child, path)
                return
            if isinstance(item, str) and any(
                marker in item for marker in _FORBIDDEN_METADATA_VALUE_MARKERS
            ):
                raise ValueError("managed secret metadata cannot carry key material")

        visit(value, "metadata")
        return value


#: Kinds a caller may submit as a JSON text secret. Certificates and private keys are deliberately
#: absent: they have their own multipart entry points, where the validator sees the real bytes and a
#: client certificate and its key are checked as a pair before either is stored.
TEXT_SECRET_KINDS = frozenset(
    {
        ManagedSecretKind.PASSWORD,
        ManagedSecretKind.TOKEN,
        ManagedSecretKind.API_KEY,
        ManagedSecretKind.CLIENT_PRIVATE_KEY_PASSWORD,
    }
)

#: Kinds a caller may submit as a certificate upload file.
CERTIFICATE_KINDS = frozenset(
    {
        ManagedSecretKind.CA_CERTIFICATE,
        ManagedSecretKind.CLIENT_CERTIFICATE,
        ManagedSecretKind.CLIENT_PRIVATE_KEY,
    }
)

#: Longest text secret the product boundary accepts. Characters, not bytes.
TEXT_SECRET_MAX_CHARS = 65_536

#: Largest accepted certificate or private key, shared by every product entry point (HTTP and CLI).
#: Real material is orders of magnitude smaller; a bound this far above it can only ever refuse
#: something that is not what it claims. Defined once here so no interface keeps a second copy.
CREDENTIAL_FILE_MAX_BYTES = 1024 * 1024


class ManagedCredentialPublic(BaseModel):
    """The product view of one managed secret.

    Deliberately a different model from ``ManagedSecretInfo``: the store's field is ``id``, the
    product's field is ``secret_id``, and keeping them apart means an interface cannot publish an
    internal field by serializing the wrong object.
    """

    model_config = ConfigDict(extra="forbid")

    secret_id: str
    kind: ManagedSecretKind
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


def public_credential(info: ManagedSecretInfo) -> ManagedCredentialPublic:
    """Project one internal secret record onto the product contract.

    The single conversion point, so every interface publishes the same fields and the store's
    internal name never reaches a response.
    """
    return ManagedCredentialPublic(
        secret_id=info.id,
        kind=info.kind,
        created_at=info.created_at,
        metadata=info.metadata,
    )


class ManagedTextSecretCreate(BaseModel):
    """A JSON text-secret submission.

    Only ``TEXT_SECRET_KINDS`` are accepted, and the value is never stripped: leading and trailing
    whitespace can be a legitimate part of a password or token, so the only emptiness check is that
    the string is non-empty.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ManagedSecretKind
    value: str = Field(min_length=1, max_length=TEXT_SECRET_MAX_CHARS)

    @model_validator(mode="after")
    def reject_file_only_kinds(self) -> ManagedTextSecretCreate:
        if self.kind not in TEXT_SECRET_KINDS:
            allowed = ", ".join(sorted(kind.value for kind in TEXT_SECRET_KINDS))
            raise ValueError(
                f"kind '{self.kind.value}' must be uploaded as a file; JSON accepts: {allowed}"
            )
        return self


class ManagedClientIdentityPublic(BaseModel):
    """The product view of one mTLS client identity: two opaque references, nothing else."""

    model_config = ConfigDict(extra="forbid")

    client_certificate: ManagedCredentialPublic
    client_private_key: ManagedCredentialPublic
