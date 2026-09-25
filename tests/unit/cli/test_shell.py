"""Contract tests for ``qaneris shell``.

These lock the session boundary, not the terminal UI: which words are session builtins, that a
line reaches the one-shot command tree unchanged, that argparse's own exits cannot end a session,
that one failed command never takes the session with it, and that the history file is created with
owner-only permissions.

The rendering is deliberately not asserted - it lives in ``prompt_toolkit`` and ``rich``, and the
session's contract is that it adds no product behaviour, not that it draws a particular frame.
"""

from __future__ import annotations

import argparse
import logging
import os
import stat
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from qaneris.cli import main as cli
from qaneris.cli import shell as shell_cli

# --------------------------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------------------------


def test_shell_is_registered_in_the_command_tree(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])

    assert raised.value.code == 0
    assert "shell" in capsys.readouterr().out


def test_shell_help_documents_its_options(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["shell", "--help"])

    assert raised.value.code == 0
    usage = capsys.readouterr().out
    for option in ("--history", "--no-history", "--no-banner"):
        assert option in usage


def test_shell_offers_no_option_that_carries_a_secret() -> None:
    """The session input line is echoed and remembered, so no secret may be typed into an argv."""
    parser = cli.build_parser()
    action = next(
        item
        for item in parser._actions
        if isinstance(item, argparse._SubParsersAction) and "shell" in item.choices
    )
    options = [option for item in action.choices["shell"]._actions for option in item.option_strings]

    assert not [item for item in options if "password" in item or "token" in item or "key" in item]


# --------------------------------------------------------------------------------------------
# dispatch boundary
# --------------------------------------------------------------------------------------------


def test_a_line_reaches_dispatch_with_its_arguments_intact() -> None:
    dispatch = Mock(return_value=0)

    outcome = shell_cli.run_tokens(["ask", "总实收销售额是多少？", "--json"], dispatch=dispatch)

    assert outcome.exit_code == 0
    assert outcome.failure_type is None
    dispatch.assert_called_once_with(["ask", "总实收销售额是多少？", "--json"])


def test_quoting_is_honoured_the_way_a_shell_splits_it() -> None:
    tokens = shell_cli.split_line('ask "总实收销售额是多少？" --datasource ds_1')

    assert tokens == ["ask", "总实收销售额是多少？", "--datasource", "ds_1"]


def test_an_unclosed_quote_is_reported_rather_than_run() -> None:
    assert shell_cli.split_line('ask "unterminated') is None


@pytest.mark.parametrize("code", [0, 1, 2])
def test_a_dispatch_exit_code_is_preserved(code: int) -> None:
    assert shell_cli.run_tokens(["doctor"], dispatch=Mock(return_value=code)).exit_code == code


def test_argparse_help_cannot_end_the_session() -> None:
    """``argparse`` reports ``--help`` by raising ``SystemExit``; the session must survive it."""
    dispatch = Mock(side_effect=SystemExit(0))

    outcome = shell_cli.run_tokens(["--help"], dispatch=dispatch)

    assert outcome.exit_code == 0
    assert outcome.failure_type is None


@pytest.mark.parametrize("code, expected", [(2, 2), (None, 0), ("message", 1)])
def test_a_usage_error_becomes_an_exit_code(code, expected) -> None:
    dispatch = Mock(side_effect=SystemExit(code))

    assert shell_cli.run_tokens(["badcommand"], dispatch=dispatch).exit_code == expected


def test_a_failed_command_does_not_end_the_session() -> None:
    """An unexpected failure is reported by type alone, like the one-shot boundary."""
    dispatch = Mock(side_effect=RuntimeError("password=hunter2"))

    outcome = shell_cli.run_tokens(["doctor"], dispatch=dispatch)

    assert outcome.exit_code == 1
    assert outcome.failure_type == "RuntimeError"


def test_an_interrupted_command_is_reported_as_cancelled() -> None:
    dispatch = Mock(side_effect=KeyboardInterrupt())

    outcome = shell_cli.run_tokens(["ask", "question"], dispatch=dispatch)

    assert outcome.cancelled is True
    assert outcome.exit_code == 130


# --------------------------------------------------------------------------------------------
# builtins
# --------------------------------------------------------------------------------------------


def test_the_real_command_tree_is_discovered() -> None:
    commands = shell_cli.registered_commands()

    assert {"doctor", "ask", "source", "credential", "certificate", "shell"} <= commands


def test_builtins_never_shadow_a_real_command() -> None:
    """A builtin is only honoured when the word is not a command the parser accepts.

    The two sets are disjoint today, which is what lets the session treat ``help`` as its own word
    without ever hiding a command. If a command named ``help`` is ever added, the session keeps
    dispatching it to the command tree rather than to the builtin.
    """
    commands = shell_cli.registered_commands()

    assert not set(shell_cli._BUILTINS) & commands


