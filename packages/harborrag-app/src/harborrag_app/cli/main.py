"""HarborRAG command-line entry point."""

from __future__ import annotations

import copy
import logging
import os
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import click
import typer
from typer.core import TyperGroup

from harborrag_app import __version__ as app_version
from harborrag_app.cli.banner import print_banner
from harborrag_app.cli.commands import (
    auth,
    chat,
    connections,
    doctor,
    failures,
    ingestions,
    legacy,
    me,
    projects,
    settings,
    source_types,
)
from harborrag_app.cli.environment import load_project_environment
from harborrag_app.cli.globals import requested_output
from harborrag_app.cli.output import OutputFormat, print_error
from harborrag_app.cli.project import ProjectError, activate_from_argv, activate_legacy_checkout
from harborrag_app.cli.typer_app import new_typer
from harborrag_app.operations.errors import ErrorBody
from harborrag_core.observability.process_logging import (
    LEVEL_ENV_VAR,
    _ManagedStreamHandler,
    configure_logging,
)

logger = logging.getLogger("harborrag.app.cli.main")

_HELP_FLAGS = ("-h", "--help")
# Global options declared by `configure()` that consume the following argv item. main()
# scans argv before Click parses it, so `_command_name` has to skip those values itself;
# a value-taking option missing from this set would be read as the sub-command name.
# `test_global_value_options_are_all_declared` asserts this matches `configure()`.
_VALUE_TAKING_GLOBAL_OPTIONS = frozenset({"--project"})
# Commands that work without a project: `doctor` reports its absence; the removed `source` and
# `ingest` names only print their replacement.
_PROJECT_OPTIONAL_COMMANDS = frozenset({"doctor", "source", "ingest"})
_NO_PROJECT_MESSAGE = (
    "no project found. Run it inside a HarborRAG repository checkout, or pass "
    "--project DIR for a directory containing harborrag.yaml."
)

# One-shot commands render their own result envelope, so diagnostic logs stay
# off unless an operator asks for them. Logs go to stderr either way, which
# keeps `-o json` output on stdout parseable at any level.
_DEFAULT_CLI_LOG_LEVEL = "WARNING"

SETUP_PANEL = "Setup and diagnostics"
ACCESS_PANEL = "Access"
CHAT_PANEL = "Chat"
RESOURCES_PANEL = "Resources"
_PANEL_ORDER = (SETUP_PANEL, ACCESS_PANEL, CHAT_PANEL, RESOURCES_PANEL)


