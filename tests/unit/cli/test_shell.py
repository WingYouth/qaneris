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
import contextlib
import io
import logging
import os
import re
import stat
import sys
from pathlib import Path
from unittest.mock import Mock, patch

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
    options = [
        option for item in action.choices["shell"]._actions for option in item.option_strings
    ]

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
# line normalization
# --------------------------------------------------------------------------------------------


def test_a_repeated_program_name_is_stripped() -> None:
    """A command copied from a README arrives fully qualified; inside the session it is redundant."""
    assert shell_cli.normalize_tokens(["qaneris", "source", "list"]) == ["source", "list"]


def test_a_line_without_the_program_name_is_untouched() -> None:
    assert shell_cli.normalize_tokens(["source", "list"]) == ["source", "list"]


def test_the_program_name_alone_leaves_nothing_to_run() -> None:
    """Typing just the program name is the one input the caller has to answer itself."""
    assert shell_cli.normalize_tokens(["qaneris"]) == []


def test_normalisation_cannot_change_which_command_was_meant() -> None:
    """The name can never be a subcommand, so dropping it is safe for every valid line."""
    assert shell_cli._PROGRAM_NAME not in shell_cli.registered_commands()


def test_a_prefixed_line_reaches_dispatch_as_the_command_itself() -> None:
    dispatch = Mock(return_value=0)

    tokens = shell_cli.normalize_tokens(shell_cli.split_line("qaneris source list"))
    shell_cli.run_tokens(tokens, dispatch=dispatch)

    dispatch.assert_called_once_with(["source", "list"])


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
# status line
# --------------------------------------------------------------------------------------------


def _render_banner(width: int, environment: dict[str, str] | None = None) -> str:
    """Render the real banner at ``width`` and return its visible text.

    The banner is drawn by rich, so its layout can only be judged from rendered output; asserting
    on the text is what keeps these tests about the contract (a rule that fits, values that
    survive) rather than about a particular frame.
    """
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme

    buffer = io.StringIO()
    console = Console(
        file=buffer,
        force_terminal=True,
        width=width,
        height=60,
        color_system="truecolor",
        theme=Theme(shell_cli._THEME),
        highlight=False,
    )
    with contextlib.ExitStack() as stack:
        for name, value in (environment or {}).items():
            stack.enter_context(patch.dict(os.environ, {name: value}))
        shell_cli._print_banner(console, Table, Text)
    return re.sub("\x1b\\[[0-9;]*m", "", buffer.getvalue())


def _longest_line(rendered: str) -> int:
    return max(len(line) for line in rendered.splitlines())


def _toolbar_styles(state: shell_cli.ShellState) -> list[str]:
    return [style for style, _ in shell_cli._toolbar_text(state)]


def test_the_status_line_reports_the_sessions_own_facts() -> None:
    state = shell_cli.ShellState(last_command="doctor", last_exit_code=0, executed=3)

    text = "".join(part for _, part in shell_cli._toolbar_text(state))

    assert "doctor" in text
    assert "0" in text
    assert "3" in text


@pytest.mark.parametrize(
    "exit_code, expected",
    [(0, "class:q.toolbar.ok"), (1, "class:q.toolbar.fail"), (2, "class:q.toolbar.fail")],
)
def test_the_exit_code_carries_the_colour_of_its_meaning(exit_code: int, expected: str) -> None:
    """A usage error (2) is shown like a failure, because that is what it is to the user."""
    pairs = shell_cli._toolbar_text(shell_cli.ShellState(last_exit_code=exit_code))

    assert (expected, str(exit_code)) in pairs


def test_an_absent_exit_code_is_neutral() -> None:
    """Before any command runs there is no verdict, so the line must not imply one."""
    pairs = shell_cli._toolbar_text(shell_cli.ShellState())

    assert ("class:q.toolbar.ok", "-") not in pairs
    assert ("class:q.toolbar.fail", "-") not in pairs


def test_a_long_command_is_truncated_for_display_only() -> None:
    state = shell_cli.ShellState(last_command="ask " + "x" * 200)

    text = "".join(part for _, part in shell_cli._toolbar_text(state))

    assert len(text) < 200
    assert "..." in text


def test_the_status_line_is_not_painted_in_reverse_video() -> None:
    """prompt_toolkit styles ``bottom-toolbar`` with ``reverse`` by default.

    Left alone, that turns the status line into a bright inverted strip: the exit-code colours
    below are inverted with it, so a green ``0`` renders as a red block on some themes. The
    session cancels the inherited attribute and states its own.
    """
    from prompt_toolkit.styles import Style, merge_styles
    from prompt_toolkit.styles.defaults import default_ui_style

    merged = merge_styles([default_ui_style(), Style.from_dict(shell_cli._PROMPT_STYLE)])

    classes = ["class:bottom-toolbar", "class:bottom-toolbar class:bottom-toolbar.text"]
    classes += [style for style, _ in shell_cli._toolbar_text(shell_cli.ShellState())]
    for style_str in classes:
        assert merged.get_attrs_for_style_str(style_str).reverse is False


