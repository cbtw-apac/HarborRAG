"""Scaffold a HarborRAG project directory."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

from harborrag_app.cli.commands.init_support import (
    report,
    seed_source_folder,
    select_ports_offset,
)
from harborrag_app.scaffold import (
    DEFAULT_PROVIDER,
    DEFAULT_SOURCE,
    PRESETS,
    SOURCES,
    InitOptions,
    ScaffoldExistsError,
    SourceVariable,
    build_scaffold,
    existing_scaffold_files,
    parse_sources,
    write_scaffold,
)

_PROVIDER_CHOICES = sorted(PRESETS)


def command(  # noqa: PLR0913 - Typer requires one parameter per public option
    directory: Annotated[
        Path,
        typer.Argument(metavar="DIR", help="Project directory to create or fill (default: .)."),
    ] = Path(),
    provider: Annotated[
        str | None,
        typer.Option("--provider", help=f"Model provider: {', '.join(_PROVIDER_CHOICES)}."),
    ] = None,
    chat_model: Annotated[
        str | None,
        typer.Option("--chat-model", help="Chat model name (provider default)."),
    ] = None,
    embed_model: Annotated[
        str | None,
        typer.Option("--embed-model", help="Embedding model name (provider default)."),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Provider API key; may be filled in later."),
    ] = None,
    api_base: Annotated[
        str | None,
        typer.Option("--api-base", help="Endpoint URL for azure-openai / openai-compatible."),
    ] = None,
    embed_dimensions: Annotated[
        int | None,
        typer.Option("--embed-dimensions", min=1, help="Embedding vector size (provider default)."),
    ] = None,
    ports_offset: Annotated[
        int | None,
        typer.Option(
            "--ports-offset",
            min=0,
            max=50000,
            help="Add N to every published service port (default: first free of 0/10000/20000).",
        ),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", metavar="PATH", help="Local folder to ingest (default: ./docs)."),
    ] = None,
    connectors: Annotated[
        str | None,
        typer.Option(
            "--connectors",
            metavar="LIST",
            help=f"Data sources to scaffold, comma-separated: {', '.join(SOURCES)} (default: local).",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Accept defaults; never prompt."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite existing scaffold files (never .env)."),
    ] = False,
) -> None:
    """Create harborrag.yaml, .env, config/ catalogs, and docker-compose.yml."""

    console = Console(highlight=False)
    errors = Console(stderr=True, highlight=False)
    root = directory.expanduser().resolve()
    interactive = not yes and sys.stdin.isatty()
    if provider is not None and provider not in PRESETS:
        raise typer.BadParameter(
            f"choose one of {', '.join(_PROVIDER_CHOICES)}", param_hint="--provider"
        )
    force = _confirm_overwrite(root, force=force, interactive=interactive, errors=errors)
    try:
        sources = _select_sources(connectors, interactive=interactive)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--connectors") from None
    offset, port_notice = select_ports_offset(ports_offset)
    try:
        options = _collect(
            root,
            provider=provider,
            chat_model=chat_model,
            embed_model=embed_model,
            api_key=api_key,
            api_base=api_base,
            embed_dimensions=embed_dimensions,
            source=source,
            interactive=interactive,
            ports_offset=offset,
            sources=sources,
        )
        files = build_scaffold(options, created_at=datetime.now(UTC).date().isoformat())
        root.mkdir(parents=True, exist_ok=True)
        written = write_scaffold(root, files, force=force)
        sample = seed_source_folder(root, options.source_path) if options.has_local_source else None
    except (ScaffoldExistsError, ValueError) as exc:
        errors.print(f"[red]harborrag init:[/] {exc}")
        raise typer.Exit(1) from None
    report(console, root, written, options, sample=sample)
    if port_notice:
        console.print(port_notice)


def _collect(  # noqa: PLR0913 - one parameter per prompt
    root: Path,
    *,
    provider: str | None,
    chat_model: str | None,
    embed_model: str | None,
    api_key: str | None,
    api_base: str | None,
    embed_dimensions: int | None,
    source: str | None,
    interactive: bool,
    ports_offset: int,
    sources: tuple[str, ...],
) -> InitOptions:
    if provider is None:
        provider = (
            str(Prompt.ask("Model provider", choices=_PROVIDER_CHOICES, default=DEFAULT_PROVIDER))
            if interactive
            else DEFAULT_PROVIDER
        )
    chosen_provider: str = provider
    preset = PRESETS[chosen_provider]
    chat_model = chat_model or _ask(interactive, "Chat model", preset.default_chat_model)
    embed_model = embed_model or _ask(interactive, "Embedding model", preset.default_embed_model)
    resolved_key: str = api_key if api_key is not None else ""
    if api_key is None and interactive:
        resolved_key = str(
            Prompt.ask(
                f"{preset.credential_variable} (blank to fill in later)",
                password=True,
                default="",
            )
        )
    if preset.requires_api_base and api_base is None and interactive:
        api_base = Prompt.ask("Endpoint URL (--api-base)")
    # Always default to a sub-folder: ingesting the project root itself would index
    # nothing useful in a fresh directory and confuse the first run.
    if "local" in sources:
        source = source or _ask(interactive, "Local folder to ingest", "./docs")
    source_values = _collect_source_values(sources, interactive=interactive)
    return InitOptions(
        provider=chosen_provider,
        chat_model=chat_model,
        embed_model=embed_model,
        api_key=resolved_key,
        api_base=api_base,
        source_path=source or "./docs",
        embed_dimensions=embed_dimensions,
        ports_offset=ports_offset,
        sources=sources,
        source_values=source_values,
    )


def _confirm_overwrite(root: Path, *, force: bool, interactive: bool, errors: Console) -> bool:
    """Ask before overwriting an existing scaffold; only `--force` skips the question.

    Asked before any other prompt so a declined overwrite costs no typing. Non-interactive
    runs (``--yes`` or no TTY) keep refusing: accepting defaults must never overwrite.
    """

    existing = existing_scaffold_files(root)
    if force or not existing:
        return force
    if not interactive:
        errors.print(
            f"[red]harborrag init:[/] {root} already contains {', '.join(existing)}; "
            "pass --force to overwrite"
        )
        raise typer.Exit(1)
    errors.print(f"{root} already contains: {', '.join(existing)}")
    if Confirm.ask(
        "Overwrite these files? (.env is kept; a fresh copy goes to .env.new)", default=False
    ):
        return True
    errors.print("Left the existing files untouched.")
    raise typer.Exit(1)


def _select_sources(connectors: str | None, *, interactive: bool) -> tuple[str, ...]:
    if connectors is not None:
        return parse_sources(connectors)
    if not interactive:
        return (DEFAULT_SOURCE,)
    choices = ", ".join(SOURCES)
    while True:
        answer = str(
            Prompt.ask(
                f"Data sources to ingest (comma-separated: {choices})", default=DEFAULT_SOURCE
            )
        )
        try:
            return parse_sources(answer)
        except ValueError as exc:
            Console(stderr=True).print(f"[red]{exc}[/]")


def _collect_source_values(sources: tuple[str, ...], *, interactive: bool) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in sources:
        for variable in SOURCES[key].variables:
            values[variable.name] = _ask_variable(variable, interactive=interactive)
    return values


def _ask_variable(variable: SourceVariable, *, interactive: bool) -> str:
    if not interactive:
        return variable.default
    return str(Prompt.ask(variable.label, password=variable.secret, default=variable.default))


def _ask(interactive: bool, label: str, default: str) -> str:
    return str(Prompt.ask(label, default=default)) if interactive else default
