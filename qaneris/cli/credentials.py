"""Credential and certificate product commands (RS-CLI-02).

The CLI is the second upload boundary after the HTTP API, and it faces the same problem from the
other direction: a shell hands its arguments to the whole machine. ``argv`` is readable from the
process list and is written to shell history, so a secret must never travel through it. This module
therefore has exactly two ways to receive secret material - a hidden terminal prompt and an explicit
stdin read - and no option anywhere that takes a secret as a value.

What each command does is deliberately small: validate the request shape, read the material safely,
call one ``QanerisService`` method, and project the result through the same ``ManagedCredentialPublic``
contract the HTTP API uses. It never touches the credential store, the certificate validator, the
catalog or the secret resolver.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from qaneris.cli.render import operation_error_detail
from qaneris.cli.source import application_service
from qaneris.common.errors import (
    CredentialKindMismatchError,
    CredentialUploadTooLargeError,
)
from qaneris.contracts.credentials import (
    CERTIFICATE_KINDS,
    CREDENTIAL_FILE_MAX_BYTES,
    TEXT_SECRET_KINDS,
    TEXT_SECRET_MAX_CHARS,
    ManagedCredentialPublic,
    ManagedSecretInfo,
    ManagedSecretKind,
    public_credential,
)

CREDENTIAL_COMMAND = "credential"
CERTIFICATE_COMMAND = "certificate"

ADD_COMMAND = "add"
SHOW_COMMAND = "show"
DELETE_COMMAND = "delete"

ADD_CA_COMMAND = "add-ca"
ADD_CLIENT_COMMAND = "add-client"
INSPECT_COMMAND = "inspect"

#: The text kinds ``credential add --kind`` accepts. Sorted for a stable, readable error message.
_TEXT_KINDS = tuple(sorted(kind.value for kind in TEXT_SECRET_KINDS))

#: Exit codes follow the existing CLI convention: 0 completed, 1 ran-and-failed, 2 usage/config.
_EXIT_OK = 0
_EXIT_FAILED = 1


# --------------------------------------------------------------------------------------------
# secret input
# --------------------------------------------------------------------------------------------


class CredentialInputError(ValueError):
    """Secret input could not be read for a reason that makes the invocation a usage error."""


def read_stdin_secret(*, limit: int = TEXT_SECRET_MAX_CHARS) -> str:
    """Read one secret from stdin, keeping every character but the last line terminator.

    A pipe adds a terminator that is not part of the secret, so at most one final ``\\n`` or
    ``\\r\\n`` is removed. Nothing else is touched: ``strip()`` would silently destroy a password
    whose leading or trailing spaces are meaningful, and a secret may legitimately contain further
    newlines.
    """
    text = sys.stdin.read(limit + 2)
    if len(text) > limit:
        raise CredentialInputError(
            f"secret from stdin exceeds {limit} characters"
        )
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


def read_prompted_secret(prompt: str) -> str:
    """Read one secret from a terminal without echoing it.

    ``getpass`` reads from the controlling terminal and turns echo off; ``input`` would print the
    secret back to the screen and, on a redirected stdin, would read the pipe instead. The prompt
    itself is written to stderr so an interactive ``--json`` run still leaves stdout holding exactly
    one JSON document.
    """
    return getpass.getpass(prompt, stream=sys.stderr)


def resolve_text_secret(args: argparse.Namespace) -> str:
    """Obtain one text secret, from an explicit pipe or from a hidden prompt.

    Reading a pipe implicitly is refused: a script must say ``--stdin``, so an invocation that
    accidentally has its stdin redirected fails loudly instead of consuming a file as a password.
    """
    if args.stdin:
        value = read_stdin_secret()
    elif sys.stdin.isatty():
        value = read_prompted_secret(f"{args.kind} value: ")
    else:
        raise CredentialInputError(
            "stdin is not a terminal; pass --stdin to read the secret from a pipe explicitly"
        )
    if not value:
        raise CredentialInputError("secret must not be empty")
    return value


def resolve_private_key_password(args: argparse.Namespace) -> str | None:
    """Obtain the optional private key decrypt password, or ``None`` when none was asked for.

    Not supplying a password is not an error and does not imply the key is unencrypted: the
    Application Service decides whether the key opens, and an encrypted key without a password is
    refused as ``certificate_validation_failed`` so the caller can retry with ``--private-key-password-prompt``
    or ``--private-key-password-stdin``.
    """
    if getattr(args, "private_key_password_stdin", False):
        value = read_stdin_secret()
    elif getattr(args, "private_key_password_prompt", False):
        value = read_prompted_secret("private key password: ")
    else:
        return None
    if not value:
        raise CredentialInputError("private key password must not be empty")
    return value


# --------------------------------------------------------------------------------------------
# certificate file input
# --------------------------------------------------------------------------------------------


def read_certificate_file(path: Path, *, limit: int = CREDENTIAL_FILE_MAX_BYTES) -> bytes:
    """Read one certificate or key file as bytes, bounded by the shared product limit.

    The size is checked before the read, so an oversized file is refused without being loaded, and
    the content is returned as bytes because DER - unlike PEM - is not text. No staging file is
    created: the material goes from the read into the Application Service call and no further.
    """
    resolved = path.expanduser()
    try:
        stat = resolved.stat()
    except OSError as error:
        raise CredentialInputError(f"cannot read file: {path}") from error
    if not resolved.is_file():
        raise CredentialInputError(f"not a regular file: {path}")
    if stat.st_size > limit:
        raise CredentialUploadTooLargeError(
            f"file exceeds the credential size limit ({limit} bytes)"
        )
    try:
        return resolved.read_bytes()
    except OSError as error:
        raise CredentialInputError(f"cannot read file: {path}") from error


# --------------------------------------------------------------------------------------------
# projection
# --------------------------------------------------------------------------------------------


def credential_payload(credential: ManagedCredentialPublic) -> dict[str, Any]:
    """The product projection of one managed secret, shared with the HTTP boundary's contract."""
    return credential.model_dump(mode="json")


