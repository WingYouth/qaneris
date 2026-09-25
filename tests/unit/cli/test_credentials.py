"""CLI credential and certificate commands (RS-CLI-02).

These tests lock the interface contract: the command wiring, the one Application Service call per
command, the human and JSON output shapes, stdout purity, the stable product error codes and exit
codes - and, above all, the rule the whole module exists for. A secret must never reach argv, a
response, a log line or an exception.

Secrets arrive only through a hidden prompt or an explicit stdin read, so the tests exercise those
two paths directly and assert that the value they carry never appears in any output.
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from qaneris.application.service import QanerisService
from qaneris.cli import credentials as credential_cli
from qaneris.cli import main as cli
from qaneris.common.errors import (
    CredentialKindMismatchError,
    ManagedSecretNotFoundError,
)
from qaneris.contracts.credentials import (
    CREDENTIAL_FILE_MAX_BYTES,
    ManagedSecretInfo,
    ManagedSecretKind,
)

CLI_UNIQUE_PASSWORD_MARKER = "CLI_UNIQUE_PASSWORD_MARKER"
CLI_UNIQUE_TOKEN_MARKER = "CLI_UNIQUE_TOKEN_MARKER"
CLI_PRIVATE_KEY_PASSWORD_MARKER = "CLI_PRIVATE_KEY_PASSWORD_MARKER"

SECRET_ID = "sec_" + "a" * 32
OTHER_SECRET_ID = "sec_" + "b" * 32
CREATED_AT = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
#: What ``ManagedCredentialPublic.model_dump(mode="json")`` produces for ``CREATED_AT``.
CREATED_AT_JSON = "2026-01-01T00:00:00Z"


# --------------------------------------------------------------------------------------------
# certificate material
# --------------------------------------------------------------------------------------------


def certificate(*, ca: bool, key: rsa.RSAPrivateKey | None = None) -> x509.Certificate:
    issuer = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Qaneris Test CA" if ca else "client")]
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(issuer.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if not ca:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
    return builder.sign(issuer, hashes.SHA256())


def write_certificate(path: Path, *, ca: bool, der: bool = False) -> Path:
    encoding = serialization.Encoding.DER if der else serialization.Encoding.PEM
    path.write_bytes(certificate(ca=ca).public_bytes(encoding))
    return path


def write_key(path: Path, *, password: str | None = None, der: bool = False) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encryption = (
        serialization.BestAvailableEncryption(password.encode())
        if password
        else serialization.NoEncryption()
    )
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.DER if der else serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            encryption,
        )
    )
    return path


def write_pair(directory: Path, *, password: str | None = None) -> tuple[Path, Path]:
    """A matching client certificate and its own key, so a pair can be imported successfully."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate_path = directory / "client.crt"
    certificate_path.write_bytes(
        certificate(ca=False, key=key).public_bytes(serialization.Encoding.PEM)
    )
    key_path = directory / "client.key"
    encryption = (
        serialization.BestAvailableEncryption(password.encode())
        if password
        else serialization.NoEncryption()
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
        )
    )
    return certificate_path, key_path


# --------------------------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------------------------


def installed(monkeypatch: pytest.MonkeyPatch, service: Any) -> Mock:
    """Install a service double at the constructor the CLI actually calls."""
    constructor = Mock(return_value=service)
    monkeypatch.setattr(credential_cli, "application_service", constructor)
    return constructor


def serving(monkeypatch: pytest.MonkeyPatch, **methods: Any) -> Any:
    service = Mock(spec=QanerisService, **methods)
    installed(monkeypatch, service)
    return service


def info(kind: ManagedSecretKind, secret_id: str = SECRET_ID) -> ManagedSecretInfo:
    return ManagedSecretInfo(id=secret_id, kind=kind, created_at=CREATED_AT, metadata={})


