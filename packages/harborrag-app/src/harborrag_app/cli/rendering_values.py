"""Defensive coercion of untyped AppResponse data into Rich renderables.

AppResponse.data is an untyped mapping, so the terminal views cannot rely on static
types and must narrow each value at the point of use. Centralising that here keeps the
views readable and leaves one place to delete once AppResponse.data becomes typed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rich.table import Table
from rich.text import Text

STATUS_STYLES = {
    "pending": "yellow",
    "running": "cyan",
    "paused": "yellow",
    "cancelling": "yellow",
    "cancelled": "dim",
    "completed": "green",
    "failed": "red",
    # Temporal execution statuses, which use American spelling and cover
    # outcomes the workflow cannot report about itself.
    "canceled": "dim",
    "terminated": "red",
    "timed_out": "red",
    "continued_as_new": "cyan",
}


def details_table() -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", justify="right")
    table.add_column()
    return table


def mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def optional_integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def text(value: Any, *, style: str | None = None) -> Text:
    text = "—" if value is None or value == "" else str(value)
    return Text(text, style=style) if style else Text(text)


def boolean(value: Any) -> Text:
    enabled = bool(value)
    return Text("yes" if enabled else "no", style="yellow" if enabled else "dim")


def status_text(value: str) -> Text:
    return Text(value.upper(), style=f"bold {STATUS_STYLES.get(value, 'white')}")


__all__ = [
    "STATUS_STYLES",
    "boolean",
    "details_table",
    "integer",
    "mapping",
    "optional_integer",
    "sequence",
    "status_text",
    "text",
]
