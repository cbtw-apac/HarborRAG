"""ASCII banner shown at the HarborRAG CLI entry point."""

from __future__ import annotations

from rich.console import Console

_LOGO = r"""
██╗  ██╗ █████╗ ██████╗ ██████╗  ██████╗ ██████╗ ██████╗  █████╗  ██████╗
██║  ██║██╔══██╗██╔══██╗██╔══██╗██╔═══██╗██╔══██╗██╔══██╗██╔══██╗██╔════╝
███████║███████║██████╔╝██████╔╝██║   ██║██████╔╝██████╔╝███████║██║  ███╗
██╔══██║██╔══██║██╔══██╗██╔══██╗██║   ██║██╔══██╗██╔══██╗██╔══██║██║   ██║
██║  ██║██║  ██║██║  ██║██████╔╝╚██████╔╝██║  ██║██║  ██║██║  ██║╚██████╔╝
╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝
""".strip("\n")

_TAGLINE = "⚓ Operate HarborRAG ingestion workflows"


def print_banner(*, console: Console | None = None) -> None:
    """Print the HARBORRAG ASCII banner above the CLI's help output."""

    console = console or Console(highlight=False, soft_wrap=False)
    console.print(_LOGO, style="bold cyan")
    console.print(_TAGLINE, style="dim cyan")
    console.print()
