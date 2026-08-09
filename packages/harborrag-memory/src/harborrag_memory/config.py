"""Configuration objects for the memory facade."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemoryManagerConfig:
    """Default behavior for the top-level memory facade."""

    recent_turn_limit: int = 2
    working_ttl_seconds: int = 3600

    def __post_init__(self) -> None:
        if not 1 <= self.recent_turn_limit <= 1000:
            raise ValueError("recent_turn_limit must be between 1 and 1000")
        if not 1 <= self.working_ttl_seconds <= 31_536_000:
            raise ValueError("working_ttl_seconds must be between 1 and 31536000")
