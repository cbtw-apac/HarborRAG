"""The global options every spec command takes, and how they resolve.

Each option is declared once here as an ``Annotated`` alias that a command lists among its
parameters, so the options stay per-command and ``main()``'s argv scan is unaffected.
A flag beats its environment variable, which beats the profile, which beats the built-in
default. These options carry what an HTTP call carries in its headers.

Start from: current_globals (a command) or resolve_globals.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import click
import typer

from harborrag_app.cli.output import OutputFormat
from harborrag_app.cli.profiles import DEFAULT_CONFIG_PATH, DEFAULT_PROFILE, load_profile

DEFAULT_TENANT_ID = "DEFAULT"
TENANT_ENV = "HARBORRAG_TENANT_ID"
OUTPUT_ENV = "HARBORRAG_OUTPUT"
PROFILE_ENV = "HARBORRAG_PROFILE"
_PANEL = "Global options"

TenantOption = Annotated[
    str | None,
    typer.Option(
        "--tenant",
        metavar="TENANT_ID",
        help=f"Tenant to act in (env {TENANT_ENV}; default: the profile's, else DEFAULT).",
        rich_help_panel=_PANEL,
    ),
]
OutputOption = Annotated[
    OutputFormat | None,
    typer.Option(
        "--output",
        "-o",
        case_sensitive=False,
        help=f"Print a table, JSON or YAML (env {OUTPUT_ENV}; default: table in a terminal, "
        "else json).",
        rich_help_panel=_PANEL,
    ),
]
ProfileOption = Annotated[
    str | None,
    typer.Option(
        "--profile",
        metavar="NAME",
        help=f"Profile of the config file to use (env {PROFILE_ENV}; default: default).",
        rich_help_panel=_PANEL,
    ),
]
ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        metavar="PATH",
        exists=True,
        dir_okay=False,
        help=f"Config file holding the profiles (default: {DEFAULT_CONFIG_PATH}).",
        rich_help_panel=_PANEL,
    ),
]
YesOption = Annotated[
    bool,
    typer.Option(
        "--yes",
        "-y",
        help="Answer yes to confirmation questions, for scripts.",
        rich_help_panel=_PANEL,
    ),
]


@dataclass(frozen=True, slots=True)
class Globals:
    """The global options of one invocation, resolved.

    Args:
        tenant: Tenant the command acts in.
        output: Format the command prints its body and errors in.
        yes: Skip confirmation questions.
        profile: Name of the profile that was read.
    """

    tenant: str
    output: OutputFormat
    yes: bool
    profile: str


def resolve_globals(  # noqa: PLR0913 - one keyword per global option and its context
    *,
    tenant: str | None,
    output: OutputFormat | None,
    profile: str | None,
    config: Path | None,
    yes: bool,
    environ: Mapping[str, str],
    stdout_is_tty: bool,
) -> Globals:
    """Resolve each option from its flag, environment variable, profile or default.

    Args:
        tenant: ``--tenant``, ``None`` when not given.
        output: ``-o/--output``, ``None`` when not given.
        profile: ``--profile``, ``None`` when not given.
        config: ``--config``; ``None`` reads ``DEFAULT_CONFIG_PATH``.
        yes: ``-y/--yes``.
        environ: Process environment; an empty value counts as unset.
        stdout_is_tty: Whether stdout is a terminal, which makes ``table`` the default.

    Returns:
        The options this invocation runs with.

    Raises:
        ProfileError: The config file or the chosen profile cannot be used.
        click.UsageError: ``HARBORRAG_OUTPUT`` is not table, json or yaml.
    """
    name = profile or environ.get(PROFILE_ENV) or DEFAULT_PROFILE
    chosen = load_profile(config or DEFAULT_CONFIG_PATH, name)
    terminal_default = OutputFormat.TABLE if stdout_is_tty else OutputFormat.JSON
    return Globals(
        tenant=tenant or environ.get(TENANT_ENV) or chosen.tenant or DEFAULT_TENANT_ID,
        output=output or _environment_output(environ) or chosen.output or terminal_default,
        yes=yes,
        profile=name,
    )


def current_globals(  # noqa: PLR0913 - one keyword per global option
    *,
    tenant: str | None,
    output: OutputFormat | None,
    profile: str | None,
    config: Path | None,
    yes: bool,
) -> Globals:
    """Resolve the options of this process: its environment and whether stdout is a terminal."""
    return resolve_globals(
        tenant=tenant,
        output=output,
        profile=profile,
        config=config,
        yes=yes,
        environ=os.environ,
        stdout_is_tty=sys.stdout.isatty(),
    )


def _environment_output(environ: Mapping[str, str]) -> OutputFormat | None:
    """``HARBORRAG_OUTPUT`` as a format, ``None`` when unset."""
    value = environ.get(OUTPUT_ENV, "").strip().lower()
    if not value:
        return None
    if value not in set(OutputFormat):
        raise click.UsageError(f"{OUTPUT_ENV} must be one of {', '.join(OutputFormat)}")
    return OutputFormat(value)