def run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    try:
        code = cli.main(argv)
    except SystemExit as error:  # argparse usage errors
        code = error.code if isinstance(error.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend stdin is an interactive terminal, so the prompt path is reachable."""
    monkeypatch.setattr(credential_cli.sys.stdin, "isatty", lambda: True)


@pytest.fixture
def piped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend stdin is a pipe, so an implicit read must be refused."""
    monkeypatch.setattr(credential_cli.sys.stdin, "isatty", lambda: False)


# --------------------------------------------------------------------------------------------
# credential add
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["password", "token", "api_key", "client_private_key_password"]
)
def test_credential_add_accepts_every_text_kind(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tty: None,
    kind: str,
) -> None:
    creating = Mock(return_value=info(ManagedSecretKind(kind)))
    serving(monkeypatch, create_managed_secret=creating)
    monkeypatch.setattr(credential_cli.getpass, "getpass", lambda *a, **k: CLI_UNIQUE_PASSWORD_MARKER)

    code, stdout, stderr = run(capsys, ["credential", "add", "--kind", kind])

    assert code == 0
    assert stderr == ""
    assert creating.call_args.args == (ManagedSecretKind(kind), CLI_UNIQUE_PASSWORD_MARKER)
    assert "[PASS] Credential created" in stdout
    assert f"Secret ID: {SECRET_ID}" in stdout
    assert f"Kind: {kind}" in stdout
    assert CLI_UNIQUE_PASSWORD_MARKER not in stdout


