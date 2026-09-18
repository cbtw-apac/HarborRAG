"""LangChain chat model for the conversation-memory profile.

Query rewriting, fact extraction, and session summaries are cheap side
errands, so they run on their own logical model rather than the answering
one. Every failure here is degradation, never an outage: a missing catalog,
a missing ``langchain`` extra, or an unbuildable client returns ``None`` and
the memory layer falls back to a plain verbatim window.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from langchain_core.language_models.chat_models import BaseChatModel

if TYPE_CHECKING:
    from pathlib import Path

    from harborrag_adapters.models.chat import HarborChatClientConfig
    from harborrag_runtime.config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.runtime.memory")

_DEGRADED = "the memory layer degrades to a verbatim window"


def _load_chat_catalog(path: Path) -> HarborChatClientConfig | None:
    """Load the shared chat catalog, or ``None`` when it cannot be read."""

    try:
        from harborrag_adapters.models.chat import HarborChatClientConfig

        return HarborChatClientConfig.from_file(path)
    except Exception as exc:  # noqa: BLE001 - memory must degrade, never fail chat
        logger.warning(
            "Memory chat model unavailable: chat catalog path=%s error_type=%s; %s",
            path,
            type(exc).__name__,
            _DEGRADED,
        )
        return None


def _resolve_logical_model(config: HarborChatClientConfig, profile: str) -> str:
    """Return the memory profile when the catalog declares it, else the default."""

    resolved = config.resolve_alias(profile)
    if resolved is not None:
        return resolved
    logger.info(
        "Memory model profile=%s is absent from the chat catalog; "
        "falling back to the default logical model=%s",
        profile,
        config.default_model,
    )
    return config.default_model


def _build_chat_model(config: HarborChatClientConfig, logical_model: str) -> BaseChatModel | None:
    """Wrap one async chat client as a LangChain model, or ``None`` on failure."""

    try:
        from harborrag_adapters.models.chat import ChatClientFactory
        from harborrag_adapters.models.chat.langchain import HarborChatModel
        from harborrag_core.models.chat import HarborChatMetadata

        client = ChatClientFactory.create_async(config)
        return HarborChatModel(
            client,
            logical_model=logical_model,
            request_metadata=HarborChatMetadata(pipeline_stage="memory"),
            sensitive=True,
        )
    except Exception as exc:  # noqa: BLE001 - memory must degrade, never fail chat
        logger.warning(
            "Memory chat model unavailable: logical_model=%s error_type=%s; %s",
            logical_model,
            type(exc).__name__,
            _DEGRADED,
        )
        return None


async def build_memory_chat_model(settings: RuntimeSettings) -> BaseChatModel | None:
    """Build the memory-profile chat model from the configured chat catalog.

    Returns ``None`` -- logged, never raised -- when the catalog is missing or
    unreadable, or when the underlying client cannot be constructed.
    """

    config = await asyncio.to_thread(_load_chat_catalog, settings.model_config_path)
    if config is None:
        return None
    return _build_chat_model(config, _resolve_logical_model(config, settings.memory_model_profile))


async def close_memory_chat_model(model: BaseChatModel | None) -> None:
    """Dispose the async client a model built here owns."""

    if model is None:
        return
    try:
        from harborrag_adapters.models.chat.langchain import HarborChatModel
    except ImportError:  # pragma: no cover - only without the langchain extra
        return
    if isinstance(model, HarborChatModel):
        await model.client.aclose()


__all__ = ["build_memory_chat_model", "close_memory_chat_model"]
