"""ODT engine configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OdtEngineConfig:
    """Reserved ODT provider settings."""
