"""Configuration objects for the memory facade."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemoryManagerConfig:
    """Default behavior for the top-level memory facade."""

    recent_turn_limit: int = 2

    def __post_init__(self) -> None:
        if not 1 <= self.recent_turn_limit <= 1000:
            raise ValueError("recent_turn_limit must be between 1 and 1000")
