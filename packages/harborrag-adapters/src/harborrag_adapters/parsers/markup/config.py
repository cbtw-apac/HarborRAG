"""Markup parser configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarkupParserConfig:
    """Enabled markup engines in deterministic route order."""

    engine_order: tuple[str, ...] = ("html", "markdown")
