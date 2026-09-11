"""HarborRAG command-line entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from harborrag_app.cli.banner import print_banner
from harborrag_app.cli.commands import chat, doctor, ingest, init, retrieve
from harborrag_app.cli.environment import load_project_environment
from harborrag_app.cli.project import ProjectError, activate_from_argv, activate_legacy_checkout
from harborrag_app.cli.runner import CliState
from harborrag_core.observability.process_logging import LEVEL_ENV_VAR, configure_logging

_HELP_FLAGS = ("-h", "--help")
# Commands that work without a project: `init` creates one, `doctor` reports its absence.
_PROJECT_OPTIONAL_COMMANDS = frozenset({"init", "doctor"})
_NO_PROJECT_MESSAGE = (
    "harborrag: no project found. Run `harborrag init DIR` to create one, cd into a "
    "directory containing harborrag.yaml, or pass --project DIR."
)

# One-shot commands render their own result envelope, so diagnostic logs stay
# off unless an operator asks for them. Logs go to stderr either way, which
# keeps `--json` output on stdout parseable at any level.
_DEFAULT_CLI_LOG_LEVEL = "WARNING"

app = typer.Typer(
    name="harborrag",
    help="[bold cyan]Operate HarborRAG ingestion workflows.[/bold cyan]",
    epilog="[dim]Use 'harborrag ingest ACTION --help' for action-specific options.[/dim]",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 120},
)


@app.callback()
def configure(
    context: typer.Context,
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

    del project  # consumed before Click ran; see main()
    context.obj = CliState(no_color=no_color)


app.command(
    "init",
    help="Scaffold a project directory: harborrag.yaml, .env, config/, docker-compose.yml.",
    rich_help_panel="Setup",
)(init.command)
app.command(
    "doctor",
    help="Check project, configuration, environment, and local services.",
    rich_help_panel="Operations",
)(doctor.command)
app.add_typer(
    ingest.app,
    name="ingest",
    help="Submit, observe, and control ingestion runs.",
    rich_help_panel="Ingestion",
)
app.command(
    "chat",
    help="Generate a response with the configured chat model.",
    rich_help_panel="Chat",
)(chat.command)
app.command(
    "retrieve",
    help="Search active Qdrant vectors with FalkorDB context expansion.",
    rich_help_panel="Retrieval",
)(retrieve.command)


def main(argv: list[str] | None = None) -> int:
    """Run the Typer application and expose a test-friendly integer exit code."""

    args = list(argv) if argv is not None else sys.argv[1:]
    # Before logging is configured and before any command builds a service: a project's
    # harborrag.yaml and .env, then the checkout's env files, must be in os.environ.
    # `init` creates a project and resolves its DIR argument from the caller's CWD, so it
    # is the one command that never activates an enclosing project.
    if _activates_project(args):
        origin = Path.cwd()
        try:
            project = activate_from_argv(args)
            # A checkout supplies catalogs without a marker file, so it is only consulted
            # once marker discovery has come up empty -- and only for a command that needs
            # catalogs, so `doctor` keeps reporting on the directory the user is in.
            checkout = (
                activate_legacy_checkout() if project is None and _requires_project(args) else None
            )
        except ProjectError as exc:
            print(f"harborrag: {exc}", file=sys.stderr)
            return 1
        # Never configure a run from a directory the user cannot see they are in.
        if project is not None:
            if project.root != origin.resolve():
                print(f"harborrag: using project {project.root}", file=sys.stderr)
        elif checkout is not None:
            if checkout != origin.resolve():
                print(f"harborrag: using repository checkout {checkout}", file=sys.stderr)
        elif _requires_project(args):
            # Fail here with guidance instead of letting a catalog loader explain a missing
            # file in Docker-image terms to someone who just ran `pip install harborrag`.
            print(_NO_PROJECT_MESSAGE, file=sys.stderr)
            return 1
    load_project_environment()
    configure_logging(os.environ.get(LEVEL_ENV_VAR, _DEFAULT_CLI_LOG_LEVEL))
    if not args or args[0] in _HELP_FLAGS:
        # Click resolves --help and no_args_is_help before the group callback
        # runs, so the banner can't live in configure(); print it here instead.
        print_banner()
    try:
        app(args=argv, prog_name="harborrag")
    except SystemExit as exc:
        return _exit_code(exc.code)
    return 0


def _command_name(args: list[str]) -> str | None:
    """The sub-command being invoked, ignoring global options and their values."""

    rest = list(args)
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        if option == "--project" and rest:
            rest.pop(0)  # its value
    return rest[0] if rest else None


def _activates_project(args: list[str]) -> bool:
    """Everything except `init` and pure help output runs inside the enclosing project."""

    if any(arg in _HELP_FLAGS for arg in args):
        return False
    return _command_name(args) != "init"


def _requires_project(args: list[str]) -> bool:
    """True for a real command invocation that needs catalogs (not help, init, or doctor)."""

    command = _command_name(args)
    return command is not None and command not in _PROJECT_OPTIONAL_COMMANDS


def _exit_code(value: Any) -> int:
    return value if isinstance(value, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())
