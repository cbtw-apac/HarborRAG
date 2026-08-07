"""Configuration objects for the memory facade."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemoryManagerConfig:
    """Default behavior for the top-level memory facade."""

    recent_turn_limit: int = 2
    working_ttl_seconds: int = 3600


__all__ = ["MemoryManagerConfig"]