class _PanelOrderedGroup(TyperGroup):
    """A command group whose help lists the panels in ``_PANEL_ORDER``.

    Typer lists commands before sub-groups, which would put a panel of groups after every
    panel of commands; this keeps each panel's commands in registration order.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:
        """Command names ordered by panel, then by registration."""
        return sorted(super().list_commands(ctx), key=self._panel_rank)

    def _panel_rank(self, name: str) -> int:
        panel = getattr(self.commands[name], "rich_help_panel", None)
        return _PANEL_ORDER.index(panel) if panel in _PANEL_ORDER else len(_PANEL_ORDER)


app = new_typer(
    name="harborrag",
    cls=_PanelOrderedGroup,
    help="[bold cyan]Operate HarborRAG ingestion workflows.[/bold cyan]",
    epilog="[dim]Use 'harborrag COMMAND --help' for command-specific options.[/dim]",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 120},
)


def _print_version(requested: bool) -> None:
    """Print ``harborrag VERSION`` and exit 0 when ``--version`` was given.

    The version is that of the ``harborrag`` distribution, else of ``harborrag-app``, which
    owns the console script.
    """
    if not requested:
        return
    try:
        installed = distribution_version("harborrag")
    except PackageNotFoundError:
        installed = app_version
    typer.echo(f"harborrag {installed}")
    raise typer.Exit


@app.callback()
def configure(
    context: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Print the version and exit.",
        ),
    ] = False,
    no_color: Annotated[
        bool,
        typer.Option(
            "--no-color",
            help="Disable ANSI color in one-shot command output.",
        ),
    ] = False,
    project: Annotated[
        Path | None,
        typer.Option(
            "--project",
            metavar="DIR",
            help="Project directory holding harborrag.yaml (default: walk up from CWD).",
        ),
    ] = None,
) -> None:
    """Configure presentation shared by all HarborRAG commands."""

    del context, version, project  # project is consumed before Click ran; see main()
    if no_color:
        os.environ["NO_COLOR"] = "1"


app.command(
    "me",
    help="Show the principal, role and tenants the CLI acts with.",
    rich_help_panel=SETUP_PANEL,
)(me.command)
app.command(
    "doctor",
    help="Check project, configuration, environment, and local services.",
    rich_help_panel=SETUP_PANEL,
)(doctor.command)
app.add_typer(
    projects.app, name="projects", help="Manage projects.", rich_help_panel=RESOURCES_PANEL
)
app.add_typer(
    source_types.app,
    name="source-types",
    help="List source types.",
    rich_help_panel=RESOURCES_PANEL,
)
app.add_typer(
    connections.app, name="connections", help="Manage connections.", rich_help_panel=RESOURCES_PANEL
)
app.add_typer(
    ingestions.app,
    name="ingestions",
    help="Start, follow and control ingestion runs.",
    rich_help_panel=RESOURCES_PANEL,
)
app.add_typer(
    failures.app,
    name="failures",
    help="List and retry failed items.",
    rich_help_panel=RESOURCES_PANEL,
)
app.add_typer(legacy.SOURCE_APP, name="source", hidden=True)
app.add_typer(legacy.INGEST_APP, name="ingest", hidden=True)
app.add_typer(
    settings.app,
    name="settings",
    help="Read, change, import and export settings.",
    rich_help_panel=RESOURCES_PANEL,
)
app.add_typer(
    auth.app,
    name="auth",
    help="Manage MCP reader keys (auth keys create|list|revoke).",
    rich_help_panel=ACCESS_PANEL,
)
app.command(
    "chat",
    help="Generate a response with the configured chat model.",
    rich_help_panel=CHAT_PANEL,
)(chat.command)


def main(argv: list[str] | None = None) -> int:
    """Run the Typer application and expose a test-friendly integer exit code."""

    args = list(argv) if argv is not None else sys.argv[1:]
    # Before logging is configured and before any command builds a service: a project's
    # harborrag.yaml and .env, then the checkout's env files, must be in os.environ.
    notice: str | None = None
    if _activates_project(args):
        origin = Path.cwd()
        try:
            project = activate_from_argv(args)
            checkout = (
                activate_legacy_checkout() if project is None and _needs_catalogs(args) else None
            )
        except ProjectError as exc:
            _report_project_failure(str(exc), args)
            return 1
        # Never configure a run from a directory the user cannot see they are in.
        if project is not None:
            if project.root != origin.resolve():
                notice = f"harborrag: using project {project.root}"
        elif checkout is not None:
            if checkout != origin.resolve():
                notice = f"harborrag: using repository checkout {checkout}"
        elif _requires_project(args):
            # Fail here with guidance instead of letting a catalog loader explain a missing
            # file in Docker-image terms to someone who just ran `pip install harborrag`.
            _report_project_failure(_NO_PROJECT_MESSAGE, args)
            return 1
    _load_environment(args)
    _configure_cli_logging(os.environ.get(LEVEL_ENV_VAR, _DEFAULT_CLI_LOG_LEVEL))
    _announce(notice)
    if not args or args[0] in _HELP_FLAGS:
        # Click resolves --help and no_args_is_help before the group callback
        # runs, so the banner can't live in configure(); print it here instead.
        print_banner()
    try:
        app(args=argv, prog_name="harborrag")
    except SystemExit as exc:
        return _exit_code(exc.code)
    return 0


def _report_project_failure(message: str, args: list[str]) -> None:
    """Print why there is no usable project: plain text in table mode, else the error envelope.

    The envelope carries ``500 internal_error``, the status of exit code 1, as a failed local
    setup does.
    """
    output = requested_output(args, os.environ, stdout_is_tty=sys.stdout.isatty())
    if output is OutputFormat.TABLE:
        print(f"harborrag: {message}", file=sys.stderr)
        return
    print_error(ErrorBody(500, "internal_error", message), output=output, trace_id=uuid4().hex)


def _announce(notice: str | None) -> None:
    """Tell a person at a terminal which directory the command runs from; else log it.

    Off stderr away from a terminal: a script reads stderr as the command's error report, so
    a notice there would break parsing it. The INFO record leaves a trace in the CLI's logs.
    ``None`` means the command runs from where the user stands, so there is nothing to say.
    """
    if notice is None:
        return
    if sys.stderr.isatty():
        print(notice, file=sys.stderr)
    else:
        logger.info(notice)


def _command_name(args: list[str]) -> str | None:
    """The sub-command being invoked, ignoring global options and their values."""

    rest = list(args)
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        if option in _VALUE_TAKING_GLOBAL_OPTIONS and rest:
            rest.pop(0)  # its value
    return rest[0] if rest else None


def _requests_version(args: list[str]) -> bool:
    """True when ``--version`` is among the global options before the command name."""

    rest = list(args)
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        if option == "--version":
            return True
        if option in _VALUE_TAKING_GLOBAL_OPTIONS and rest:
            rest.pop(0)
    return False


def _activates_project(args: list[str]) -> bool:
    """Everything except pure help and version output runs inside the enclosing project."""
    return not any(arg in _HELP_FLAGS for arg in args) and not _requests_version(args)


def _load_environment(args: list[str]) -> None:
    """Load the project's env files, except for ``--version``, which needs none."""
    if not _requests_version(args):
        load_project_environment()


