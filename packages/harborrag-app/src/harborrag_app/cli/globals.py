"""The global options every spec command takes, and how they resolve.

Each option is declared once here as an ``Annotated`` alias that a command lists among its
parameters, so the options stay per-command and ``main()``'s argv scan is unaffected.
A flag beats its environment variable, which beats the profile, which beats the built-in
default. These options carry what an HTTP call carries in its headers.

Start from: current_globals (a command), resolve_globals, or requested_output (a failure
reported before Click parses the arguments).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import click
import typer

from harborrag_app.cli.environment import stack_tenant_id
from harborrag_app.cli.output import OutputFormat
from harborrag_app.cli.profiles import DEFAULT_CONFIG_PATH, DEFAULT_PROFILE, load_profile
from harborrag_app.cli.project import invocation_path
from harborrag_core.domain.identity import DEFAULT_TENANT_ID

TENANT_ENV = "HARBORRAG_TENANT_ID"
OUTPUT_ENV = "HARBORRAG_OUTPUT"
PROFILE_ENV = "HARBORRAG_PROFILE"
GLOBAL_OPTIONS_PANEL = "Global options"
_OUTPUT_FLAGS = ("--output", "-o")
_END_OF_OPTIONS = "--"

TenantOption = Annotated[
    str | None,
    typer.Option(
        "--tenant",
        metavar="TENANT_ID",
        help=(
            f"Tenant to act in (env {TENANT_ENV}; default: the profile's, else the tenant in "
            "env/.env.api, else DEFAULT)."
        ),
        rich_help_panel=GLOBAL_OPTIONS_PANEL,
    ),
]
OutputOption = Annotated[
    OutputFormat | None,
    typer.Option(
        *_OUTPUT_FLAGS,
        case_sensitive=False,
        help=f"Print a table, JSON or YAML (env {OUTPUT_ENV}; default: table in a terminal, "
        "else json).",
        rich_help_panel=GLOBAL_OPTIONS_PANEL,
    ),
]
ProfileOption = Annotated[
    str | None,
    typer.Option(
        "--profile",
        metavar="NAME",
        help=f"Profile of the config file to use (env {PROFILE_ENV}; default: default).",
        rich_help_panel=GLOBAL_OPTIONS_PANEL,
    ),
]
ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        metavar="PATH",
        callback=invocation_path(exists=True, dir_okay=False),
        help=f"Config file holding the profiles (default: {DEFAULT_CONFIG_PATH}).",
        rich_help_panel=GLOBAL_OPTIONS_PANEL,
    ),
]
YesOption = Annotated[
    bool,
    typer.Option(
        "--yes",
        "-y",
        help="Answer yes to confirmation questions, for scripts.",
        rich_help_panel=GLOBAL_OPTIONS_PANEL,
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
    stack_tenant: str | None = None,
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
        stack_tenant: Tenant of the stack's env file; the tenant when no flag, variable or
            profile names one, ahead of ``DEFAULT``.

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
        tenant=(
            tenant or environ.get(TENANT_ENV) or chosen.tenant or stack_tenant or DEFAULT_TENANT_ID
        ),
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
        stack_tenant=stack_tenant_id(),
    )


def requested_output(
    args: Sequence[str], environ: Mapping[str, str], *, stdout_is_tty: bool
) -> OutputFormat:
    """Resolve the output format from raw arguments, for a failure before Click parses them.

    The last ``-o/--output`` wins, then ``HARBORRAG_OUTPUT``, then the terminal default. The
    profile is not read, as loading it can fail too. A value that names no format counts as
    absent.

    Args:
        args: Command-line arguments after the program name.
        environ: Process environment.
        stdout_is_tty: Whether stdout is a terminal, which makes ``table`` the default.

    Returns:
        The format to print the failure in.
    """
    terminal_default = OutputFormat.TABLE if stdout_is_tty else OutputFormat.JSON
    return _flagged_output(args) or _output_format(environ.get(OUTPUT_ENV, "")) or terminal_default


def _flagged_output(args: Sequence[str]) -> OutputFormat | None:
    """Return the format the last ``-o/--output`` before ``--`` names, ``None`` when none does."""
    long_flag, short_flag = _OUTPUT_FLAGS
    found: OutputFormat | None = None
    remaining = iter(args)
    for arg in remaining:
        if arg == _END_OF_OPTIONS:
            break
        if arg in _OUTPUT_FLAGS:
            value = next(remaining, "")
        elif arg.startswith(f"{long_flag}="):
            value = arg.partition("=")[2]
        elif arg.startswith(short_flag):
            value = arg.removeprefix(short_flag)
        else:
            continue
        found = _output_format(value) or found
    return found


def _output_format(value: str) -> OutputFormat | None:
    """``value`` as a format, ignoring case and surrounding spaces; ``None`` when it names none."""
    normalized = value.strip().lower()
    return OutputFormat(normalized) if normalized in set(OutputFormat) else None


def _environment_output(environ: Mapping[str, str]) -> OutputFormat | None:
    """``HARBORRAG_OUTPUT`` as a format, ``None`` when unset."""
    value = environ.get(OUTPUT_ENV, "").strip().lower()
    if not value:
        return None
    if value not in set(OutputFormat):
        raise click.UsageError(f"{OUTPUT_ENV} must be one of {', '.join(OutputFormat)}")
    return OutputFormat(value)
