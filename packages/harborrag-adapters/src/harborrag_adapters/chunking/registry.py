from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Self, cast

from harborrag_core.contracts.chunking import TextSplit, TokenCounter

from .base import ChunkRequest, HarborBaseChunk


class HarborChunkRegistry:
    """Map stable names and aliases to chunk adapter classes."""

    def __init__(
        self,
        adapters: Iterable[type[HarborBaseChunk[Any]]] = (),
    ) -> None:
        self._adapters: dict[str, type[HarborBaseChunk[Any]]] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(
        self,
        adapter: type[HarborBaseChunk[Any]],
        *,
        aliases: Iterable[str] = (),
        replace: bool = False,
    ) -> Self:
        """Register one adapter class without partially applying collisions."""

        if not isinstance(adapter, type) or not issubclass(adapter, HarborBaseChunk):
            raise TypeError("chunk adapter must inherit HarborBaseChunk")

        canonical_name = self._normalize_name(adapter.chunk_name)
        keys = tuple(
            dict.fromkeys((canonical_name, *(self._normalize_name(alias) for alias in aliases)))
        )
        for key in keys:
            existing = self._adapters.get(key)
            if existing is not None and existing is not adapter and not replace:
                raise ValueError(
                    f"Chunk adapter key {key!r} is already registered to "
                    f"{existing.__name__}; pass replace=True to override."
                )

        for key in keys:
            self._adapters[key] = adapter
        return self

    def unregister(self, name: str) -> None:
        """Remove one canonical name or alias."""

        key = self._normalize_name(name)
        try:
            del self._adapters[key]
        except KeyError as exc:
            raise ValueError(f"Unknown chunk adapter: {name}") from exc

    def get_class(self, name: str) -> type[HarborBaseChunk[Any]]:
        """Return a registered adapter class by canonical name or alias."""

        key = self._normalize_name(name)
        try:
            return self._adapters[key]
        except KeyError as exc:
            raise ValueError(f"Unknown chunk adapter: {name}") from exc

    def create(
        self,
        name: str,
        token_counter: TokenCounter,
        **kwargs: Any,
    ) -> HarborBaseChunk[Any]:
        """Construct a registered adapter with its required token counter."""

        return self.get_class(name)(token_counter, **kwargs)

    def available(self, name: str) -> bool:
        """Return whether a registered adapter can load its runtime dependency."""

        return self.get_class(name).available()

    def names(self) -> tuple[str, ...]:
        """Return all registered canonical names and aliases in stable order."""

        return tuple(sorted(self._adapters))

    @classmethod
    def default(cls) -> Self:
        """Create an isolated registry containing all built-in adapters."""

        from .htmlsplitter import HtmlStructureSplitter
        from .jsonsplitter import JsonStructureSplitter
        from .markdownsplitter import MarkdownStructureSplitter
        from .recursive import RecursiveTextRefiner

        return cls(
            (
                HtmlStructureSplitter,
                JsonStructureSplitter,
                MarkdownStructureSplitter,
                RecursiveTextRefiner,
            )
        )

    @staticmethod
    def _normalize_name(name: str) -> str:
        if not isinstance(name, str):
            raise TypeError("chunk adapter name must be a string")
        normalized = name.strip().lower()
        if not normalized:
            raise ValueError("chunk adapter name must be non-empty")
        return normalized


chunk_registry = HarborChunkRegistry.default()


class HarborChunk:
    """Factory facade for selecting and calling a registered chunk adapter.

    Example::

        chunker = HarborChunk("markdown", token_counter)
        splits = chunker.split(request)
    """

    def __init__(
        self,
        adapter: str,
        token_counter: TokenCounter,
        *,
        registry: HarborChunkRegistry | None = None,
        **kwargs: Any,
    ) -> None:
        self._registry = registry or chunk_registry
        self.adapter = self._registry.create(adapter, token_counter, **kwargs)
        self.adapter_name = self.adapter.name

    def split(self, request: ChunkRequest) -> tuple[TextSplit, ...]:
        """Validate and delegate a request to the selected adapter."""

        if not self.adapter.supports(request):
            expected = self.adapter.request_type.__name__
            raise TypeError(
                f"Chunk adapter {self.adapter.name!r} expects {expected}; "
                f"received {type(request).__name__}"
            )
        adapter = cast(HarborBaseChunk[ChunkRequest], self.adapter)
        return adapter.split(request)

    @classmethod
    def available(
        cls,
        adapter: str,
        *,
        registry: HarborChunkRegistry | None = None,
    ) -> bool:
        """Return whether an adapter is available without importing its provider."""

        return (registry or chunk_registry).available(adapter)

    @classmethod
    def adapters(cls) -> tuple[str, ...]:
        """List names available from the process-wide default registry."""

        return chunk_registry.names()
