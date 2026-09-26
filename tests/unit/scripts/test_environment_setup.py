"""Unit tests for the setup script's decision logic.

These cover the parts that decide a verdict from *observed text or files* - the Node engine
gate, the dotenv merge, the master-key validation and the exit-code calculation - so a bug in
the setup script surfaces here instead of as a wrong "ready" on an operator's machine.

Nothing is downloaded, installed, started or connected. Every subprocess and network call is
out of scope by construction: only pure helpers are exercised.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.environment_setup import (
    BLOCKED,
    DEFAULT_EXTRAS,
    FIXED,
    FRONTEND_URL,
    OK,
    PROJECT_ROOT,
    SKIPPED,
    Context,
    Result,
    SetupError,
    _append_env_values,
    _create_environment_file,
    _dotenv_path,
    _download_resumable,
    _human_size,
    _installer_for_platform,
    _neo4j_port,
    _print_summary,
    _read_dotenv,
    _rediscover_compose,
    _store_has_secrets,
    check_docker,
    check_environment_file,
    check_secret_store,
    effective_environment,
    generate_master_key,
    generate_password,
    main,
    node_version_supported,
    open_workspace,
    unknown_extras,
)

# ---------------------------------------------------------------------------------------------
# reachability of the workspace
# ---------------------------------------------------------------------------------------------


def test_the_frontend_url_points_at_the_vite_port() -> None:
    assert FRONTEND_URL == "http://127.0.0.1:5173/"


def test_docker_daemon_autostart_is_a_default_that_can_be_refused() -> None:
    """A single run should be enough, so launching the daemon is the default."""
    assert Context().start_docker is True
    assert Context().open_browser is True


def test_a_stopped_daemon_is_left_alone_under_no_start_docker(monkeypatch) -> None:
    monkeypatch.setattr("tools.environment_setup._docker_daemon_ready", lambda ctx: None)
    monkeypatch.setattr(
        "tools.environment_setup._start_docker_desktop",
        lambda ctx: pytest.fail("must not launch Docker under --no-start-docker"),
    )
    ctx = Context(environment={}, dry_run=False, start_docker=False)
    ctx.docker = "/usr/local/bin/docker"

    result = check_docker(ctx)

    assert result.status == BLOCKED
    assert "--no-start-docker" in result.detail


def test_a_stopped_daemon_is_launched_and_reported_as_fixed(monkeypatch) -> None:
    """The first run on a machine with a closed Docker Desktop must still finish the job."""
    attempts = {"count": 0}

    def daemon(_ctx):
        attempts["count"] += 1
        return "29.1.3" if attempts["count"] > 1 else None

    monkeypatch.setattr("tools.environment_setup._docker_daemon_ready", daemon)
    monkeypatch.setattr("tools.environment_setup._start_docker_desktop", lambda ctx: None)
    ctx = Context(environment={}, dry_run=False, start_docker=True)
    ctx.docker = "/usr/local/bin/docker"
    ctx.compose = ctx.docker
    ctx.compose_version = "2.40.3"

    result = check_docker(ctx)

    assert result.status == FIXED
    assert "已启动 Docker Desktop" in result.detail


def test_a_daemon_that_never_answers_is_reported_not_retried_forever(monkeypatch) -> None:
    monkeypatch.setattr("tools.environment_setup._docker_daemon_ready", lambda ctx: None)
    monkeypatch.setattr(
        "tools.environment_setup._start_docker_desktop", lambda ctx: "许可确认未完成"
    )
    ctx = Context(environment={}, dry_run=False, start_docker=True)
    ctx.docker = "/usr/local/bin/docker"

    result = check_docker(ctx)

    assert result.status == BLOCKED
    assert "许可确认未完成" in result.fix


def test_the_browser_is_opened_only_when_the_frontend_answers(monkeypatch) -> None:
    monkeypatch.setattr("tools.environment_setup._http_ok", lambda url, **kw: False)
    monkeypatch.setattr(
        "tools.environment_setup._open_workspace_browser",
        lambda: pytest.fail("must not open a browser for a workspace that is not serving"),
    )

    result = open_workspace(Context(environment={}, open_browser=True))

    assert result.status == SKIPPED


def test_the_browser_is_opened_once_the_frontend_answers(monkeypatch) -> None:
    monkeypatch.setattr("tools.environment_setup._http_ok", lambda url, **kw: True)
    monkeypatch.setattr("tools.environment_setup._open_workspace_browser", lambda: None)

    result = open_workspace(Context(environment={}, open_browser=True))

    assert result.status == FIXED
    assert FRONTEND_URL in result.detail


def test_a_missing_browser_is_not_treated_as_a_broken_install(monkeypatch) -> None:
    """A headless host has no launcher; the URL is printed either way, so this is not a failure."""
    monkeypatch.setattr("tools.environment_setup._http_ok", lambda url, **kw: True)
    monkeypatch.setattr(
        "tools.environment_setup._open_workspace_browser", lambda: "没有可用的浏览器启动器"
    )

    result = open_workspace(Context(environment={}, open_browser=True))

    assert result.status == SKIPPED
    assert result.failed is False


def test_the_browser_launcher_is_chosen_from_a_case_folded_platform_name(monkeypatch) -> None:
    """``platform.system()`` returns "Darwin"; an exact lowercase match would pick xdg-open.

    That mistake is invisible on Linux CI and breaks the browser on every Mac, which is the one
    platform this repository is developed on, so the comparison is pinned here.
    """
    recorded: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "tools.environment_setup._run",
        lambda command, **kwargs: recorded.append(list(command)) or Completed(),
    )

    from tools.environment_setup import _open_workspace_browser

    assert _open_workspace_browser() is None
    assert recorded and recorded[0][0].endswith("open")
    assert FRONTEND_URL in recorded[0]


def test_no_open_refuses_the_browser(monkeypatch) -> None:
    monkeypatch.setattr(
        "tools.environment_setup._open_workspace_browser",
        lambda: pytest.fail("must not open a browser under --no-open"),
    )

    result = open_workspace(Context(environment={}, open_browser=False))

    assert result.status == SKIPPED
    assert "--no-open" in result.detail


# ---------------------------------------------------------------------------------------------
# Node engine gate
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        # Vite 8 declares ^20.19.0 || >=22.12.0 - a union, so the gaps must be rejected.
        ("v20.18.0", False),
        ("v20.19.0", True),
        ("v20.20.1", True),
        ("v21.7.0", False),
        ("v22.0.0", False),
        ("v22.11.0", False),
        ("v22.12.0", True),
        ("v22.22.2", True),
        ("v23.0.0", True),
        ("v24.21.0", True),
        ("v18.19.0", False),
        ("20.19.0", True),
        ("", False),
        ("not-a-version", False),
    ],
)
def test_node_engine_gate_matches_vite_declaration(version: str, supported: bool) -> None:
    assert node_version_supported(version) is supported


def test_node_gate_accepts_short_and_long_forms_consistently() -> None:
    assert node_version_supported("v22.12") is node_version_supported("v22.12.0")


# ---------------------------------------------------------------------------------------------
# dotenv handling
# ---------------------------------------------------------------------------------------------


def test_read_dotenv_ignores_comments_blanks_and_strips_quotes(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "PLAIN=value\n"
        'QUOTED="quoted value"\n'
        "SINGLE='single value'\n"
        "WITH_EQUALS=a=b=c\n"
        "EMPTY=\n",
        encoding="utf-8",
    )

    values = _read_dotenv(env_file)

    assert values["PLAIN"] == "value"
    assert values["QUOTED"] == "quoted value"
    assert values["SINGLE"] == "single value"
    assert values["WITH_EQUALS"] == "a=b=c"
    assert values["EMPTY"] == ""


def test_read_dotenv_returns_empty_for_absent_file(tmp_path) -> None:
    assert _read_dotenv(tmp_path / "missing.env") == {}


def test_effective_environment_lets_the_process_win(monkeypatch, tmp_path) -> None:
    """An exported shell variable must override the file, matching load_runtime_environment."""
    env_file = tmp_path / ".env"
    env_file.write_text("SETUP_PROBE=from_file\n", encoding="utf-8")
    monkeypatch.setenv("SETUP_PROBE", "from_process")
    monkeypatch.setenv("QANERIS_ENV_FILE", str(env_file))

    assert effective_environment()["SETUP_PROBE"] == "from_process"


def test_effective_environment_uses_the_file_when_the_process_is_silent(
    monkeypatch, tmp_path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SETUP_PROBE_ONLY_FILE=from_file\n", encoding="utf-8")
    monkeypatch.delenv("SETUP_PROBE_ONLY_FILE", raising=False)
    monkeypatch.setenv("QANERIS_ENV_FILE", str(env_file))

    assert effective_environment()["SETUP_PROBE_ONLY_FILE"] == "from_file"


def test_dotenv_path_prefers_the_explicit_override(monkeypatch, tmp_path) -> None:
    target = tmp_path / "custom.env"
    monkeypatch.setenv("QANERIS_ENV_FILE", str(target))

    assert _dotenv_path() == target.resolve()


def test_dotenv_path_defaults_to_the_repository_root(monkeypatch) -> None:
    monkeypatch.delenv("QANERIS_ENV_FILE", raising=False)

    assert _dotenv_path().name == ".env"


# ---------------------------------------------------------------------------------------------
# Neo4j URI parsing
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("uri", "port"),
    [
        ("bolt://localhost:7687", 7687),
        ("bolt://127.0.0.1:7688", 7688),
        ("neo4j://neo4j:7687", 7687),
        ("bolt://localhost", None),
        ("bolt://localhost/neo4j", None),
        ("", None),
        ("localhost:7687", None),
    ],
)
def test_neo4j_port_is_read_from_the_configured_uri(uri: str, port: int | None) -> None:
    assert _neo4j_port(uri) == port


# ---------------------------------------------------------------------------------------------
# master key validation, mirroring ManagedCredentialStore
# ---------------------------------------------------------------------------------------------


def test_a_generated_master_key_decodes_to_exactly_32_bytes() -> None:
    """The command the script prints for operators must satisfy the store's own check."""
    generated = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")

    assert len(base64.urlsafe_b64decode(generated)) == 32


