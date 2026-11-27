"""Embedding client for the conversation-memory profile.

Long-term recall and extraction dedup compare a query against stored memories
by vector, so both need one embedding call per text. That runs on its own
logical embed model (``HARBORRAG_MEMORY_EMBED_PROFILE``) rather than the
ingestion/retrieval one, because memory vectors live in their own collection
and may be sized differently from the evidence index.

Every failure here is degradation, never an outage. A missing catalog, a
missing embed extra, or an unbuildable client returns ``None``, logged at
WARNING, and the memory layer falls back to the repository's own lexical
search.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from harborrag_core.observability.log_events import log_event

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
    from harborrag_core.ports.memory import MemoryEmbedder
    from harborrag_runtime.config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.runtime.memory")

_DEGRADED_HINT = "Memory search falls back to the repository's lexical filter."


def _load_embed_catalog(path: Path) -> HarborEmbedClientConfig | None:
    """Load the shared embed catalog, or ``None`` when it cannot be read."""
    try:
        from harborrag_adapters.models.embed import HarborEmbedClientConfig

        return HarborEmbedClientConfig.from_file(path)
    except Exception as exc:  # noqa: BLE001 - memory must degrade, never fail chat
        log_event(
            logger,
            "memory.embedder_unavailable",
            level=logging.WARNING,
            hint=_DEGRADED_HINT,
            reason="catalog_unreadable",
            config_path=str(path),
            error=exc,
        )
        return None


def _resolve_logical_model(config: HarborEmbedClientConfig, profile: str | None) -> str:
    """Return the memory embed profile when declared, else the catalog default."""
    if profile is None:
        return config.default_model
    resolved = config.resolve_alias(profile)
    if resolved is not None:
        return resolved
    log_event(
        logger,
        "memory.embed_profile_fallback",
        profile_id=profile,
        model_id=config.default_model,
    )
    return config.default_model


def _resolve_dimensions(config: HarborEmbedClientConfig, logical_model: str) -> int | None:
    """Return the catalog's unambiguous dimension for the model, or ``None``.

    An ambiguous catalog is not fatal here. The request then omits
    ``dimensions`` and accepts whatever the deployment returns, because the
    memory index validates its own vector width.
    """
    from ..composition.resources import embedding_dimensions

    try:
        return embedding_dimensions(config, logical_model)
    except Exception:  # noqa: BLE001 - an unsized catalog is not a memory outage
        log_event(logger, "memory.embed_dimensions_unresolved", model_id=logical_model)
        return None


class _MemoryEmbedder:
    """Callable adapter turning one memory text into a dense vector.

    Instances satisfy the ``MemoryEmbedder`` port (``str -> Sequence[float]``)
    while still exposing the client, so ``close_memory_embedder`` can dispose
    the connection pool this object owns.

    Args:
        client: Embed client built from the memory profile's catalog.
        logical_model: Catalog model every request is sent to.
        dimensions: Vector width to request, or ``None`` to accept the deployment's.
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
        """Embed one memory text as a unit-length query vector.

        Recall and dedup both compare a phrase against remembered phrases, so the
        text is embedded as search text, not as a stored document. Unit vectors
        keep the dedup threshold a plain cosine value. Memory text is user
        content, so the request is never cached and never logged.

        Args:
            text: One memory or query phrase.

        Returns:
            The dense vector for ``text``.
        """
        from harborrag_core.models.embed import EmbeddingPurpose, HarborEmbedRequest

        response = await self.client.aembed(
            request=HarborEmbedRequest(
                inputs=(text,),
                logical_model=self._logical_model,
                dimensions=self._dimensions,
                purpose=EmbeddingPurpose.QUERY,
                normalize=True,
                cacheable=False,
                sensitive=True,
            )
        )
        value = response.embeddings[0].value
        if not isinstance(value, tuple):
            raise ValueError("memory embedding must be a float vector")
        return value

    async def aclose(self) -> None:
        """Close the embed client's connection pool."""
        await self.client.aclose()


async def build_memory_embedder(settings: RuntimeSettings) -> MemoryEmbedder | None:
    """Build the memory-profile embedder from the configured embed catalog.

    Returns ``None``, logged and never raised, when the catalog is missing or
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
        log_event(
            logger,
            "memory.embedder_unavailable",
            level=logging.WARNING,
            hint=_DEGRADED_HINT,
            reason="client_build_failed",
            model_id=logical_model,
            error=exc,
        )
        return None


async def close_memory_embedder(embedder: MemoryEmbedder | None) -> None:
    """Dispose the async client an embedder built here owns."""
    if isinstance(embedder, _MemoryEmbedder):
        await embedder.aclose()
