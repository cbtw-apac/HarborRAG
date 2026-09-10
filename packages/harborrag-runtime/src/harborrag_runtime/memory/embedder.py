"""Embedding client for the conversation-memory profile.

Long-term recall and extraction dedup compare a query against stored memories
by vector, so both need one embedding call per text. That runs on its own
logical embed model (``HARBORRAG_MEMORY_EMBED_PROFILE``) rather than the
ingestion/retrieval one, because memory vectors live in their own collection
and may be sized differently from the evidence index.

Every failure here is degradation, never an outage: a missing catalog, a
missing embed extra, or an unbuildable client returns ``None`` -- logged at
WARNING -- and the memory layer falls back to the repository's own lexical
search.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
    from harborrag_core.ports.memory import MemoryEmbedder
    from harborrag_runtime.config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.runtime.memory")

_DEGRADED = "memory search falls back to the repository's lexical filter"


def _load_embed_catalog(path: Path) -> HarborEmbedClientConfig | None:
    """Load the shared embed catalog, or ``None`` when it cannot be read."""

    try:
        from harborrag_adapters.models.embed import HarborEmbedClientConfig

        return HarborEmbedClientConfig.from_file(path)
    except Exception as exc:  # noqa: BLE001 - memory must degrade, never fail chat
        logger.warning(
            "Memory embedder unavailable: embed catalog path=%s error_type=%s; %s",
            path,
            type(exc).__name__,
            _DEGRADED,
        )
        return None


def _resolve_logical_model(config: HarborEmbedClientConfig, profile: str | None) -> str:
    """Return the memory embed profile when declared, else the catalog default."""

    if profile is None:
        return config.default_model
    resolved = config.resolve_alias(profile)
    if resolved is not None:
        return resolved
    logger.info(
        "Memory embed profile=%s is absent from the embed catalog; "
        "falling back to the default logical model=%s",
        profile,
        config.default_model,
    )
    return config.default_model


def _resolve_dimensions(config: HarborEmbedClientConfig, logical_model: str) -> int | None:
    """The catalog's unambiguous dimension for the model, or ``None``.

    An ambiguous catalog is not fatal here: the request simply omits
    ``dimensions`` and accepts whatever the deployment returns, because the
    memory index validates its own vector width.
    """

    from ..composition.resources import embedding_dimensions

    try:
        return embedding_dimensions(config, logical_model)
    except Exception:  # noqa: BLE001 - an unsized catalog is not a memory outage
        logger.info(
            "Memory embed model=%s has no unambiguous dimension; sending the request without one",
            logical_model,
        )
        return None


class _MemoryEmbedder:
    """Callable adapter turning one memory text into a dense vector.

    Instances satisfy the ``MemoryEmbedder`` port (``str -> Sequence[float]``)
    while still exposing the client, so ``close_memory_embedder`` can dispose
    the connection pool this object owns.
    """

    def __init__(
        self,
        client: HarborEmbedClient,
        *,
        logical_model: str,
        dimensions: int | None,
    ) -> None:
        self.client = client
        self._logical_model = logical_model
        self._dimensions = dimensions

    async def __call__(self, text: str) -> Sequence[float]:
        from harborrag_core.models.embed import EmbeddingPurpose, HarborEmbedRequest

        response = await self.client.aembed(
            request=HarborEmbedRequest(
                inputs=(text,),
                logical_model=self._logical_model,
                dimensions=self._dimensions,
                # Search text, not stored documents: recall and dedup both
                # compare a phrase against remembered phrases.
                purpose=EmbeddingPurpose.QUERY,
                # Unit vectors keep the dedup threshold a plain cosine value.
                normalize=True,
                # Memory text is user content: never cached, never logged.
                cacheable=False,
                sensitive=True,
            )
        )
        value = response.embeddings[0].value
        if not isinstance(value, tuple):
            raise ValueError("memory embedding must be a float vector")
        return value

    async def aclose(self) -> None:
        await self.client.aclose()


async def build_memory_embedder(settings: RuntimeSettings) -> MemoryEmbedder | None:
    """Build the memory-profile embedder from the configured embed catalog.

    Returns ``None`` -- logged, never raised -- when the catalog is missing or
    unreadable, or when the underlying client cannot be constructed.
    """

    config = await asyncio.to_thread(_load_embed_catalog, settings.model_config_path)
    if config is None:
        return None
    logical_model = _resolve_logical_model(config, settings.memory_embed_profile)
    try:
        from harborrag_adapters.models.embed import HarborEmbedClient

        return _MemoryEmbedder(
            HarborEmbedClient.from_config(config),
            logical_model=logical_model,
            dimensions=_resolve_dimensions(config, logical_model),
        )
    except Exception as exc:  # noqa: BLE001 - memory must degrade, never fail chat
        logger.warning(
            "Memory embedder unavailable: logical_model=%s error_type=%s; %s",
            logical_model,
            type(exc).__name__,
            _DEGRADED,
        )
        return None


async def close_memory_embedder(embedder: MemoryEmbedder | None) -> None:
    """Dispose the async client an embedder built here owns."""

    if isinstance(embedder, _MemoryEmbedder):
        await embedder.aclose()


__all__ = ["build_memory_embedder", "close_memory_embedder"]