def _call(operation: Callable[[], Any]) -> Any:
    """Run one Application Service call with anything it prints diverted to stderr.

    The CLI emits its own report after this returns, so a ``--json`` document can never be
    contaminated by incidental provider, driver or debug output. A credential operation is quieter
    than a scan, but the rule is the same for every product command and does not depend on that
    staying true.
    """
    with redirect_stdout(sys.stderr):
        return operation()


def _public(info: ManagedSecretInfo) -> ManagedCredentialPublic:
    return public_credential(info)


def print_credential_created(
    credential: ManagedCredentialPublic, *, json_output: bool
) -> None:
    if json_output:
        print(
            json.dumps(
                {"status": "completed", "credential": credential_payload(credential)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print("[PASS] Credential created")
    print(f"Secret ID: {credential.secret_id}")
    print(f"Kind: {credential.kind.value}")
    print(f"Created: {credential.created_at.isoformat()}")


def print_credential(credential: ManagedCredentialPublic, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(credential_payload(credential), ensure_ascii=False, indent=2))
        return
    print(f"Secret ID: {credential.secret_id}")
    print(f"Kind: {credential.kind.value}")
    print(f"Created: {credential.created_at.isoformat()}")


def print_credential_deleted(secret_id: str, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {"status": "completed", "secret_id": secret_id},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print("[PASS] Credential deleted")
    print(f"Secret ID: {secret_id}")


def print_client_identity(
    certificate: ManagedCredentialPublic,
    private_key: ManagedCredentialPublic,
    *,
    json_output: bool,
) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "status": "completed",
                    "client_certificate": credential_payload(certificate),
                    "client_private_key": credential_payload(private_key),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print("[PASS] Client identity created")
    print(f"Certificate Secret ID: {certificate.secret_id}")
    print(f"Private Key Secret ID: {private_key.secret_id}")


def report_credential_failure(error: Exception, *, json_output: bool) -> None:
    """Report a failed credential operation, keeping its stable code and never a traceback."""
    detail = operation_error_detail(error)
    if json_output:
        print(
            json.dumps(
                {"status": "operation_failed", "error": detail}, ensure_ascii=False, indent=2
            )
        )
        return
    print(f"Credential failed: {detail['message']}", file=sys.stderr)


# --------------------------------------------------------------------------------------------
# certificate kind guard
# --------------------------------------------------------------------------------------------


def require_certificate_kind(credential: ManagedCredentialPublic) -> None:
    """Refuse a certificate command aimed at a text secret.

    A ``sec_`` identifier says nothing about what the secret holds, so the kind is read back first.
    Without this the certificate commands would be a second, untyped way to read or delete a
    password, and the "certificate" in the command name would be a false promise.
    """
    if credential.kind not in CERTIFICATE_KINDS:
        raise CredentialKindMismatchError(
            f"secret is a {credential.kind.value}, not a certificate or private key"
        )


# --------------------------------------------------------------------------------------------
# command registration
# --------------------------------------------------------------------------------------------


def _kind(value: str) -> str:
    if value not in _TEXT_KINDS:
        raise argparse.ArgumentTypeError(
            f"must be one of: {', '.join(_TEXT_KINDS)}"
        )
    return value


def _secret_id(value: str) -> str:
    secret = value.strip()
    if not secret:
        raise argparse.ArgumentTypeError("must not be empty")
    return secret


def _file_path(value: str) -> Path:
    return Path(value).expanduser()


def add_credential_commands(commands: argparse._SubParsersAction) -> None:
    credential = commands.add_parser(
        "credential", help="create, inspect and delete managed text credentials"
    )
    actions = credential.add_subparsers(dest="credential_command", required=True)

    add = actions.add_parser("add", help="create one managed text credential")
    add.add_argument(
        "--kind",
        required=True,
        type=_kind,
        help=f"one of: {', '.join(_TEXT_KINDS)}",
    )
    # The only way to supply a secret: a hidden prompt, or an explicit pipe. There is deliberately
    # no ``--value`` / ``--password`` / ``--token`` option to put a secret into argv.
    add.add_argument(
        "--stdin",
        action="store_true",
        help="read the secret from stdin instead of prompting",
    )
    add.add_argument("--json", action="store_true", dest="json_output")

    show = actions.add_parser("show", help="show one managed credential by secret id")
    show.add_argument("secret_id", metavar="SECRET_ID", type=_secret_id)
    show.add_argument("--json", action="store_true", dest="json_output")

    delete = actions.add_parser("delete", help="delete one managed credential by secret id")
    delete.add_argument("secret_id", metavar="SECRET_ID", type=_secret_id)
    delete.add_argument("--json", action="store_true", dest="json_output")


def add_certificate_commands(commands: argparse._SubParsersAction) -> None:
    certificate = commands.add_parser(
        "certificate", help="create, inspect and delete managed certificates and keys"
    )
    actions = certificate.add_subparsers(dest="certificate_command", required=True)

    add_ca = actions.add_parser("add-ca", help="import one CA certificate file")
    add_ca.add_argument("file", metavar="FILE", type=_file_path, help="a PEM or DER certificate")
    add_ca.add_argument("--json", action="store_true", dest="json_output")

    add_client = actions.add_parser(
        "add-client", help="import one mTLS client certificate and its private key"
    )
    add_client.add_argument(
        "--certificate", required=True, type=_file_path, metavar="FILE", help="client certificate"
    )
    add_client.add_argument(
        "--private-key", required=True, type=_file_path, metavar="FILE", help="client private key"
    )
    password_source = add_client.add_mutually_exclusive_group()
    password_source.add_argument(
        "--private-key-password-prompt",
        action="store_true",
        help="prompt for the private key decrypt password",
    )
    password_source.add_argument(
        "--private-key-password-stdin",
        action="store_true",
        help="read the private key decrypt password from stdin",
    )
    add_client.add_argument("--json", action="store_true", dest="json_output")

    inspect = actions.add_parser("inspect", help="show one certificate or private key by secret id")
    inspect.add_argument("secret_id", metavar="SECRET_ID", type=_secret_id)
    inspect.add_argument("--json", action="store_true", dest="json_output")

    delete = actions.add_parser("delete", help="delete one certificate or private key by secret id")
    delete.add_argument("secret_id", metavar="SECRET_ID", type=_secret_id)
    delete.add_argument("--json", action="store_true", dest="json_output")


# --------------------------------------------------------------------------------------------
# command execution
# --------------------------------------------------------------------------------------------


def run_credential_command(args: argparse.Namespace) -> int:
    """Run one ``credential`` subcommand and return its exit code."""
    command = args.credential_command
    json_output = bool(args.json_output)
    service = application_service()

    if command == ADD_COMMAND:
        kind = ManagedSecretKind(args.kind)
        value = resolve_text_secret(args)
        try:
            info = _call(lambda: service.create_managed_secret(kind, value))
        finally:
            # The secret is a local either way, but clearing it here keeps the plaintext out of
            # this frame for as long as the interpreter allows.
            value = ""
        print_credential_created(_public(info), json_output=json_output)
        return _EXIT_OK

    if command == SHOW_COMMAND:
        info = _call(lambda: service.inspect_managed_secret(args.secret_id))
        print_credential(_public(info), json_output=json_output)
        return _EXIT_OK

    _call(lambda: service.delete_managed_secret(args.secret_id))
    print_credential_deleted(args.secret_id, json_output=json_output)
    return _EXIT_OK


def run_certificate_command(args: argparse.Namespace) -> int:
    """Run one ``certificate`` subcommand and return its exit code."""
    command = args.certificate_command
    json_output = bool(args.json_output)
    service = application_service()

    if command == ADD_CA_COMMAND:
        data = read_certificate_file(args.file)
        info = _call(
            lambda: service.create_managed_secret(ManagedSecretKind.CA_CERTIFICATE, data)
        )
        print_credential_created(_public(info), json_output=json_output)
        return _EXIT_OK

    if command == ADD_CLIENT_COMMAND:
        certificate = read_certificate_file(args.certificate)
        private_key = read_certificate_file(args.private_key)
        password = resolve_private_key_password(args)
        try:
            stored_certificate, stored_key = _call(
                lambda: service.create_managed_client_identity(
                    certificate, private_key, private_key_password=password
                )
            )
        finally:
            password = None
        print_client_identity(
            _public(stored_certificate), _public(stored_key), json_output=json_output
        )
        return _EXIT_OK

    # ``inspect`` and ``delete`` address certificates only: the kind is read back and checked before
    # anything is printed or removed, so neither can become a second untyped credential surface.
    credential = _public(_call(lambda: service.inspect_managed_secret(args.secret_id)))
    require_certificate_kind(credential)
    if command == INSPECT_COMMAND:
        print_credential(credential, json_output=json_output)
        return _EXIT_OK
    _call(lambda: service.delete_managed_secret(args.secret_id))
    print_credential_deleted(args.secret_id, json_output=json_output)
    return _EXIT_OK