@pytest.mark.parametrize(
    "kind", ["ca_certificate", "client_certificate", "client_private_key", "nonsense"]
)
def test_credential_add_refuses_a_non_text_kind(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Certificates have their own file commands; ``--kind`` must not become a way around them."""
    creating = Mock()
    serving(monkeypatch, create_managed_secret=creating)

    code, _, stderr = run(capsys, ["credential", "add", "--kind", kind])

    assert code == 2
    assert creating.call_count == 0
    assert "usage:" in stderr or "invalid" in stderr


def test_credential_add_json_is_the_shared_public_projection(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tty: None
) -> None:
    serving(monkeypatch, create_managed_secret=Mock(return_value=info(ManagedSecretKind.TOKEN)))
    monkeypatch.setattr(credential_cli.getpass, "getpass", lambda *a, **k: CLI_UNIQUE_TOKEN_MARKER)

    code, stdout, stderr = run(capsys, ["credential", "add", "--kind", "token", "--json"])

    assert code == 0
    assert stderr == ""
    document = json.loads(stdout)
    assert document["status"] == "completed"
    assert document["credential"] == {
        "secret_id": SECRET_ID,
        "kind": "token",
        "created_at": CREATED_AT_JSON,
        "metadata": {},
    }
    assert CLI_UNIQUE_TOKEN_MARKER not in stdout


# --------------------------------------------------------------------------------------------
# secret input mechanisms
# --------------------------------------------------------------------------------------------


def test_interactive_add_prompts_with_getpass_exactly_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tty: None
) -> None:
    serving(monkeypatch, create_managed_secret=Mock(return_value=info(ManagedSecretKind.PASSWORD)))
    prompted = Mock(return_value=CLI_UNIQUE_PASSWORD_MARKER)
    monkeypatch.setattr(credential_cli.getpass, "getpass", prompted)

    run(capsys, ["credential", "add", "--kind", "password"])

    assert prompted.call_count == 1
    # The prompt is a terminal interaction, so it is written to stderr and never to stdout.
    assert prompted.call_args.kwargs.get("stream") is credential_cli.sys.stderr


def test_interactive_add_never_echoes_the_secret(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tty: None
) -> None:
    serving(monkeypatch, create_managed_secret=Mock(return_value=info(ManagedSecretKind.PASSWORD)))
    monkeypatch.setattr(credential_cli.getpass, "getpass", lambda *a, **k: CLI_UNIQUE_PASSWORD_MARKER)

    code, stdout, stderr = run(capsys, ["credential", "add", "--kind", "password"])

    assert code == 0
    assert CLI_UNIQUE_PASSWORD_MARKER not in stdout
    assert CLI_UNIQUE_PASSWORD_MARKER not in stderr


def test_stdin_add_does_not_prompt(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    creating = Mock(return_value=info(ManagedSecretKind.PASSWORD))
    serving(monkeypatch, create_managed_secret=creating)
    prompted = Mock(side_effect=AssertionError("must not prompt with --stdin"))
    monkeypatch.setattr(credential_cli.getpass, "getpass", prompted)
    monkeypatch.setattr("sys.stdin", _pipe(CLI_UNIQUE_PASSWORD_MARKER))

    code, stdout, _ = run(capsys, ["credential", "add", "--kind", "password", "--stdin"])

    assert code == 0
    assert prompted.call_count == 0
    assert creating.call_args.args == (ManagedSecretKind.PASSWORD, CLI_UNIQUE_PASSWORD_MARKER)
    assert CLI_UNIQUE_PASSWORD_MARKER not in stdout


def test_a_pipe_without_stdin_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, piped: None
) -> None:
    """Reading a pipe implicitly would let a redirected file be consumed as a password."""
    creating = Mock()
    serving(monkeypatch, create_managed_secret=creating)

    code, _, stderr = run(capsys, ["credential", "add", "--kind", "password"])

    assert code == 2
    assert creating.call_count == 0
    assert "--stdin" in stderr


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" secret ", " secret "),
        ("secret\n", "secret"),
        ("secret\r\n", "secret"),
        ("sec\nret\n", "sec\nret"),
        ("secret", "secret"),
    ],
)
def test_stdin_removes_at_most_one_final_line_ending(raw: str, expected: str) -> None:
    """Surrounding whitespace is part of a password; only the terminator a pipe adds is dropped."""
    import io

    original = credential_cli.sys.stdin
    credential_cli.sys.stdin = io.StringIO(raw)
    try:
        assert credential_cli.read_stdin_secret() == expected
    finally:
        credential_cli.sys.stdin = original


def test_an_empty_stdin_secret_is_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    creating = Mock()
    serving(monkeypatch, create_managed_secret=creating)
    monkeypatch.setattr("sys.stdin", _pipe(""))

    code, _, stderr = run(capsys, ["credential", "add", "--kind", "password", "--stdin"])

    assert code == 2
    assert creating.call_count == 0
    assert "empty" in stderr


def test_an_oversized_stdin_secret_is_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    creating = Mock()
    serving(monkeypatch, create_managed_secret=creating)
    monkeypatch.setattr("sys.stdin", _pipe("x" * 65_537))

    code, _, stderr = run(capsys, ["credential", "add", "--kind", "password", "--stdin"])

    assert code == 2
    assert creating.call_count == 0
    assert "65536" in stderr


def test_an_empty_prompted_secret_is_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tty: None
) -> None:
    creating = Mock()
    serving(monkeypatch, create_managed_secret=creating)
    monkeypatch.setattr(credential_cli.getpass, "getpass", lambda *a, **k: "")

    code, _, _ = run(capsys, ["credential", "add", "--kind", "password"])

    assert code == 2
    assert creating.call_count == 0


def _pipe(text: str) -> Any:
    """A stdin double that is a readable non-TTY."""
    import io

    stream = io.StringIO(text)
    stream.isatty = lambda: False  # type: ignore[method-assign]
    return stream


# --------------------------------------------------------------------------------------------
# credential show / delete
# --------------------------------------------------------------------------------------------


def test_credential_show_calls_inspect_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    inspecting = Mock(return_value=info(ManagedSecretKind.API_KEY))
    serving(monkeypatch, inspect_managed_secret=inspecting)

    code, stdout, stderr = run(capsys, ["credential", "show", SECRET_ID])

    assert code == 0
    assert stderr == ""
    assert inspecting.call_args.args == (SECRET_ID,)
    assert f"Secret ID: {SECRET_ID}" in stdout
    assert "Kind: api_key" in stdout


def test_credential_show_json_is_public_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(monkeypatch, inspect_managed_secret=Mock(return_value=info(ManagedSecretKind.PASSWORD)))

    code, stdout, _ = run(capsys, ["credential", "show", SECRET_ID, "--json"])

    assert code == 0
    assert json.loads(stdout) == {
        "secret_id": SECRET_ID,
        "kind": "password",
        "created_at": CREATED_AT_JSON,
        "metadata": {},
    }


def test_credential_show_unknown_is_exit_one_with_stable_code(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(monkeypatch, inspect_managed_secret=Mock(side_effect=ManagedSecretNotFoundError("no")))

    code, stdout, _ = run(capsys, ["credential", "show", SECRET_ID, "--json"])

    assert code == 1
    document = json.loads(stdout)
    assert document["status"] == "operation_failed"
    assert document["error"]["code"] == "managed_secret_not_found"


def test_credential_delete_calls_delete_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    deleting = Mock()
    serving(monkeypatch, delete_managed_secret=deleting)

    code, stdout, stderr = run(capsys, ["credential", "delete", SECRET_ID])

    assert code == 0
    assert stderr == ""
    assert deleting.call_args.args == (SECRET_ID,)
    assert "[PASS] Credential deleted" in stdout
    assert f"Secret ID: {SECRET_ID}" in stdout


def test_credential_delete_json_reports_the_id_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(monkeypatch, delete_managed_secret=Mock())

    code, stdout, _ = run(capsys, ["credential", "delete", SECRET_ID, "--json"])

    assert code == 0
    assert json.loads(stdout) == {"status": "completed", "secret_id": SECRET_ID}


def test_credential_delete_in_use_exits_one(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from qaneris.common.errors import ManagedSecretInUseError

    serving(monkeypatch, delete_managed_secret=Mock(side_effect=ManagedSecretInUseError("used")))

    code, stdout, _ = run(capsys, ["credential", "delete", SECRET_ID, "--json"])

    assert code == 1
    assert json.loads(stdout)["error"]["code"] == "managed_secret_in_use"


# --------------------------------------------------------------------------------------------
# certificate add-ca
# --------------------------------------------------------------------------------------------


def test_add_ca_reads_the_file_and_calls_the_service_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creating = Mock(return_value=info(ManagedSecretKind.CA_CERTIFICATE))
    serving(monkeypatch, create_managed_secret=creating)
    path = write_certificate(tmp_path / "ca.pem", ca=True)

    code, stdout, stderr = run(capsys, ["certificate", "add-ca", str(path)])

    assert code == 0
    assert stderr == ""
    kind, data = creating.call_args.args
    assert kind is ManagedSecretKind.CA_CERTIFICATE
    assert isinstance(data, bytes)
    assert data.startswith(b"-----BEGIN CERTIFICATE-----")
    assert "Kind: ca_certificate" in stdout


def test_add_ca_accepts_der(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DER is bytes, not text: the file must reach the service without a UTF-8 decode."""
    creating = Mock(return_value=info(ManagedSecretKind.CA_CERTIFICATE))
    serving(monkeypatch, create_managed_secret=creating)
    path = write_certificate(tmp_path / "ca.der", ca=True, der=True)

    code, _, _ = run(capsys, ["certificate", "add-ca", str(path)])

    assert code == 0
    assert not creating.call_args.args[1].startswith(b"-----BEGIN")


def test_add_ca_rejects_a_file_over_the_shared_limit(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creating = Mock()
    serving(monkeypatch, create_managed_secret=creating)
    path = tmp_path / "big.pem"
    path.write_bytes(b"x" * (CREDENTIAL_FILE_MAX_BYTES + 1))

    code, _, _ = run(capsys, ["certificate", "add-ca", str(path), "--json"])

    assert code == 1
    assert creating.call_count == 0


def test_add_ca_reports_a_missing_file_as_a_usage_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creating = Mock()
    serving(monkeypatch, create_managed_secret=creating)

    code, _, _ = run(capsys, ["certificate", "add-ca", str(tmp_path / "absent.pem")])

    assert code == 2
    assert creating.call_count == 0


def test_add_client_calls_create_client_identity_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creating = Mock(
        return_value=(
            info(ManagedSecretKind.CLIENT_CERTIFICATE),
            info(ManagedSecretKind.CLIENT_PRIVATE_KEY, OTHER_SECRET_ID),
        )
    )
    serving(monkeypatch, create_managed_client_identity=creating)
    certificate_path, key_path = write_pair(tmp_path)

    code, stdout, stderr = run(
        capsys,
        [
            "certificate",
            "add-client",
            "--certificate",
            str(certificate_path),
            "--private-key",
            str(key_path),
        ],
    )

    assert code == 0
    assert stderr == ""
    arguments = creating.call_args
    assert arguments.args[0].startswith(b"-----BEGIN CERTIFICATE-----")
    assert arguments.args[1].startswith(b"-----BEGIN PRIVATE KEY-----")
    assert arguments.kwargs["private_key_password"] is None
    assert f"Certificate Secret ID: {SECRET_ID}" in stdout
    assert f"Private Key Secret ID: {OTHER_SECRET_ID}" in stdout


def test_add_client_without_a_password_option_passes_none(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI does not guess whether the key is encrypted; the service decides."""
    creating = Mock(
        return_value=(
            info(ManagedSecretKind.CLIENT_CERTIFICATE),
            info(ManagedSecretKind.CLIENT_PRIVATE_KEY, OTHER_SECRET_ID),
        )
    )
    serving(monkeypatch, create_managed_client_identity=creating)
    certificate_path, key_path = write_pair(tmp_path)

    run(
        capsys,
        [
            "certificate",
            "add-client",
            "--certificate",
            str(certificate_path),
            "--private-key",
            str(key_path),
        ],
    )

    assert creating.call_args.kwargs["private_key_password"] is None


def test_add_client_prompt_password_uses_getpass(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tty: None
) -> None:
    creating = Mock(
        return_value=(
            info(ManagedSecretKind.CLIENT_CERTIFICATE),
            info(ManagedSecretKind.CLIENT_PRIVATE_KEY, OTHER_SECRET_ID),
        )
    )
    serving(monkeypatch, create_managed_client_identity=creating)
    certificate_path, key_path = write_pair(tmp_path)
    prompted = Mock(return_value=CLI_PRIVATE_KEY_PASSWORD_MARKER)
    monkeypatch.setattr(credential_cli.getpass, "getpass", prompted)

    code, stdout, stderr = run(
        capsys,
        [
            "certificate",
            "add-client",
            "--certificate",
            str(certificate_path),
            "--private-key",
            str(key_path),
            "--private-key-password-prompt",
        ],
    )

    assert code == 0
    assert prompted.call_count == 1
    assert creating.call_args.kwargs["private_key_password"] == CLI_PRIVATE_KEY_PASSWORD_MARKER
    assert CLI_PRIVATE_KEY_PASSWORD_MARKER not in stdout
    assert CLI_PRIVATE_KEY_PASSWORD_MARKER not in stderr


def test_add_client_stdin_password_reads_the_pipe(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creating = Mock(
        return_value=(
            info(ManagedSecretKind.CLIENT_CERTIFICATE),
            info(ManagedSecretKind.CLIENT_PRIVATE_KEY, OTHER_SECRET_ID),
        )
    )
    serving(monkeypatch, create_managed_client_identity=creating)
    certificate_path, key_path = write_pair(tmp_path)
    prompted = Mock(side_effect=AssertionError("must not prompt"))
    monkeypatch.setattr(credential_cli.getpass, "getpass", prompted)
    monkeypatch.setattr("sys.stdin", _pipe(CLI_PRIVATE_KEY_PASSWORD_MARKER))

    code, stdout, _ = run(
        capsys,
        [
            "certificate",
            "add-client",
            "--certificate",
            str(certificate_path),
            "--private-key",
            str(key_path),
            "--private-key-password-stdin",
        ],
    )

    assert code == 0
    assert prompted.call_count == 0
    assert creating.call_args.kwargs["private_key_password"] == CLI_PRIVATE_KEY_PASSWORD_MARKER
    assert CLI_PRIVATE_KEY_PASSWORD_MARKER not in stdout


def test_the_two_password_sources_are_mutually_exclusive(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creating = Mock()
    serving(monkeypatch, create_managed_client_identity=creating)
    certificate_path, key_path = write_pair(tmp_path)

    code, _, stderr = run(
        capsys,
        [
            "certificate",
            "add-client",
            "--certificate",
            str(certificate_path),
            "--private-key",
            str(key_path),
            "--private-key-password-prompt",
            "--private-key-password-stdin",
        ],
    )

    assert code == 2
    assert creating.call_count == 0
    assert "not allowed with" in stderr


def test_add_client_json_reports_two_distinct_ids(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    serving(
        monkeypatch,
        create_managed_client_identity=Mock(
            return_value=(
                info(ManagedSecretKind.CLIENT_CERTIFICATE),
                info(ManagedSecretKind.CLIENT_PRIVATE_KEY, OTHER_SECRET_ID),
            )
        ),
    )
    certificate_path, key_path = write_pair(tmp_path)

    code, stdout, _ = run(
        capsys,
        [
            "certificate",
            "add-client",
            "--certificate",
            str(certificate_path),
            "--private-key",
            str(key_path),
            "--json",
        ],
    )

    assert code == 0
    document = json.loads(stdout)
    assert document["status"] == "completed"
    assert document["client_certificate"]["secret_id"] == SECRET_ID
    assert document["client_private_key"]["secret_id"] == OTHER_SECRET_ID
    assert document["client_certificate"]["secret_id"] != document["client_private_key"]["secret_id"]
    assert set(document) == {"status", "client_certificate", "client_private_key"}


# --------------------------------------------------------------------------------------------
# certificate inspect / delete and the kind guard
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        ManagedSecretKind.CA_CERTIFICATE,
        ManagedSecretKind.CLIENT_CERTIFICATE,
        ManagedSecretKind.CLIENT_PRIVATE_KEY,
    ],
)
def test_certificate_inspect_accepts_every_certificate_kind(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, kind: ManagedSecretKind
) -> None:
    serving(monkeypatch, inspect_managed_secret=Mock(return_value=info(kind)))

    code, stdout, _ = run(capsys, ["certificate", "inspect", SECRET_ID, "--json"])

    assert code == 0
    assert json.loads(stdout)["kind"] == kind.value


@pytest.mark.parametrize(
    "kind", [ManagedSecretKind.PASSWORD, ManagedSecretKind.TOKEN, ManagedSecretKind.API_KEY]
)
def test_certificate_inspect_refuses_a_text_secret(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, kind: ManagedSecretKind
) -> None:
    """``certificate`` commands address certificate material only - not a second untyped surface."""
    serving(monkeypatch, inspect_managed_secret=Mock(return_value=info(kind)))

    code, stdout, _ = run(capsys, ["certificate", "inspect", SECRET_ID, "--json"])

    assert code == 1
    assert json.loads(stdout)["error"]["code"] == "credential_kind_mismatch"


@pytest.mark.parametrize(
    "kind", [ManagedSecretKind.PASSWORD, ManagedSecretKind.TOKEN, ManagedSecretKind.API_KEY]
)
def test_certificate_delete_refuses_a_text_secret(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, kind: ManagedSecretKind
) -> None:
    deleting = Mock()
    serving(
        monkeypatch,
        inspect_managed_secret=Mock(return_value=info(kind)),
        delete_managed_secret=deleting,
    )

    code, stdout, _ = run(capsys, ["certificate", "delete", SECRET_ID, "--json"])

    assert code == 1
    assert deleting.call_count == 0
    assert json.loads(stdout)["error"]["code"] == "credential_kind_mismatch"


def test_certificate_delete_removes_a_certificate(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    deleting = Mock()
    serving(
        monkeypatch,
        inspect_managed_secret=Mock(return_value=info(ManagedSecretKind.CA_CERTIFICATE)),
        delete_managed_secret=deleting,
    )

    code, stdout, _ = run(capsys, ["certificate", "delete", SECRET_ID])

    assert code == 0
    assert deleting.call_args.args == (SECRET_ID,)
    assert "[PASS] Credential deleted" in stdout


def test_kind_mismatch_is_a_real_error_type() -> None:
    """The refusal is a stable Application error, not a string the CLI happens to print."""
    error = CredentialKindMismatchError("wrong kind")

    assert error.code == "credential_kind_mismatch"


# --------------------------------------------------------------------------------------------
# argv security
# --------------------------------------------------------------------------------------------


def test_no_option_takes_a_secret_value() -> None:
    """A secret must never be passable as an argument: argv is world-readable and logged.

    ``--private-key`` is allowed to exist because it takes a *file path*, so the check is on the
    options that would carry material itself rather than on a substring of the parser source.
    """
    parser = cli.build_parser()
    forbidden = ("--password", "--token", "--api-key", "--secret", "--private-key-password", "--value")
    options = _all_option_strings(parser)

    for name in forbidden:
        assert name not in options, name
    # The file-path option is present, and it is a path - not key material.
    assert "--private-key" in options
    assert "--private-key-password-prompt" in options
    assert "--private-key-password-stdin" in options


def _all_option_strings(parser: Any) -> set[str]:
    found: set[str] = set()
    for action in parser._actions:
        found.update(action.option_strings)
        for choice in getattr(action, "choices", {}).values() if isinstance(
            getattr(action, "choices", None), dict
        ) else []:
            found.update(_all_option_strings(choice))
    return found


def test_the_command_module_does_not_read_a_secret_from_argv() -> None:
    """No ``args.<secret>`` lookup exists: secrets come only from getpass or stdin.

    ``args.secret_id`` is a legitimate lookup - it is an identifier, not material - so the check
    matches a whole attribute name rather than a prefix.
    """
    source = inspect.getsource(credential_cli)
    assert "getpass" in source
    assert "--stdin" in source
    # A password/token arriving as an attribute of the parsed namespace would be an argv leak.
    for attribute in ("password", "token", "api_key", "secret", "value"):
        assert not re.search(rf"args\.{attribute}\b", source), attribute
    assert re.search(r"args\.secret_id\b", source)


# --------------------------------------------------------------------------------------------
# boundary hygiene
# --------------------------------------------------------------------------------------------


def test_the_cli_reaches_nothing_but_the_application_service() -> None:
    source = inspect.getsource(credential_cli)
    for name in (
        "ManagedCredentialStore",
        "CertificateValidator",
        "SecretResolver",
        "Catalog(",
        "QanerisService(",
    ):
        assert name not in source, name
    for method in (
        "create_managed_secret",
        "create_managed_client_identity",
        "inspect_managed_secret",
        "delete_managed_secret",
    ):
        assert method in source


def test_the_cli_reuses_the_shared_public_projection() -> None:
    """No second credential model: the CLI projects through ``public_credential``."""
    source = inspect.getsource(credential_cli)
    assert "public_credential" in source
    assert "ManagedCredentialPublic" in source


# --------------------------------------------------------------------------------------------
# help and usage
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        ["credential", "--help"],
        ["credential", "add", "--help"],
        ["credential", "show", "--help"],
        ["credential", "delete", "--help"],
        ["certificate", "--help"],
        ["certificate", "add-ca", "--help"],
        ["certificate", "add-client", "--help"],
        ["certificate", "inspect", "--help"],
        ["certificate", "delete", "--help"],
    ],
)
def test_help_is_available_for_every_command(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(arguments)

    assert raised.value.code == 0
    assert "usage:" in capsys.readouterr().out
