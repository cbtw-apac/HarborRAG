"""Text parser configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextParserConfig:
    """Enabled text engine."""

    engine: str = "text"
