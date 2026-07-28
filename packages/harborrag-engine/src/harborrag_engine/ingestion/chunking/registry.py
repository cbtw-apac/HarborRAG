from __future__ import annotations

from .errors import UnknownChunkingStrategyError
from .strategies.base import ChunkStrategy


class ChunkStrategyRegistry:
    """Map configuration-facing names to structural unit strategies."""

    def __init__(self, strategies: tuple[ChunkStrategy, ...] = ()) -> None:
        self._strategies: dict[str, ChunkStrategy] = {}
        for strategy in strategies:
            self.register(strategy)

    def register(self, strategy: ChunkStrategy) -> None:
        """Register a uniquely named chunking strategy."""

        name = strategy.name.strip()
        if not name:
            raise ValueError("strategy name must be non-empty")
        if name in self._strategies:
            raise ValueError(f"chunking strategy is already registered: {name}")
        self._strategies[name] = strategy

    def get(self, name: str) -> ChunkStrategy:
        """Return the strategy registered under a configuration-facing name."""

        try:
            return self._strategies[name]
        except KeyError as exc:
            raise UnknownChunkingStrategyError(f"unknown chunking strategy: {name}") from exc

    @property
    def names(self) -> tuple[str, ...]:
        """Return registered strategy names in stable order."""

        return tuple(sorted(self._strategies))
