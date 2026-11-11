"""Defensive coercion of untyped AppResponse data into Rich renderables.

AppResponse.data is an untyped mapping, so the terminal views cannot rely on static
types and must narrow each value at the point of use. Centralising that here keeps the
views readable and leaves one place to delete once AppResponse.data becomes typed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rich.text import Text


def mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def text(value: Any, *, style: str | None = None) -> Text:
    text = "—" if value is None or value == "" else str(value)
    return Text(text, style=style) if style else Text(text)