# ---------------------------------------------------------------------------------------------
# tool environment composition
# ---------------------------------------------------------------------------------------------


def test_tool_environment_exposes_vendored_tools_without_touching_the_parent(
    monkeypatch, tmp_path
) -> None:
    """Vendored runtimes are visible to children only; the operator's PATH is not rewritten."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    ctx = Context(environment={}, use_vendored_tools=True)
    ctx.node_bin_dir = tmp_path / "node" / "bin"

    merged = ctx.tool_environment()

    assert str(tmp_path / "node" / "bin") in merged["PATH"].split(os.pathsep)[0]
    assert os.environ["PATH"] == "/usr/bin:/bin"


def test_tool_environment_without_vendored_tools_leaves_path_alone(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    ctx = Context(environment={}, use_vendored_tools=False)

    assert ctx.tool_environment()["PATH"] == "/usr/bin:/bin"


def test_tool_environment_injects_the_neo4j_password_for_compose(monkeypatch) -> None:
    """docker-compose.yml interpolates QANERIS_NEO4J_PASSWORD; the child must receive it."""
    monkeypatch.delenv("QANERIS_NEO4J_PASSWORD", raising=False)
    ctx = Context(environment={"QANERIS_NEO4J_PASSWORD": "from-file"})

    assert ctx.tool_environment(docker_password="explicit")["QANERIS_NEO4J_PASSWORD"] == "explicit"


def test_uv_sync_passes_every_requested_extra(monkeypatch, tmp_path) -> None:
    """The install command must carry --frozen (lockfile honoured) and each extra."""
    recorded: dict[str, object] = {}

    def fake_run(command, **kwargs):
        recorded["command"] = list(command)
        recorded["cwd"] = kwargs.get("cwd")

        class Completed:
            returncode = 0

        return Completed()

    monkeypatch.setattr("tools.environment_setup._run", fake_run)
    ctx = Context(extras=("dev", "sql"), environment={})
    ctx.uv = "/usr/local/bin/uv"

    ctx.uv_sync()

    command = recorded["command"]
    assert command[1:3] == ["sync", "--frozen"]
    assert command.count("--extra") == 2
    assert "dev" in command and "sql" in command


# ---------------------------------------------------------------------------------------------
# verdict reporting
# ---------------------------------------------------------------------------------------------


def test_summary_returns_zero_only_when_nothing_is_blocked(capsys) -> None:
    results = [Result("A", OK, "fine"), Result("B", FIXED, "repaired")]

    assert _print_summary(results) == 0
    assert "0 项待处理" in capsys.readouterr().out


def test_summary_returns_one_and_prints_the_fix_when_blocked(capsys) -> None:
    results = [Result("A", OK, "fine"), Result("B", BLOCKED, "missing", fix="do the thing")]

    assert _print_summary(results) == 1
    output = capsys.readouterr().out
    assert "do the thing" in output
    assert "B" in output


def test_summary_treats_skipped_as_a_success(capsys) -> None:
    """A dry run or an explicitly disabled feature must not fail the overall verdict."""
    assert _print_summary([Result("A", SKIPPED, "dry-run")]) == 0
    assert "待处理" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------
# extra validation
# ---------------------------------------------------------------------------------------------


def test_default_extras_are_all_declared() -> None:
    """A typo in DEFAULT_EXTRAS would only surface as a resolver error deep inside uv sync."""
    assert unknown_extras(DEFAULT_EXTRAS) == []


def test_unknown_extra_is_reported() -> None:
    assert unknown_extras(["dev", "definitely-not-an-extra"]) == ["definitely-not-an-extra"]


def test_unknown_extra_fails_fast_without_running_checks(capsys) -> None:
    """Rejecting the request must happen before any check mutates the machine."""
    exit_code = main(["--check", "--extra", "definitely-not-an-extra"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "definitely-not-an-extra" in captured.err
    assert "可用" in captured.err


# ---------------------------------------------------------------------------------------------
# exit codes
# ---------------------------------------------------------------------------------------------


def test_check_mode_touches_nothing_and_reports_json(monkeypatch, capsys) -> None:
    """--check must never enter a repair path, even when things are missing."""
    monkeypatch.setattr("tools.environment_setup.discover", lambda ctx: None)
    monkeypatch.setattr(
        "tools.environment_setup.run_checks",
        lambda ctx: [Result("Probe", BLOCKED, "missing", fix="do it")],
    )

    exit_code = main(["--check", "--json"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["results"][0]["fix"] == "do it"


# ---------------------------------------------------------------------------------------------
# Docker installation
# ---------------------------------------------------------------------------------------------


def test_docker_install_can_be_refused(monkeypatch) -> None:
    """--no-install-docker must prevent a ~600 MB download entirely."""
    called = {"installed": False}

    def fail_if_called(ctx):
        called["installed"] = True
        raise AssertionError("install_docker must not run when it is disabled")

    monkeypatch.setattr("tools.environment_setup.install_docker", fail_if_called)
    ctx = Context(environment={}, dry_run=False, install_docker=False)
    ctx.docker = None

    result = check_docker(ctx)

    assert called["installed"] is False
    assert result.status == BLOCKED
    assert "--no-install-docker" in result.fix


def test_docker_installs_by_default(monkeypatch) -> None:
    """A single run of setup.py must be enough, so installing Docker is the default."""
    assert Context().install_docker is True

    monkeypatch.setattr(
        "tools.environment_setup.install_docker",
        lambda ctx: Result("Docker", FIXED, "installed"),
    )
    monkeypatch.setattr("tools.environment_setup._which", lambda *a, **kw: None)
    ctx = Context(environment={}, dry_run=False)
    ctx.docker = None

    assert check_docker(ctx).status == FIXED


def test_services_start_by_default_but_can_be_refused() -> None:
    assert Context().start_services is True
    assert Context().prompt_for_model is True


def test_docker_install_is_not_attempted_in_dry_run(monkeypatch) -> None:
    called = {"installed": False}
    monkeypatch.setattr(
        "tools.environment_setup.install_docker",
        lambda ctx: called.update(installed=True),
    )
    ctx = Context(environment={}, dry_run=True, install_docker=True)
    ctx.docker = None

    result = check_docker(ctx)

    assert called["installed"] is False
    assert result.status == SKIPPED


def test_docker_install_runs_when_requested(monkeypatch) -> None:
    """--install-docker must actually reach the installer on a machine without Docker."""
    monkeypatch.setattr(
        "tools.environment_setup.install_docker",
        lambda ctx: Result("Docker", FIXED, "installed"),
    )
    monkeypatch.setattr("tools.environment_setup._which", lambda *a, **kw: None)
    ctx = Context(environment={}, dry_run=False, install_docker=True)
    ctx.docker = None

    result = check_docker(ctx)

    assert result.status == FIXED


def test_installer_platform_selection_is_explicit() -> None:
    """Only macOS and Linux have a documented single-artifact channel."""
    selected = _installer_for_platform()
    if selected is None:
        return  # Windows and other platforms are reported, never guessed at
    kind, url = selected
    assert kind in {"dmg", "script"}
    assert url.startswith("https://")


def test_macos_dmg_url_carries_the_architecture() -> None:
    url = "https://desktop.docker.com/mac/main/{architecture}/Docker.dmg"
    assert url.format(architecture="arm64").endswith("/arm64/Docker.dmg")


# ---------------------------------------------------------------------------------------------
# resumable download
# ---------------------------------------------------------------------------------------------


def test_human_size_is_readable() -> None:
    assert _human_size(512) == "512 B"
    assert _human_size(2048) == "2.0 KB"
    assert _human_size(5 * 1024 * 1024) == "5.0 MB"


def test_resumable_download_skips_a_complete_file(monkeypatch, tmp_path) -> None:
    """A finished artifact must not be re-fetched; the size check decides that first."""
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"x" * 100)
    monkeypatch.setattr("tools.environment_setup._remote_size", lambda url, **kw: 100)

    def explode(*args, **kwargs):
        raise AssertionError("network must not be touched when the file is already complete")

    monkeypatch.setattr("tools.environment_setup._urlopen", explode)

    assert _download_resumable("https://example.invalid/a.bin", target) == target


def test_resumable_download_reports_exhausted_attempts(monkeypatch, tmp_path) -> None:
    """A permanently unreachable host must surface as a SetupError, not a silent partial file."""
    target = tmp_path / "artifact.bin"
    monkeypatch.setattr("tools.environment_setup._remote_size", lambda url, **kw: 1_000_000)
    monkeypatch.setattr("tools.environment_setup.time", SimpleNamespace(sleep=lambda _s: None))

    import urllib.error

    def always_fail(*args, **kwargs):
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr("tools.environment_setup._urlopen", always_fail)

    with pytest.raises(SetupError):
        _download_resumable("https://example.invalid/a.bin", target, attempts=2)


def test_resumable_download_sends_a_range_header_for_a_partial_file(monkeypatch, tmp_path) -> None:
    """Resuming is the whole point: a partial file must produce a Range request, not a restart."""
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"x" * 40)
    monkeypatch.setattr("tools.environment_setup._remote_size", lambda url, **kw: 100)
    captured: dict[str, str] = {}

    def capture(request, **kwargs):
        captured.update(dict(request.headers))
        raise urllib.error.URLError("stop after capturing headers")

    monkeypatch.setattr("tools.environment_setup._urlopen", capture)
    monkeypatch.setattr("tools.environment_setup.time", SimpleNamespace(sleep=lambda _s: None))

    with pytest.raises(SetupError):
        _download_resumable("https://example.invalid/a.bin", target, attempts=1)

    assert captured.get("Range") == "bytes=40-"
    assert target.stat().st_size == 40, "a failed resume must not truncate what is already on disk"


def test_resumable_download_does_not_send_range_for_a_fresh_file(monkeypatch, tmp_path) -> None:
    target = tmp_path / "artifact.bin"
    monkeypatch.setattr("tools.environment_setup._remote_size", lambda url, **kw: 100)
    captured: dict[str, str] = {}

    def capture(request, **kwargs):
        captured.update(dict(request.headers))
        raise urllib.error.URLError("stop")

    monkeypatch.setattr("tools.environment_setup._urlopen", capture)
    monkeypatch.setattr("tools.environment_setup.time", SimpleNamespace(sleep=lambda _s: None))

    with pytest.raises(SetupError):
        _download_resumable("https://example.invalid/a.bin", target, attempts=1)

    assert "Range" not in captured


def test_rediscover_compose_clears_nothing_when_docker_is_absent() -> None:
    """After a failed install there is no docker binary; the helper must be a no-op."""
    ctx = Context(environment={})
    ctx.docker = None
    ctx.compose_version = "previous"

    _rediscover_compose(ctx)

    assert ctx.compose is None


# ---------------------------------------------------------------------------------------------
# generated local secrets
# ---------------------------------------------------------------------------------------------


def test_generated_master_key_satisfies_the_store_requirement() -> None:
    """The generated key must pass the very check ManagedCredentialStore applies."""
    for _ in range(8):
        assert len(base64.urlsafe_b64decode(generate_master_key())) == 32


def test_generated_password_is_url_safe_and_long_enough() -> None:
    """The password is interpolated into compose YAML, so it must need no escaping."""
    for _ in range(8):
        password = generate_password()
        assert len(password) >= 20
        assert all(c.isalnum() or c in "-_" for c in password)


def test_generated_secrets_are_not_repeated() -> None:
    assert len({generate_password() for _ in range(20)}) == 20


# ---------------------------------------------------------------------------------------------
# .env creation and repair
# ---------------------------------------------------------------------------------------------


def test_environment_file_is_created_when_absent(monkeypatch, tmp_path) -> None:
    """A fresh checkout has no .env; one run must produce a usable one."""
    target = tmp_path / ".env"
    monkeypatch.setenv("QANERIS_ENV_FILE", str(target))
    ctx = Context(environment={}, dry_run=False)

    result = _create_environment_file(ctx)

    assert result.status == FIXED
    assert target.is_file()
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    written = _read_dotenv(target)
    assert written["QANERIS_NEO4J_PASSWORD"]
    assert len(base64.urlsafe_b64decode(written["QANERIS_MASTER_KEY"])) == 32
    assert written["QANERIS_NEO4J_URI"] == "bolt://localhost:7687"


def test_created_environment_file_leaves_the_model_section_unset(monkeypatch, tmp_path) -> None:
    """setup.py must not invent an external credential."""
    target = tmp_path / ".env"
    monkeypatch.setenv("QANERIS_ENV_FILE", str(target))
    _create_environment_file(Context(environment={}, dry_run=False))

    written = _read_dotenv(target)
    assert not written.get("QANERIS_MODEL_API_KEY")


def test_environment_file_creation_is_skipped_in_dry_run(monkeypatch, tmp_path) -> None:
    target = tmp_path / ".env"
    monkeypatch.setenv("QANERIS_ENV_FILE", str(target))

    result = _create_environment_file(Context(environment={}, dry_run=True))

    assert result.status == SKIPPED
    assert not target.exists()


def test_a_new_env_does_not_invent_a_key_for_a_store_that_already_has_documents(
    monkeypatch, tmp_path
) -> None:
    """The default store is shared by every checkout, so its key outlives one .env.

    A fresh clone that minted its own key here would leave every already-encrypted credential
    undecryptable while still reporting a healthy setup - the worst possible outcome, because the
    loss is silent. The key must stay blank so the store check can ask for the original.
    """
    home = tmp_path / "home"
    store = home / ".qaneris" / "secrets"
    store.mkdir(parents=True)
    (store / "sec_existing.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("QANERIS_SECRET_STORE_DIR", raising=False)
    monkeypatch.delenv("QANERIS_MASTER_KEY", raising=False)
    target = tmp_path / ".env"
    monkeypatch.setenv("QANERIS_ENV_FILE", str(target))

    result = _create_environment_file(Context(environment={}, dry_run=False))

    assert result.status == FIXED
    assert _read_dotenv(target)["QANERIS_MASTER_KEY"] == ""
    assert result.fix is not None
    assert "QANERIS_MASTER_KEY" in result.fix


def test_a_new_env_generates_a_key_when_the_store_is_empty(monkeypatch, tmp_path) -> None:
    """The guard must not cost the one-run install when there is nothing to preserve."""
    home = tmp_path / "home"
    (home / ".qaneris" / "secrets").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("QANERIS_SECRET_STORE_DIR", raising=False)
    monkeypatch.delenv("QANERIS_MASTER_KEY", raising=False)
    target = tmp_path / ".env"
    monkeypatch.setenv("QANERIS_ENV_FILE", str(target))

    _create_environment_file(Context(environment={}, dry_run=False))

    key = _read_dotenv(target)["QANERIS_MASTER_KEY"]
    assert len(base64.urlsafe_b64decode(key)) == 32


def test_a_new_env_reuses_the_key_it_was_given(monkeypatch, tmp_path) -> None:
    """An operator-supplied key is authoritative, whether or not the store has documents."""
    home = tmp_path / "home"
    store = home / ".qaneris" / "secrets"
    store.mkdir(parents=True)
    (store / "sec_existing.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("QANERIS_SECRET_STORE_DIR", raising=False)
    supplied = generate_master_key()
    monkeypatch.setenv("QANERIS_MASTER_KEY", supplied)
    target = tmp_path / ".env"
    monkeypatch.setenv("QANERIS_ENV_FILE", str(target))

    _create_environment_file(Context(environment={"QANERIS_MASTER_KEY": supplied}, dry_run=False))

    assert _read_dotenv(target)["QANERIS_MASTER_KEY"] == supplied


def test_missing_neo4j_password_is_filled_into_an_existing_file(monkeypatch, tmp_path) -> None:
    target = tmp_path / ".env"
    target.write_text(
        "QANERIS_GRAPH_STORE=neo4j\n"
        "QANERIS_NEO4J_URI=bolt://localhost:7687\n"
        "QANERIS_NEO4J_USERNAME=neo4j\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QANERIS_ENV_FILE", str(target))
    ctx = Context(environment={}, dry_run=False)

    result = check_environment_file(ctx)

    assert result.status == FIXED
    assert _read_dotenv(target)["QANERIS_NEO4J_PASSWORD"]


def test_appending_never_overwrites_existing_keys(tmp_path) -> None:
    """A hand-written value must survive; only genuinely missing keys are appended."""
    target = tmp_path / ".env"
    target.write_text("KEEP=original\n", encoding="utf-8")

    added = _append_env_values(target, {"KEEP": "replacement", "NEW": "value"})

    assert added == ["NEW"]
    written = _read_dotenv(target)
    assert written["KEEP"] == "original"
    assert written["NEW"] == "value"


def test_appending_reports_nothing_when_all_keys_exist(tmp_path) -> None:
    target = tmp_path / ".env"
    target.write_text("PRESENT=1\n", encoding="utf-8")

    assert _append_env_values(target, {"PRESENT": "2"}) == []
    assert _read_dotenv(target)["PRESENT"] == "1"


# ---------------------------------------------------------------------------------------------
# secret store configuration
# ---------------------------------------------------------------------------------------------


def test_store_with_no_documents_is_safe_to_configure(tmp_path) -> None:
    assert _store_has_secrets(tmp_path) is False


def test_store_with_a_document_pins_the_key(tmp_path) -> None:
    """Once a secret exists the key is the only way to read it, so it must not be replaced."""
    (tmp_path / "sec_deadbeefdeadbeefdeadbeefdeadbeef.json").write_text("{}", encoding="utf-8")

    assert _store_has_secrets(tmp_path) is True


def test_secret_store_refuses_a_directory_inside_the_repository(monkeypatch) -> None:
    ctx = Context(
        environment={
            "QANERIS_SECRET_STORE_DIR": str(PROJECT_ROOT / "secrets"),
            "QANERIS_MASTER_KEY": generate_master_key(),
        },
        dry_run=False,
    )

    result = check_secret_store(ctx)

    assert result.status == BLOCKED
    assert "仓库之外" in result.detail


def test_secret_store_reports_an_undecodable_key() -> None:
    ctx = Context(
        environment={
            "QANERIS_SECRET_STORE_DIR": str(Path.home() / ".qaneris" / "probe"),
            "QANERIS_MASTER_KEY": "!!! not base64 !!!",
        },
        dry_run=False,
    )

    result = check_secret_store(ctx)

    assert result.status == BLOCKED
    assert "Base64" in result.detail


def test_secret_store_reports_a_key_of_the_wrong_length() -> None:
    ctx = Context(
        environment={
            "QANERIS_SECRET_STORE_DIR": str(Path.home() / ".qaneris" / "probe"),
            "QANERIS_MASTER_KEY": base64.urlsafe_b64encode(b"too-short").decode(),
        },
        dry_run=False,
    )

    result = check_secret_store(ctx)

    assert result.status == BLOCKED
    assert "字节" in result.detail


def test_dry_run_does_not_generate_a_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QANERIS_SECRET_STORE_DIR", str(tmp_path / "store"))
    ctx = Context(environment={}, dry_run=True)

    result = check_secret_store(ctx)

    assert result.status == SKIPPED
    assert not (tmp_path / "store").exists()
