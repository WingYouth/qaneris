"""Interactive session over the existing command tree.

``qaneris shell`` is an inline terminal UI: a banner, a persistent prompt and a status line are
drawn by the terminal UI, while every command's own output stays in the terminal's normal
scrollback. That shape is the point - a session that can be scrolled, copied and searched like any
other terminal output, rather than an alternate-screen application that owns the display.

The session owns no product behaviour. A line is split and handed to the same ``main()`` the
one-shot commands use, so the shell cannot drift from the command contract: a command that works as
``qaneris ask ...`` works here unchanged, with the same output, the same exit code and the same
error rules. Nothing in this module reaches into the Application Service, the catalog or a driver,
and nothing here re-implements a command's parsing or rendering.

Two facts shape the implementation:

* ``argparse`` ends a bad invocation with ``SystemExit``, ``--help`` included. A session that let
  that through would die on a typo, so the dispatch boundary turns it into an exit code.
* Some drivers log about the *server* rather than about the command - the Neo4j driver reports DBMS
  notifications at WARNING. Those records are harmless beside a one-shot report, but in a session
  they would be written across the prompt, so the session raises their level.

The optional dependencies are imported inside the command, never at module import time, so
``qaneris --help`` keeps working on an installation that never asked for a shell.
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SHELL_COMMAND = "shell"

#: Exit codes follow the existing CLI convention; ``130`` is the conventional SIGINT code, used
#: here for one interrupted command rather than for an interrupted session.
_EXIT_OK = 0
_EXIT_FAILED = 1
_EXIT_CONFIGURATION = 2
_EXIT_INTERRUPTED = 130

#: Session-local words that never reach the command tree. They are only honoured when the word is
#: not a registered command, so a future ``qaneris help`` cannot be silently shadowed.
#:
#: ``?`` is the conventional terminal shortcut for help, and the status line advertises it, so the
#: session honours it here rather than leaving the hint pointing at a word that does nothing.
_BUILTINS = ("?", "clear", "exit", "help", "quit")

#: Where the session remembers its input. ``.tools/`` is this repository's gitignored runtime
#: directory, already excluded from version control.
_HISTORY_DIRECTORY = ".tools"
_HISTORY_FILENAME = "shell_history"
_HISTORY_DIRECTORY_MODE = 0o700
_HISTORY_FILE_MODE = 0o600

_TOOLBAR_COMMAND_LIMIT = 60

#: The program's own name. Commands are usually written with it in a terminal or a README, so a
#: line may arrive as ``qaneris source list``. It is stripped rather than rejected: the token can
#: never be a subcommand, so removing it cannot change which command was meant.
_PROGRAM_NAME = "qaneris"

#: Terminal-agnostic palette. Named ANSI colours rather than RGB values: they resolve against the
#: user's own theme, so the session stays legible on a light background and a dark one alike.
#: ``brand`` is the single accent; everything structural is neutral so the accent keeps meaning.
_THEME = {
    "q.brand": "bold cyan",
    "q.title": "bold",
    "q.muted": "dim",
    "q.ok": "bold green",
    "q.warn": "yellow",
    "q.fail": "bold red",
    "q.key": "cyan",
    "q.rule": "dim cyan",
}

#: The commands the banner offers as a starting point. Each entry is the command and the short
#: description of what it does; the list is deliberately short, because the banner is a signpost
#: rather than the help text - ``help`` is one keystroke away for the full tree.
_BANNER_STARTS = (
    ("doctor", "check the runtime environment"),
    ("source list", "list datasources"),
    ('ask "..."', "ask one question"),
)

#: Horizontal space between the banner's two sections, and the space the card itself spends on
#: borders and padding. Both are subtracted before deciding whether two columns will fit.
_BANNER_GAP = 6
_BANNER_CARD_OVERHEAD = 6

#: Third-party loggers that describe the database server instead of this command. Raising them to
#: ERROR keeps the prompt readable; they are not part of the product's own output contract.
_QUIESCED_LOGGERS = ("neo4j.notifications",)

#: A rounded prompt: the shape separates the session's own input line from command output without
#: spending a second line the way a bracketed marker would.
_PROMPT = [
    ("class:q.prompt", "\u256d\u2500 "),
    ("class:q.prompt.chevron", "\u276f "),
]

_PROMPT_CONTINUATION = [("class:q.prompt", "\u2502 ")]

#: prompt_toolkit styles for the pieces the session draws itself.
#:
#: ``bottom-toolbar`` is overridden deliberately. prompt_toolkit's default style paints that
#: class with ``reverse``, which turns the status line into a bright inverted strip - on a light
#: theme it renders as a solid light bar that fights the command output above it, and the
#: per-segment colours below are inverted with it. ``noreverse`` cancels that inherited attribute
#: so the strip is the colour stated here and the exit-code colours mean what they say.
_PROMPT_STYLE = {
    "bottom-toolbar": "noreverse bg:ansibrightblack",
    "q.prompt": "ansicyan",
    "q.prompt.chevron": "ansicyan bold",
    "q.toolbar": "bg:ansibrightblack ansigray",
    "q.toolbar.label": "bg:ansibrightblack ansigray bold",
    "q.toolbar.ok": "bg:ansibrightblack ansigreen bold",
    "q.toolbar.fail": "bg:ansibrightblack ansired bold",
    "q.toolbar.hint": "bg:ansibrightblack ansigray italic",
}

_MISSING_DEPENDENCIES_MESSAGE = (
    "Configuration error: the interactive shell needs its optional dependencies.\n"
    "Install them with:  pip install -e '.[shell]'"
)


@dataclass
class ShellState:
    """The facts the status line publishes. Display state only - never product state."""

    last_command: str = "-"
    last_exit_code: int | None = None
    executed: int = 0


@dataclass(frozen=True)
class CommandOutcome:
    """What one dispatched line produced, in the form the session can render."""

    exit_code: int
    failure_type: str | None = None
    cancelled: bool = False


def add_shell_command(commands: argparse._SubParsersAction) -> None:
    shell = commands.add_parser(
        "shell", help="run an interactive session over the qaneris commands"
    )
    shell.add_argument(
        "--history",
        type=Path,
        default=None,
        help=f"command history file (default: {_HISTORY_DIRECTORY}/{_HISTORY_FILENAME})",
    )
    shell.add_argument(
        "--no-history",
        action="store_true",
        dest="no_history",
        help="do not read or write a command history file",
    )
    shell.add_argument(
        "--no-banner",
        action="store_true",
        dest="no_banner",
        help="skip the welcome panel",
    )


def split_line(line: str) -> list[str] | None:
    """Split one session line, or ``None`` when its quoting does not close."""
    try:
        return shlex.split(line)
    except ValueError:
        return None


def normalize_tokens(tokens: Sequence[str]) -> list[str]:
    """Drop a repeated program name from the front of a line.

    Copying a command out of a README or back out of the scrollback produces the fully qualified
    form, and inside the session that prefix is redundant. It is safe to remove because the
    program's own name can never be a subcommand, so no valid line is altered by dropping it.
    An empty result means the user typed the program name alone, which the caller answers with
    the help text - the same thing the one-shot entry point does.
    """
    remaining = list(tokens)
    while remaining and remaining[0] == _PROGRAM_NAME:
        remaining.pop(0)
    return remaining


def run_tokens(
    tokens: Sequence[str], *, dispatch: Callable[[Sequence[str]], int]
) -> CommandOutcome:
    """Run one already-split command line and report its outcome.

    This is the session's whole dispatch boundary, kept free of rendering so it can be tested
    without a terminal. ``dispatch`` is the one-shot CLI entry point: the session passes the real
    ``main`` and a test passes a fake.

    ``argparse`` reports ``--help`` and every usage error by raising ``SystemExit``; letting that
    escape would end the session, so it is converted into the exit code it carries. An unexpected
    failure is reported by type alone, matching the one-shot boundary, because an arbitrary message
    could carry a provider credential or a bound query parameter.
    """
    try:
        code = dispatch(tokens)
    except SystemExit as error:
        return CommandOutcome(_system_exit_code(error))
    except KeyboardInterrupt:
        return CommandOutcome(_EXIT_INTERRUPTED, cancelled=True)
    except Exception as error:  # noqa: BLE001 - one failed command must not end the session
        return CommandOutcome(_EXIT_FAILED, failure_type=type(error).__name__)
    return CommandOutcome(code if isinstance(code, int) else _EXIT_FAILED)


def quiesce_driver_logging() -> None:
    """Raise the level of third-party loggers that report on the server, not on the command.

    Scoped to the session process: the API and the one-shot commands keep their behaviour, because
    one-shot noise lands on stderr beside a report where it is harmless.
    """
    for name in _QUIESCED_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


def prepare_history(path: Path | None) -> Path | None:
    """Create the history file with restricted permissions, or return ``None``.

    Questions asked in a session can describe business data, so the file follows the same
    discipline as the managed credential store: an owner-only directory and an owner-only file.
    A history file that cannot be prepared is not worth refusing to start over, so failure simply
    means the session keeps its history in memory.
    """
    if path is None:
        return None
    try:
        parent = path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            os.chmod(parent, _HISTORY_DIRECTORY_MODE)
        if not path.exists():
            path.touch()
            os.chmod(path, _HISTORY_FILE_MODE)
    except OSError:
        return None
    return path


def registered_commands() -> frozenset[str]:
    """Return the command names the real parser accepts.

    Imported lazily because ``qaneris.cli.main`` imports this module. The session uses the set to
    decide whether a word is a session builtin or a real command, so a builtin can never shadow a
    command that exists.
    """
    from qaneris.cli.main import build_parser

    parser = build_parser()
    # argparse exposes no public accessor for a parser's own subcommands.
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return frozenset(action.choices)
    return frozenset()


def _system_exit_code(error: SystemExit) -> int:
    """Return the exit code ``argparse`` meant to end the process with."""
    if error.code is None:
        return _EXIT_OK
    return error.code if isinstance(error.code, int) else _EXIT_FAILED


def _default_history_path() -> Path:
    return Path(_HISTORY_DIRECTORY) / _HISTORY_FILENAME


def _exit_style(exit_code: int | None) -> str:
    """Colour the exit code by what it means, not by whether the command printed anything.

    0 and 1 are the product's own outcomes; 2 is a usage error and 130 an interrupted command.
    An unknown or absent code stays neutral rather than borrowing a verdict.
    """
    if exit_code == 0:
        return "class:q.toolbar.ok"
    if exit_code in {1, 2}:
        return "class:q.toolbar.fail"
    return "class:q.toolbar"


def _toolbar_text(state: ShellState) -> list[tuple[str, str]]:
    """Build the status line as ``(style, text)`` pairs.

    Pairs rather than markup: the last command is user input, and a markup string would render a
    bracketed value as styling instead of as the text the user typed. The pairing is also what lets
    the exit code carry its own colour without the command text inheriting it.
    """
    command = state.last_command
    if len(command) > _TOOLBAR_COMMAND_LIMIT:
        command = command[: _TOOLBAR_COMMAND_LIMIT - 3] + "..."
    code = "\u2013" if state.last_exit_code is None else str(state.last_exit_code)
    return [
        ("class:q.toolbar.label", " qaneris "),
        ("class:q.toolbar", command),
        ("class:q.toolbar.label", "  exit "),
        (_exit_style(state.last_exit_code), code),
        ("class:q.toolbar.label", "  run "),
        ("class:q.toolbar", str(state.executed)),
        ("class:q.toolbar.hint", "   help \u00b7 Ctrl-D exit"),
    ]


def _banner_facts() -> list[tuple[str, str]]:
    """Read the facts the banner shows, each degrading to a neutral value on its own.

    Everything here is already public to the process - the model the session would call, where its
    catalog lives, which store holds credentials. None of it is a secret, and none of it decides
    behaviour: a value that cannot be resolved reports that instead of failing the session.
    """
    from qaneris.common.environment import load_runtime_environment
    from qaneris.llm import resolve_model_profile

    # The one-shot CLI loads the dotenv file inside ``main()``, which has not run yet when the
    # banner is drawn. Loading it here is the same call that ``main()`` makes and is idempotent -
    # an already-exported value still wins - so the banner reports the environment the session is
    # actually about to use rather than the one this process started with. A file that cannot be
    # read is reported as an absent value below instead of ending the session before it starts.
    try:
        load_runtime_environment()
        environment_error = None
    except Exception as error:  # noqa: BLE001 - the banner reports it rather than raising
        environment_error = type(error).__name__

    if environment_error is not None:
        return [("environment", f"unreadable ({environment_error})")]

    facts: list[tuple[str, str]] = []
    try:
        profile = resolve_model_profile()
    except Exception:  # noqa: BLE001 - a banner must never be the reason a session fails
        profile = None
    facts.append(("model", profile.model if profile is not None else "not configured"))

    store = os.getenv("QANERIS_SECRET_STORE_DIR", "").strip()
    facts.append(("credentials", store if store else "not configured"))

    catalog = os.getenv("QANERIS_CATALOG", "").strip()
    facts.append(("catalog", catalog or "qaneris.db (default)"))
    return facts


def _display_path(value: str) -> str:
    """Shorten a path under the home directory to ``~``. Display only; the value is unchanged.

    The credential store and the catalog are the two facts most likely to be long absolute paths,
    and an abbreviated home directory is both shorter and easier to read. A value that is not a
    path under the home directory - a model name, a word like ``not configured`` - is returned
    untouched.
    """
    try:
        relative = Path(value).expanduser().relative_to(Path.home())
    except (RuntimeError, ValueError):
        return value
    return str(Path("~") / relative)


def _banner_section(
    title: str, rows: Sequence[tuple[str, str]], key_style: str, value_style: str, text_class: Any
) -> Any:
    """Build one titled block: a heading, then ``key   value`` rows aligned on a shared gutter.

    The gutter is computed from the longest key, so the values line up without a nested grid -
    a nested grid asks rich to divide the remaining width between two cells, which is what made
    a long credential-store path ellipsise even when the terminal had room for it.
    """
    gutter = max(len(key) for key, _ in rows) + 2
    block = text_class(title, style="q.title")
    for key, value in rows:
        block.append("\n  ")
        block.append(key.ljust(gutter), style=key_style)
        block.append(value, style=value_style)
    return block


def _banner_width(rows: Sequence[tuple[str, str]], title: str) -> int:
    """Return the width one section needs to render without wrapping a single row."""
    gutter = max(len(key) for key, _ in rows) + 2
    return max(len(title), max(2 + gutter + len(value) for _, value in rows))


def _print_banner(console: Any, group_class: Any, text_class: Any) -> None:
    """Draw the welcome card.

    Presentation only: it states no capability of its own. Three facts shape the layout. The card
    spans the terminal, so the rule cannot be a fixed-width string - a hard-coded line either
    stops halfway across a wide terminal or wraps on a narrow one. Every value below the heading
    is *data* - a model name, a directory, a catalog path - so each is appended as text rather
    than interpolated into markup, where a path containing square brackets would be read as a
    style tag and silently lose the bracketed part. And the two sections sit side by side only
    when they both fit: below that width they stack, which costs a few lines but never wraps a
    path in the middle of a value.
    """
    from qaneris import __version__

    heading = text_class()
    heading.append("\u25c6 ", style="q.brand")
    heading.append("QANERIS", style="q.title")
    heading.append(f"  v{__version__}", style="q.muted")
    heading.append("  \u00b7  ", style="q.rule")
    heading.append("interactive session over the qaneris command tree", style="q.muted")

    starts_rows = list(_BANNER_STARTS)
    environment_rows = [(label, _display_path(value)) for label, value in _banner_facts()]
    starts = _banner_section("Start here", starts_rows, "q.key", "q.muted", text_class)
    environment = _banner_section("Environment", environment_rows, "q.muted", "dim", text_class)

    columns = group_class.grid(padding=(0, _BANNER_GAP))
    columns.add_column(justify="left", vertical="top")
    columns.add_column(justify="left", vertical="top")
    available = console.width - _BANNER_CARD_OVERHEAD
    required = (
        _banner_width(starts_rows, "Start here")
        + _banner_width(environment_rows, "Environment")
        + _BANNER_GAP
    )
    if required <= available:
        columns.add_row(starts, environment)
    else:
        columns.add_row(starts)
        columns.add_row(text_class(""))
        columns.add_row(environment)

    body = group_class.grid(padding=(0, 0))
    body.add_column(justify="left")
    body.add_row(heading)
    body.add_row(text_class(""))
    body.add_row(columns)

    console.print()
    console.print(
        _banner_panel(body, text_class, "qaneris shell", "Ctrl-C cancel \u00b7 Ctrl-D exit")
    )
    console.print()


def _banner_panel(body: Any, text_class: Any, title: str, subtitle: str) -> Any:
    """Wrap the banner body in the rounded card the session is known by.

    Imported here rather than at module import time for the same reason as the rest of the
    optional toolkit: ``rich`` is a ``[shell]`` extra and the one-shot commands must not need it.
    """
    from rich.box import ROUNDED
    from rich.panel import Panel

    return Panel(
        body,
        box=ROUNDED,
        border_style="q.rule",
        padding=(1, 2),
        title=text_class(title, style="q.title"),
        title_align="left",
        subtitle=text_class(subtitle, style="q.muted"),
        subtitle_align="right",
    )


def _render_outcome(outcome: CommandOutcome, state: ShellState, console: Any) -> None:
    """Record one outcome and report only what the command itself did not already print.

    A command that reported its own outcome prints nothing extra: the status line already carries
    the exit code, and repeating it here would put the same fact in two places. Only the two cases
    the command cannot have described - an interruption and an unexpected exception - are drawn.
    """
    state.last_exit_code = outcome.exit_code
    state.executed += 1
    if outcome.cancelled:
        console.print(
            "[q.warn]\u25b8 cancelled[/q.warn] [q.muted]\u2014 the session is unchanged[/q.muted]"
        )
        return
    if outcome.failure_type is not None:
        console.print(
            f"[q.fail]\u25b8 {outcome.failure_type}[/q.fail] "
            "[q.muted]ended the command before it could report[/q.muted]"
        )


def _is_nested_session(tokens: Sequence[str], commands: frozenset[str]) -> bool:
    """Report whether a line would start a second session inside this one.

    ``shell`` is a real command, so it has to be checked before the builtin branch rather than
    after it - the builtin branch deliberately defers to any registered command. Only the bare
    word is redirected: the outer session has already settled the history and banner options this
    line could carry, so running it again could not honour them.
    """
    return bool(tokens) and tokens[0] == SHELL_COMMAND and SHELL_COMMAND in commands


def _handle_builtin(name: str, *, dispatch: Callable[[Sequence[str]], int], console: Any) -> bool:
    """Run one session builtin. Return ``False`` when the session should end."""
    if name in {"exit", "quit"}:
        return False
    if name == "clear":
        console.clear()
        return True
    # ``help`` (and its ``?`` alias) is the command tree's own help text, so the session never
    # keeps a second copy of it that could drift from the commands actually installed.
    run_tokens(["--help"], dispatch=dispatch)
    return True


def run_shell_command(args: argparse.Namespace) -> int:
    """Run the interactive session until the user leaves it."""
    # Checked before the optional imports so a script that calls this gets the actionable message
    # rather than one about missing dependencies.
    if not sys.stdin.isatty():
        print(
            "Usage error: the shell needs a terminal; use the one-shot commands in a script.",
            file=sys.stderr,
        )
        return _EXIT_CONFIGURATION

    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.styles import Style
        from rich.console import Console
        from rich.table import Table
        from rich.text import Text
        from rich.theme import Theme
    except ImportError:
        print(_MISSING_DEPENDENCIES_MESSAGE, file=sys.stderr)
        return _EXIT_CONFIGURATION

    from qaneris.cli.main import main as dispatch

    quiesce_driver_logging()
    # Colour is turned off by rich itself when stdout is not a terminal or NO_COLOR is set, so the
    # palette needs no separate check here.
    console = Console(theme=Theme(_THEME), highlight=False)
    state = ShellState()
    commands = registered_commands()

    history_path = (
        None
        if args.no_history
        else prepare_history(args.history.expanduser() if args.history else _default_history_path())
    )
    history = FileHistory(str(history_path)) if history_path else InMemoryHistory()
    session = PromptSession(history=history, style=Style.from_dict(_PROMPT_STYLE))

    if not args.no_banner:
        _print_banner(console, Table, Text)

    while True:
        try:
            with patch_stdout():
                line = session.prompt(
                    _PROMPT,
                    prompt_continuation=_PROMPT_CONTINUATION,
                    bottom_toolbar=lambda: _toolbar_text(state),
                )
        except KeyboardInterrupt:
            # Ctrl-C at an empty prompt clears the line and keeps the session, like a shell.
            continue
        except EOFError:
            break

        line = line.strip()
        if not line:
            continue

        tokens = split_line(line)
        if tokens is None:
            console.print("[q.fail]\u25b8 cannot parse[/q.fail] [q.muted]unmatched quote[/q.muted]")
            continue

        tokens = normalize_tokens(tokens)
        if not tokens:
            # The program name on its own. The one-shot entry point answers this with its help
            # text, and the session does the same rather than reporting an unknown command.
            _handle_builtin("help", dispatch=dispatch, console=console)
            continue

        if _is_nested_session(tokens, commands):
            # ``shell`` inside the session would start a second session nested in this one: the
            # banner is drawn again, every builtin gains a second meaning, and leaving costs two
            # ``exit`` lines. The user is already in a session, so the only useful answer this line
            # can produce is the ``shell`` command's own help.
            _render_outcome(
                run_tokens([SHELL_COMMAND, "--help"], dispatch=dispatch), state, console
            )
            continue

        if tokens[0] in _BUILTINS and tokens[0] not in commands:
            if not _handle_builtin(tokens[0], dispatch=dispatch, console=console):
                break
            continue

        state.last_command = line
        _render_outcome(run_tokens(tokens, dispatch=dispatch), state, console)

    console.print()
    # The closing line reports where history actually went for *this* session: claiming a file
    # after ``--no-history``, or naming the default file after ``--history``, would both be wrong.
    # Built as text rather than markup: a history path is an argument the user chose, and a
    # directory whose name contains square brackets would otherwise be parsed as a style tag and
    # print a different path than the one actually written.
    farewell = Text("goodbye \u00b7 ", style="q.muted")
    if history_path is not None:
        farewell.append("history saved to ", style="q.muted")
        farewell.append(str(history_path), style="q.muted")
    else:
        farewell.append("no history was kept", style="q.muted")
    console.print(farewell)
    return _EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the standalone ``qaneris-shell`` console script.

    ``--env-file`` belongs to the top-level parser, so it is lifted in front of the subcommand
    instead of being left where the caller typed it - ``shell`` itself takes no such option, and
    passing it through unchanged would fail as an unrecognized argument.
    """
    from qaneris.cli.main import main as cli_main

    arguments = list(argv if argv is not None else sys.argv[1:])
    leading, rest = _extract_env_file(arguments)
    return cli_main([*leading, SHELL_COMMAND, *rest])


def _extract_env_file(arguments: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split ``--env-file`` out of ``arguments``, keeping its value attached."""
    leading: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--env-file" and index + 1 < len(arguments):
            leading.extend((argument, arguments[index + 1]))
            index += 2
            continue
        if argument.startswith("--env-file="):
            leading.append(argument)
            index += 1
            continue
        rest.append(argument)
        index += 1
    return leading, rest


if __name__ == "__main__":
    raise SystemExit(main())
