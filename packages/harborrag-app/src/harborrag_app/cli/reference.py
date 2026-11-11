"""The CLI command reference page, rendered from the Click command tree.

Owns the Markdown of ``docs/users/cli-reference/commands.md``: an intro, then one section per
visible command in path order with its help, usage line and a table of its arguments and
options. The page depends only on how the commands are declared, never on the terminal width,
color or environment, so ``make cli-reference`` rewrites it byte for byte and a test fails when
it is stale. Hidden commands, such as the legacy stubs, are left out.

Start from: render_reference.
"""

from __future__ import annotations

import html
import re
import sys
from collections.abc import Iterator

import click
import typer

from harborrag_app.cli.main import app

_TITLE = "# CLI command reference"
_INTRO = (
    "Look up the usage line, arguments and options of every `harborrag` command. How the CLI "
    "maps to the HTTP API, its global options, output formats and exit codes are in the "
    "[CLI reference](README.md)."
)
_GENERATED = (
    "This page is generated from the CLI's own help by `make cli-reference`; to change it, "
    "change the command's help text."
)
_TABLE_HEADER = "| Name | Type | Default | Description |\n| --- | --- | --- | --- |"
_CODE_SPAN = re.compile(r"(`+[^`]*`+)")


def render_reference() -> str:
    """Render the command reference page.

    Returns:
        Markdown ending in one newline: the intro, then a section per visible command.
    """
    root = click.Context(typer.main.get_command(app), info_name="harborrag")
    sections = [_section(context) for context in _visible_commands(root)]
    return "\n\n".join([_TITLE, _INTRO, _GENERATED, *sections]) + "\n"


def main() -> int:
    """Write the reference page to stdout.

    Returns:
        ``0``.
    """
    sys.stdout.write(render_reference())
    return 0


def _visible_commands(context: click.Context) -> Iterator[click.Context]:
    """Yield a context per visible runnable command under ``context``, in path order."""
    command = context.command
    if not isinstance(command, click.Group):
        yield context
        return
    for name in sorted(command.commands):
        subcommand = command.commands[name]
        if not subcommand.hidden:
            yield from _visible_commands(click.Context(subcommand, info_name=name, parent=context))


def _section(context: click.Context) -> str:
    """The ``##`` section of one command: its help, usage line and parameter table."""
    command = context.command
    usage = " ".join([context.command_path, *command.collect_usage_pieces(context)])
    blocks = [f"## {context.command_path}", *_paragraphs(command.help), f"```text\n{usage}\n```"]
    rows = [_row(param) for param in command.params if not getattr(param, "hidden", False)]
    if rows:
        blocks.append("\n".join([_TABLE_HEADER, *rows]))
    return "\n\n".join(blocks)


def _paragraphs(text: str | None) -> list[str]:
    """Help text as Markdown paragraphs, each on one line."""
    return [_prose(" ".join(part.split())) for part in (text or "").split("\n\n") if part.strip()]


def _row(param: click.Parameter) -> str:
    """One table row: name, type, default and help."""
    help_text = " ".join((getattr(param, "help", None) or "").split())
    cells = (_name(param), _type(param), _default(param), _prose(help_text))
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def _name(param: click.Parameter) -> str:
    """An option's flags and metavar, or an argument's metavar, as code."""
    if not isinstance(param, click.Option):
        return f"`{param.human_readable_name}`"
    flags = ", ".join([*param.opts, *param.secondary_opts])
    return f"`{flags} {param.metavar}`" if param.metavar else f"`{flags}`"


def _type(param: click.Parameter) -> str:
    """The choices of a choice parameter as code, else Click's name of the type."""
    if isinstance(param.type, click.Choice):
        return ", ".join(f"`{choice}`" for choice in param.type.choices)
    return param.type.name


def _default(param: click.Parameter) -> str:
    """``required``, the default as code, or nothing when there is none."""
    if param.required:
        return "required"
    default = param.default
    if default is None or default in ((), []):
        return ""
    if isinstance(default, bool):
        return f"`{str(default).lower()}`"
    return f"`{default}`"


def _prose(text: str) -> str:
    """``text`` with ``&``, ``<`` and ``>`` outside code spans as entities, so the page shows them."""
    return "".join(
        part if part.startswith("`") else html.escape(part, quote=False)
        for part in _CODE_SPAN.split(text)
    )


if __name__ == "__main__":
    raise SystemExit(main())
