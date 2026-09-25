"""The managed credential store: AES-256-GCM at rest, one document per secret (RS-CRED-01A).

The store owns physical protection and nothing else. It does not know what a secret is for, cannot
test a connection, and cannot decide whether deleting a secret is safe - reference protection
belongs to ``CredentialService``, which can see the catalog. That split is what keeps this module
small enough to audit: every byte it writes goes through one encryption path, and every byte it
reads comes back through one authenticated decryption path.

Threat model in one line: an attacker who reads the store directory learns which secrets exist and
what kind they are, and nothing about their values.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from qaneris.common.artifacts import project_root
from qaneris.common.errors import (
    CredentialStoreConfigurationError,
    ManagedSecretIntegrityError,
    ManagedSecretNotFoundError,
)
from qaneris.contracts.credentials import ManagedSecretInfo, ManagedSecretKind

#: Configuration the managed store cannot invent for itself.
STORE_DIR_ENV = "QANERIS_SECRET_STORE_DIR"
MASTER_KEY_ENV = "QANERIS_MASTER_KEY"

#: File format version. A different version is refused rather than guessed at.
DOCUMENT_VERSION = 1
ALGORITHM = "AES-256-GCM"

#: AES-GCM parameters. 12 bytes is the size the mode is specified for; the key is AES-256.
KEY_BYTES = 32
NONCE_BYTES = 12

#: Additional authenticated data: binds a document to its own identity and kind, so a ciphertext
#: cannot be moved to another secret id or replayed under another kind.
_AAD_TEMPLATE = "qaneris-managed-secret-v1|{secret_id}|{kind}"

SECRET_ID_PREFIX = "sec_"

#: A managed secret id is exactly ``sec_`` plus the hex form of a UUID4.
_SECRET_ID_PATTERN = re.compile(rf"^{SECRET_ID_PREFIX}[0-9a-f]{{32}}$")

_STORE_DIRECTORY_MODE = 0o700
_SECRET_FILE_MODE = 0o600


class ManagedCredentialStore:
    """Encrypted, file-per-secret storage for credentials and TLS material."""

    def __init__(self, store_dir: Path, master_key: bytes):
        if len(master_key) != KEY_BYTES:
            raise CredentialStoreConfigurationError(
                f"managed credential store requires a {KEY_BYTES}-byte master key"
            )
        self.store_dir = Path(store_dir)
        self._master_key = master_key

    # ----------------------------------------------------------------------------------------
    # composition
    # ----------------------------------------------------------------------------------------

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        create: bool = True,
    ) -> ManagedCredentialStore:
        """Build the store from ``QANERIS_SECRET_STORE_DIR`` and ``QANERIS_MASTER_KEY``.

        Configuration is mandatory and never inferred: the store directory must be an explicit path
        outside the repository, and the master key must be 32 bytes of URL-safe Base64. Nothing is
        generated here, so a deployment that forgot to configure the store fails closed instead of
        silently writing secrets under a key that exists only in memory.
        """
        env = environment if environment is not None else os.environ
        store_dir = cls._store_directory(env)
        master_key = cls._master_key(env)
        if create:
            cls._ensure_directory(store_dir)
        return cls(store_dir, master_key)

    @staticmethod
    def _store_directory(env: Mapping[str, str]) -> Path:
        configured = (env.get(STORE_DIR_ENV) or "").strip()
        if not configured:
            raise CredentialStoreConfigurationError(
                f"managed credential store requires {STORE_DIR_ENV}"
            )
        store_dir = Path(configured).expanduser().resolve()
        repository = project_root()
        if store_dir == repository or repository in store_dir.parents:
            raise CredentialStoreConfigurationError(
                f"{STORE_DIR_ENV} must be outside the Qaneris project directory"
            )
        return store_dir

    @staticmethod
    def _master_key(env: Mapping[str, str]) -> bytes:
        configured = (env.get(MASTER_KEY_ENV) or "").strip()
        if not configured:
            raise CredentialStoreConfigurationError(
                f"managed credential store requires {MASTER_KEY_ENV}"
            )
        try:
            key = base64.urlsafe_b64decode(configured)
        except (binascii.Error, ValueError) as error:
            raise CredentialStoreConfigurationError(
                f"{MASTER_KEY_ENV} must be URL-safe Base64"
            ) from error
        if len(key) != KEY_BYTES:
            raise CredentialStoreConfigurationError(
                f"{MASTER_KEY_ENV} must decode to exactly {KEY_BYTES} bytes"
            )
        return key

    @staticmethod
    def _ensure_directory(store_dir: Path) -> None:
        store_dir.mkdir(parents=True, exist_ok=True)
        # POSIX target permission; on a platform without chmod semantics this is simply a no-op.
        os.chmod(store_dir, _STORE_DIRECTORY_MODE)

    @staticmethod
    def generate_master_key() -> str:
        """Return a fresh master key, URL-safe Base64 encoded, for an operator to store safely."""
        return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode("ascii")

    # ----------------------------------------------------------------------------------------
    # write path
    # ----------------------------------------------------------------------------------------

    def create(
        self,
        kind: ManagedSecretKind,
        plaintext: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ManagedSecretInfo:
        """Encrypt one secret under a new identifier and return its public view.

        The kind is recorded in the document and authenticated as AAD, so it cannot be altered
        without invalidating the ciphertext.
        """
        secret_id = f"{SECRET_ID_PREFIX}{uuid.uuid4().hex}"
        created_at = datetime.now(UTC)
        info = ManagedSecretInfo(
            id=secret_id,
            kind=kind,
            created_at=created_at,
            metadata=dict(metadata or {}),
        )
        # One nonce per document, used for the encryption it is written next to. Reuse would be
        # fatal for GCM, so it is drawn fresh here and never derived from anything.
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(self._master_key).encrypt(
            nonce, plaintext.encode("utf-8"), self._aad(secret_id, kind)
        )
        document = {
            "version": DOCUMENT_VERSION,
            "id": secret_id,
            "kind": kind.value,
            "algorithm": ALGORITHM,
            "nonce": _encode(nonce),
            "ciphertext": _encode(ciphertext),
            "created_at": created_at.isoformat(),
            "metadata": dict(info.metadata),
        }
        self._write_document(secret_id, document)
        return info

    def _aad(self, secret_id: str, kind: ManagedSecretKind) -> bytes:
        return _AAD_TEMPLATE.format(secret_id=secret_id, kind=kind.value).encode("utf-8")

    def _write_document(self, secret_id: str, document: dict[str, Any]) -> None:
        """Write one document atomically, so a reader never sees a partial secret."""
        self._ensure_directory(self.store_dir)
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{secret_id}.", suffix=".tmp", dir=self.store_dir
        )
        try:
            os.chmod(temporary, _SECRET_FILE_MODE)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path(secret_id))
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    # ----------------------------------------------------------------------------------------
    # read path
    # ----------------------------------------------------------------------------------------

    def resolve(self, secret_id: str) -> str:
        """Decrypt and return one secret. Fails closed on anything but an authentic document."""
        document = self._read_document(secret_id)
        kind = self._document_kind(document, secret_id)
        ciphertext = _decode(document.get("ciphertext"), "ciphertext")
        nonce = _decode(document.get("nonce"), "nonce", expected_length=NONCE_BYTES)
        try:
            plaintext = AESGCM(self._master_key).decrypt(
                nonce, ciphertext, self._aad(secret_id, kind)
            )
        except InvalidTag as error:
            # Also what a wrong master key looks like. The two are deliberately indistinguishable.
            raise ManagedSecretIntegrityError(
                f"managed secret {secret_id} could not be authenticated"
            ) from error
        return plaintext.decode("utf-8")

    def inspect(self, secret_id: str) -> ManagedSecretInfo:
        """Return the public view of one secret without decrypting it."""
        document = self._read_document(secret_id)
        kind = self._document_kind(document, secret_id)
        created_at = document.get("created_at")
        if not isinstance(created_at, str):
            raise ManagedSecretIntegrityError(
                f"managed secret {secret_id} has no usable creation time"
            )
        try:
            parsed = datetime.fromisoformat(created_at)
        except ValueError as error:
            raise ManagedSecretIntegrityError(
                f"managed secret {secret_id} has an unusable creation time"
            ) from error
        metadata = document.get("metadata")
        return ManagedSecretInfo(
            id=secret_id,
            kind=kind,
            created_at=parsed,
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def exists(self, secret_id: str) -> bool:
        """Report whether a well-formed identifier names a stored secret."""
        if not _SECRET_ID_PATTERN.match(secret_id):
            return False
        return self._path(secret_id).is_file()

    def delete(self, secret_id: str) -> None:
        """Remove exactly one stored secret document."""
        path = self._path(secret_id)
        try:
            path.unlink()
        except FileNotFoundError as error:
            raise ManagedSecretNotFoundError(
                f"managed secret {secret_id} does not exist"
            ) from error

    # ----------------------------------------------------------------------------------------
    # internals
    # ----------------------------------------------------------------------------------------

    def _path(self, secret_id: str) -> Path:
        """Map an identifier to a file, refusing anything that is not a managed secret id.

        The filename comes from the validated identifier alone, so a caller cannot express a
        directory, an absolute path or a traversal. A rejected identifier is reported exactly like a
        missing one and is deliberately not echoed back, so the error is not an oracle for probing
        the store directory.
        """
        if not _SECRET_ID_PATTERN.match(secret_id):
            raise ManagedSecretNotFoundError("managed secret does not exist")
        return self.store_dir / f"{secret_id}.json"

    def _read_document(self, secret_id: str) -> dict[str, Any]:
        path = self._path(secret_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as error:
            raise ManagedSecretNotFoundError(
                f"managed secret {secret_id} does not exist"
            ) from error
        except OSError as error:
            raise ManagedSecretIntegrityError(
                f"managed secret {secret_id} could not be read"
            ) from error
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManagedSecretIntegrityError(
                f"managed secret {secret_id} is not a readable document"
            ) from error
        if not isinstance(document, dict):
            raise ManagedSecretIntegrityError(
                f"managed secret {secret_id} is not a readable document"
            )
        if document.get("version") != DOCUMENT_VERSION:
            raise ManagedSecretIntegrityError(
                f"managed secret {secret_id} has an unsupported format version"
            )
        if document.get("algorithm") != ALGORITHM:
            raise ManagedSecretIntegrityError(
                f"managed secret {secret_id} has an unsupported algorithm"
            )
        if document.get("id") != secret_id:
            raise ManagedSecretIntegrityError(
                f"managed secret {secret_id} does not identify itself consistently"
            )
        return document

    @staticmethod
    def _document_kind(document: dict[str, Any], secret_id: str) -> ManagedSecretKind:
        try:
            return ManagedSecretKind(document.get("kind"))
        except ValueError as error:
            raise ManagedSecretIntegrityError(
                f"managed secret {secret_id} has an unsupported kind"
            ) from error


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: Any, field: str, *, expected_length: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise ManagedSecretIntegrityError(f"managed secret has no usable {field}")
    try:
        decoded = base64.urlsafe_b64decode(value)
    except (binascii.Error, ValueError) as error:
        raise ManagedSecretIntegrityError(f"managed secret has an unusable {field}") from error
    if expected_length is not None and len(decoded) != expected_length:
        raise ManagedSecretIntegrityError(f"managed secret has an unusable {field}")
    if not decoded:
        raise ManagedSecretIntegrityError(f"managed secret has an unusable {field}")
    return decoded