def test_the_status_line_advertises_only_words_the_session_honours() -> None:
    """A hint that names a word the session does not implement is worse than no hint."""
    text = "".join(part for _, part in shell_cli._toolbar_text(shell_cli.ShellState()))
    commands = shell_cli.registered_commands()

    for word in re.findall(r"[A-Za-z?][\w?]*", text):
        assert (
            word in shell_cli._BUILTINS
            or word in commands
            or word in {"qaneris", "exit", "run", "Ctrl", "D"}
        )


def test_a_nested_session_is_redirected_to_help() -> None:
    """``shell`` inside the session would nest a second session and cost two ``exit`` lines."""
    commands = shell_cli.registered_commands()

    assert shell_cli._is_nested_session(["shell"], commands) is True


def test_the_nested_session_check_ignores_every_other_line() -> None:
    commands = shell_cli.registered_commands()

    for tokens in ([], ["doctor"], ["source", "list"], ["qaneris", "shell"]):
        assert shell_cli._is_nested_session(tokens, commands) is False


def test_the_question_mark_is_a_help_builtin() -> None:
    """``?`` is the conventional shortcut, so the session implements it rather than dropping it."""
    assert "?" in shell_cli._BUILTINS

    dispatch = Mock(return_value=0)
    console = Mock()

    assert shell_cli._handle_builtin("?", dispatch=dispatch, console=console) is True
    dispatch.assert_called_once_with(["--help"])


def test_the_status_line_never_renders_markup_from_user_input() -> None:
    """The last command is user input, so a bracketed value must survive as literal text."""
    state = shell_cli.ShellState(last_command="ask '[red]not a tag[/red]'")

    text = "".join(part for _, part in shell_cli._toolbar_text(state))

    assert "[red]not a tag[/red]" in text


# --------------------------------------------------------------------------------------------
# banner
# --------------------------------------------------------------------------------------------


def test_banner_facts_read_the_configured_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QANERIS_CATALOG", "/tmp/example.db")
    monkeypatch.setenv("QANERIS_SECRET_STORE_DIR", "/tmp/secrets")

    facts = dict(shell_cli._banner_facts())

    assert facts["catalog"] == "/tmp/example.db"
    assert facts["credentials"] == "/tmp/secrets"


def test_banner_facts_name_the_defaults_instead_of_going_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset value is reported as a default or as unconfigured, never as an empty cell."""
    monkeypatch.delenv("QANERIS_CATALOG", raising=False)

    facts = dict(shell_cli._banner_facts())

    assert facts["catalog"]
    assert facts["model"]


def test_banner_facts_never_raise_on_an_unreadable_environment_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A broken dotenv is reported in the banner rather than ending the session before it starts."""
    monkeypatch.setenv("QANERIS_ENV_FILE", str(tmp_path / "absent.env"))

    facts = dict(shell_cli._banner_facts())

    assert "environment" in facts


def test_the_banner_never_reads_a_bracketed_path_as_markup() -> None:
    """A path is data. Interpolated into markup, a bracketed directory name loses its brackets."""
    rendered = _render_banner(80, {"QANERIS_CATALOG": "/tmp/[red]weird/catalog.db"})

    assert "/tmp/[red]weird/catalog.db" in rendered


def test_the_banner_rule_fills_the_terminal_instead_of_a_fixed_width() -> None:
    """A hard-coded rule either stops halfway across a wide terminal or wraps on a narrow one."""
    narrow = _render_banner(64)
    wide = _render_banner(140)

    assert _longest_line(narrow) <= 64
    assert _longest_line(wide) <= 140
    # The card is drawn by rich, so the border it emits is what proves the width was honoured.
    assert _longest_line(wide) > _longest_line(narrow)


def test_a_narrow_banner_stacks_its_sections_instead_of_wrapping_a_value() -> None:
    """Below the width both sections need, they stack - a wrapped path reads as two values."""
    rendered = _render_banner(64)

    assert "Environment" in rendered
    assert "Start here" in rendered
    # Stacked, the second heading starts a line of its own instead of sharing one with the first.
    assert not any("Start here" in line and "Environment" in line for line in rendered.splitlines())


def test_a_wide_banner_places_its_sections_side_by_side() -> None:
    rendered = _render_banner(120)

    assert any("Start here" in line and "Environment" in line for line in rendered.splitlines())


def test_a_home_directory_path_is_abbreviated_for_display() -> None:
    """The credential store is long enough to crowd the card, and ``~`` is how users write it."""
    inside = str(Path.home() / ".qaneris" / "secrets")

    assert shell_cli._display_path(inside) == "~/.qaneris/secrets"


@pytest.mark.parametrize("value", ["/var/lib/qaneris/secrets", "not configured", "deepseek-v4.1"])
def test_a_value_that_is_not_under_the_home_directory_is_left_alone(value: str) -> None:
    """Only a real home-directory prefix is shortened; a model name must not be rewritten."""
    assert shell_cli._display_path(value) == value


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