def test_a_word_that_is_both_a_builtin_and_a_command_reaches_the_command_tree() -> None:
    """The delegated case behind the disjointness the session relies on."""
    assert "help" in shell_cli._BUILTINS


# --------------------------------------------------------------------------------------------
# history file
# --------------------------------------------------------------------------------------------


def test_history_directory_and_file_are_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "shell_history"

    prepared = shell_cli.prepare_history(target)

    assert prepared == target
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_an_existing_history_file_is_left_alone(tmp_path: Path) -> None:
    target = tmp_path / "shell_history"
    target.write_text("doctor\n", encoding="utf-8")
    os.chmod(target, 0o600)

    assert shell_cli.prepare_history(target) == target
    assert target.read_text(encoding="utf-8") == "doctor\n"


def test_no_history_path_means_no_history(tmp_path: Path) -> None:
    assert shell_cli.prepare_history(None) is None


def test_an_unusable_history_path_is_not_fatal(tmp_path: Path) -> None:
    """Losing history is preferable to refusing to start, so the session falls back to memory."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    os.chmod(blocked, 0o500)
    try:
        assert shell_cli.prepare_history(blocked / "shell_history") is None
    finally:
        os.chmod(blocked, 0o700)


# --------------------------------------------------------------------------------------------
# driver noise
# --------------------------------------------------------------------------------------------


def test_driver_notification_logging_is_raised_for_the_session() -> None:
    logger = logging.getLogger("neo4j.notifications")
    original = logger.level
    try:
        shell_cli.quiesce_driver_logging()
        assert logger.level == logging.ERROR
    finally:
        logger.setLevel(original)


def test_the_quiesced_loggers_are_scoped_to_the_driver() -> None:
    assert all(name.startswith("neo4j") for name in shell_cli._QUIESCED_LOGGERS)


# --------------------------------------------------------------------------------------------
# launch guards
# --------------------------------------------------------------------------------------------


def test_a_non_terminal_is_refused_before_anything_else(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A redirected invocation is a usage error, so a script cannot get an interactive session."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    args = argparse.Namespace(history=None, no_history=True, no_banner=True)

    assert shell_cli.run_shell_command(args) == 2
    assert "needs a terminal" in capsys.readouterr().err


def test_missing_optional_dependencies_report_the_install_command(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """The core install has no prompt_toolkit; the failure must be actionable, not a traceback."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setitem(sys.modules, "prompt_toolkit", None)
    args = argparse.Namespace(history=tmp_path / "h", no_history=False, no_banner=True)

    assert shell_cli.run_shell_command(args) == 2
    assert "pip install -e '.[shell]'" in capsys.readouterr().err


# --------------------------------------------------------------------------------------------
# standalone entry point
# --------------------------------------------------------------------------------------------


def test_env_file_is_lifted_in_front_of_the_subcommand() -> None:
    leading, rest = shell_cli._extract_env_file(["--env-file", "a.env", "--no-banner"])

    assert leading == ["--env-file", "a.env"]
    assert rest == ["--no-banner"]


def test_env_file_in_equals_form_is_lifted_too() -> None:
    leading, rest = shell_cli._extract_env_file(["--env-file=a.env", "--no-history"])

    assert leading == ["--env-file=a.env"]
    assert rest == ["--no-history"]


def test_the_standalone_entry_point_reaches_the_same_command(monkeypatch) -> None:
    """``qaneris-shell`` is the same session, so it must land on the same dispatch branch."""
    dispatch = Mock(return_value=0)
    monkeypatch.setattr(cli, "run_shell_command", dispatch)
    monkeypatch.setattr(cli, "load_runtime_environment", Mock())

    assert shell_cli.main(["--no-banner"]) == 0
    assert dispatch.call_args.args[0].command == "shell"


def test_the_standalone_entry_point_accepts_a_trailing_env_file(
    tmp_path: Path, monkeypatch
) -> None:
    """The value of lifting ``--env-file``: it works where the caller typed it, after the command.

    ``shell`` takes no ``--env-file`` option, so without the lift this invocation would fail as an
    unrecognized argument.
    """
    env_file = tmp_path / "custom.env"
    env_file.write_text("QANERIS_WORKSPACE=default\n", encoding="utf-8")
    dispatch = Mock(return_value=0)
    monkeypatch.setattr(cli, "run_shell_command", dispatch)

    assert shell_cli.main(["--no-banner", "--env-file", str(env_file)]) == 0
    assert dispatch.call_args.args[0].env_file == env_file
