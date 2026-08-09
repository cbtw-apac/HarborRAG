"""HarborRAG command-line entry point."""

from __future__ import annotations

import os
import sys
from typing import Annotated, Any

import typer

from harborrag_app.cli.banner import print_banner
from harborrag_app.cli.commands import chat, doctor, ingest, retrieve
from harborrag_app.cli.environment import load_project_environment
from harborrag_app.cli.runner import CliState
from harborrag_core.observability.process_logging import LEVEL_ENV_VAR, configure_logging

_HELP_FLAGS = ("-h", "--help")

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
) -> None:
    """Configure presentation shared by all HarborRAG commands."""

    context.obj = CliState(no_color=no_color)


app.command(
    "doctor",
    help="Check Temporal runtime readiness.",
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

    # Before logging is configured and before any command builds a service: connector
    # and model credentials live in the project's env files, and every command that
    # resolves a connector needs them present in os.environ.
    load_project_environment()
    configure_logging(os.environ.get(LEVEL_ENV_VAR, _DEFAULT_CLI_LOG_LEVEL))
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args or args[0] in _HELP_FLAGS:
        # Click resolves --help and no_args_is_help before the group callback
        # runs, so the banner can't live in configure(); print it here instead.
        print_banner()
    try:
        app(args=argv, prog_name="harborrag")
    except SystemExit as exc:
        return _exit_code(exc.code)
    return 0


def _exit_code(value: Any) -> int:
    return value if isinstance(value, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())
