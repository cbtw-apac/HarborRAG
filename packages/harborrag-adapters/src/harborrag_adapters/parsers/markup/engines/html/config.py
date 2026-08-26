"""HTML engine configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HtmlEngineConfig:
    """HTML cleaning policy."""

    preserve_links: bool = False
