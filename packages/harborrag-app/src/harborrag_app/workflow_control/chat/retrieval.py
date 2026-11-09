"""Tenant-scoped document retrieval and evidence relevance filtering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from harborrag_core.config_store import SettingsLevel
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_runtime.sdk import RetrievalLane, RetrievalMode, RetrievalRequest

from ..retrieval.access import corpus_mode_for

if TYPE_CHECKING:
    from harborrag_core.ports.config_store import ConfigProvider
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.contracts import RetrievalResponse
    from harborrag_runtime.memory import MemoryContext
    from harborrag_runtime.sdk import HarborRAG

    from ..memory.identity import MemoryIdentity


def relevant_results(
    results: Sequence[RetrievalResult],
    *,
    minimum: float,
) -> tuple[RetrievalResult, ...]:
    """Drop results the lane did not judge similar enough to be worth showing.

    Filtering here rather than in the prompt builder is deliberate: the same
    tuple becomes the model's evidence *and* the caller's citations, so one
    gate keeps an answer from being reported as grounded in a document it
    never used.

    A result whose lane reported no relevance is kept. ``None`` means "this
    lane cannot say", not "this is a poor match", and discarding on absence
    would silently empty the context for any backend that does not score.
    """

    if minimum <= 0.0:
        return tuple(results)
    return tuple(
        result for result in results if result.relevance is None or result.relevance >= minimum
    )


async def chat_top_k(
    config: ConfigProvider | None, identity: MemoryIdentity, settings: RuntimeSettings
) -> int:
    """Return the ``retrieval.top_k`` override for the session's level, else the environment value.

    Raises:
        HarborUnavailableError: The provider cannot load a never-cached tenant.
    """
    if config is None:
        return settings.chat_retrieval_top_k
    effective = await config.effective(identity.tenant_id, _settings_level(identity))
    override = effective.override("retrieval.top_k")
    return override if isinstance(override, int) else settings.chat_retrieval_top_k


async def chat_rerank(config: ConfigProvider | None, identity: MemoryIdentity) -> bool:
    """Return whether ``retrieval.rerank`` is on for the session's level.

    A provider that cannot load a never-cached tenant raises ``HarborUnavailableError``.

    Args:
        config: Runtime settings provider; ``None`` never reranks.
        identity: Session whose project, else tenant, is the settings level.

    Returns:
        Whether chat retrieval reranks its passages.
    """
    if config is None:
        return False
    effective = await config.effective(identity.tenant_id, _settings_level(identity))
    return effective.override("retrieval.rerank") is True


def _settings_level(identity: MemoryIdentity) -> SettingsLevel:
    """Return the project level when the session has a project, else the tenant level."""
    if identity.project_id:
        return SettingsLevel("project", project_id=identity.project_id)
    return SettingsLevel("tenant")


async def search_documents(  # noqa: PLR0913 - one keyword-only input per retrieval parameter
    runtime: HarborRAG,
    context: MemoryContext,
    *,
    identity: MemoryIdentity,
    settings: RuntimeSettings,
    observe_graph: bool,
    top_k: int,
    filters: Mapping[str, object],
    rerank: bool,
) -> RetrievalResponse:
    """Search the current question within ``filters``, observing the graph or reranking as asked."""
    return await runtime.retrieval.search(
        RetrievalRequest(
            access=AccessContext(
                principal_id=identity.principal_id,
                tenant_id=TenantId(identity.tenant_id),
                corpus_mode=corpus_mode_for(settings, identity.tenant_id),
            ),
            query=context.standalone_query,
            top_k=top_k,
            filters=dict(filters),
            lane=RetrievalLane.HYBRID,
            observe_graph=observe_graph,
            mode=RetrievalMode.LOCAL_SEMANTIC if observe_graph else RetrievalMode.FLAT,
            rerank=rerank,
        )
    )
