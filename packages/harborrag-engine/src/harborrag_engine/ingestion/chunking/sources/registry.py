from __future__ import annotations

from ..errors import UnknownChunkingStrategyError
from .base import ChunkRecordValidator, ChunkStrategy


class ChunkStrategyRegistry:
    """Own the explicit extension boundary for source chunking policies."""

    def __init__(self, strategies: tuple[ChunkStrategy, ...] = ()) -> None:
        self._strategies: dict[str, ChunkStrategy] = {}
        for strategy in strategies:
            self.register(strategy)

    def register(self, strategy: ChunkStrategy) -> None:
        """Register a uniquely named source strategy."""

        name = strategy.name.strip()
        if not name:
            raise ValueError("strategy name must be non-empty")
        if name in self._strategies:
            raise ValueError(f"chunking strategy is already registered: {name}")
        self._strategies[name] = strategy

    def get(self, name: str) -> ChunkStrategy:
        """Return the configured strategy or fail with a domain error."""

        try:
            return self._strategies[name]
        except KeyError as exc:
            raise UnknownChunkingStrategyError(f"unknown chunking strategy: {name}") from exc

    def record_validator(self, name: str) -> ChunkRecordValidator | None:
        """Return a strategy's optional source-owned record validator."""

        validator = getattr(self.get(name), "record_validator", None)
        return validator if callable(validator) else None
