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
_BUILTINS = ("exit", "quit", "help", "clear")

#: Where the session remembers its input. ``.tools/`` is this repository's gitignored runtime
#: directory, already excluded from version control.
_HISTORY_DIRECTORY = ".tools"
_HISTORY_FILENAME = "shell_history"
_HISTORY_DIRECTORY_MODE = 0o700
_HISTORY_FILE_MODE = 0o600

_TOOLBAR_COMMAND_LIMIT = 60

#: Third-party loggers that describe the database server instead of this command. Raising them to
#: ERROR keeps the prompt readable; they are not part of the product's own output contract.
_QUIESCED_LOGGERS = ("neo4j.notifications",)

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


def _toolbar_text(state: ShellState) -> list[tuple[str, str]]:
    """Build the status line as ``(style, text)`` pairs.

    Pairs rather than markup: the last command is user input, and a markup string would render a
    bracketed value as styling instead of as the text the user typed.
    """
    command = state.last_command
    if len(command) > _TOOLBAR_COMMAND_LIMIT:
        command = command[: _TOOLBAR_COMMAND_LIMIT - 3] + "..."
    code = "-" if state.last_exit_code is None else str(state.last_exit_code)
    return [
        ("bold", " last "),
        ("", command),
        ("bold", "  exit "),
        ("", code),
        ("bold", "  commands "),
        ("", str(state.executed)),
        ("italic", "  ? help · Ctrl-D exit"),
    ]


def _print_banner(console: Any, table_class: Any, panel_class: Any) -> None:
    """Draw the welcome panel. Presentation only: it states no capability of its own."""
    from qaneris import __version__

    grid = table_class.grid(padding=(0, 3))
    grid.add_column(justify="left")
    grid.add_column(justify="left")
    grid.add_row(
        f"[bold]Qaneris[/bold] {__version__}\n\n[dim]interactive session[/dim]",
        "[bold]Getting started[/bold]\n"
        "Type a command, or [cyan]help[/cyan] for the full list.\n"
        '[cyan]doctor[/cyan] · [cyan]source list[/cyan] · [cyan]ask "..."[/cyan]\n\n'
        "[bold]Keys[/bold]\n"
        "Ctrl-C cancels the line · Ctrl-D exits",
    )
    console.print(panel_class(grid, title="qaneris shell", border_style="cyan", expand=False))


def _render_outcome(outcome: CommandOutcome, state: ShellState, console: Any) -> None:
    """Record one outcome and report only what the command itself did not already print."""
    state.last_exit_code = outcome.exit_code
    state.executed += 1
    if outcome.cancelled:
        console.print("[yellow]Cancelled.[/yellow]")
        return
    if outcome.failure_type is not None:
        console.print(
            f"[red]{outcome.failure_type}[/red] ended the command before it could report. "
            "The command's own messages, if any, are above."
        )


def _handle_builtin(name: str, *, dispatch: Callable[[Sequence[str]], int], console: Any) -> bool:
    """Run one session builtin. Return ``False`` when the session should end."""
    if name in {"exit", "quit"}:
        return False
    if name == "clear":
        console.clear()
        return True
    # ``help`` is the command tree's own help text, so the session never keeps a second copy of it.
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
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
    except ImportError:
        print(_MISSING_DEPENDENCIES_MESSAGE, file=sys.stderr)
        return _EXIT_CONFIGURATION

    from qaneris.cli.main import main as dispatch

    quiesce_driver_logging()
    console = Console()
    state = ShellState()
    commands = registered_commands()

    history_path = (
        None
        if args.no_history
        else prepare_history(args.history.expanduser() if args.history else _default_history_path())
    )
    history = FileHistory(str(history_path)) if history_path else InMemoryHistory()
    session = PromptSession(history=history)

    if not args.no_banner:
        _print_banner(console, Table, Panel)

    while True:
        try:
            with patch_stdout():
                line = session.prompt("qaneris> ", bottom_toolbar=lambda: _toolbar_text(state))
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
            console.print("[red]Cannot parse line:[/red] unmatched quote.")
            continue

        if tokens[0] in _BUILTINS and tokens[0] not in commands:
            if not _handle_builtin(tokens[0], dispatch=dispatch, console=console):
                break
            continue

        state.last_command = line
        _render_outcome(run_tokens(tokens, dispatch=dispatch), state, console)

    console.print("[dim]bye[/dim]")
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
