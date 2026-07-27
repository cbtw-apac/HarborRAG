"""JSON engine configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JsonEngineConfig:
    """JSON nesting safety controls."""

    max_flatten_depth: int = 200