def _needs_catalogs(args: list[str]) -> bool:
    """True when the command reads catalogs: every project command, and ``doctor``."""

    return _command_name(args) == "doctor" or _requires_project(args)


def _requires_project(args: list[str]) -> bool:
    """True for a real command invocation that needs catalogs (not help or doctor)."""

    command = _command_name(args)
    return (
        command is not None
        and command not in _PROJECT_OPTIONAL_COMMANDS
        and command in _registered_names()
    )


def _registered_names() -> frozenset[str]:
    """Names of every top-level command and group, so unknown ones skip project lookup."""

    names = {
        command.name or getattr(command.callback, "__name__", "")
        for command in app.registered_commands
    }
    names.update(group.name for group in app.registered_groups if group.name)
    return frozenset(names)


_TRACEBACK_HINT = " (set HARBORRAG_LOG_LEVEL=DEBUG for the traceback)"


class _QuietTracebackFilter(logging.Filter):
    """Drop `exc_info` above DEBUG so a failure is one line, not a stack dump.

    Returns a shortened copy instead of editing the record: every handler sees the
    same LogRecord, and the others (a second sink, pytest's caplog) keep the traceback.
    """

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        if record.exc_info and logging.getLogger("harborrag").getEffectiveLevel() > logging.DEBUG:
            quiet = copy.copy(record)
            quiet.msg = record.getMessage() + _TRACEBACK_HINT
            quiet.args = None
            quiet.exc_info = None
            quiet.exc_text = None
            return quiet
        return True


_QUIET_TRACEBACKS = _QuietTracebackFilter()


def _configure_cli_logging(level: str) -> logging.Logger:
    logger = configure_logging(level)
    for handler in logger.handlers:
        # Only the handler configure_logging owns; others (e.g. a test's capture) keep tracebacks.
        if isinstance(handler, _ManagedStreamHandler):
            handler.addFilter(_QUIET_TRACEBACKS)  # a no-op when already installed
    return logger


def _exit_code(value: Any) -> int:
    return value if isinstance(value, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())
