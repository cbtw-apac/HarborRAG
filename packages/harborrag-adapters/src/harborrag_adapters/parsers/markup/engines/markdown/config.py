"""Markdown engine configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarkdownEngineConfig:
    """Markdown extraction policy."""

    preserve_code_blocks: bool = True
