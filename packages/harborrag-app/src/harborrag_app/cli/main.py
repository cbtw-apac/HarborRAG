"""HarborRAG command-line entry point."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from harborrag_app.cli.commands import doctor, ingest, status
from harborrag_app.cli.runner import CliState

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
app.command(
    "status",
    help="Query an ingestion run (short alias).",
    rich_help_panel="Operations",
)(status.command)
app.add_typer(
    ingest.app,
    name="ingest",
    help="Submit, observe, and control ingestion runs.",
    rich_help_panel="Ingestion",
)


def main(argv: list[str] | None = None) -> int:
    """Run the Typer application and expose a test-friendly integer exit code."""

    try:
        app(args=argv, prog_name="harborrag")
    except SystemExit as exc:
        return _exit_code(exc.code)
    return 0


def _exit_code(value: Any) -> int:
    return value if isinstance(value, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())
