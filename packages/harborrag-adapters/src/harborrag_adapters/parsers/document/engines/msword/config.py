"""Legacy Word binary engine configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MsWordEngineConfig:
    """Reserved legacy Word binary provider settings."""
