"""Legacy PowerPoint binary engine configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MsPowerPointEngineConfig:
    """Reserved legacy PowerPoint binary provider settings."""
