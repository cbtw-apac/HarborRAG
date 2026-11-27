"""Public SDK façade for conversation-memory context assembly and extraction."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from harborrag_core.observability.log_events import FailureKind, log_event, log_failure
from harborrag_core.ports.memory import MemoryOwner

if TYPE_CHECKING:
    from collections.abc import Sequence

    from harborrag_core.ports.conversation import ConversationMessage, ConversationMessageStore
    from harborrag_core.ports.memory import Memory, MemoryIndex, MemoryRepository
    from harborrag_memory import MemoryContext
    from harborrag_runtime.contracts import MemoryContextRequest
    from harborrag_runtime.sdk import HarborRAG


logger = logging.getLogger("harborrag.runtime.memory")

_UNANCHORED_OUTCOME = "unanchored_context_kept"


def _owner(request: MemoryContextRequest) -> MemoryOwner:
    """Return the caller's long-term memory isolation key for one request."""
    return MemoryOwner(
        tenant_id=request.tenant_id,
        principal_id=request.principal_id,
        user_id=request.user_id,
        session_id=request.session_id,
        project_id=request.project_id,
    )


class MemoryFacade:
    """Turn a request plus the caller's stores into one prompt-ready context.

    Args:
        owner: SDK instance whose cached memory services every call uses.
    """

    def __init__(self, owner: HarborRAG) -> None:
        self._owner = owner

    async def build_context(
        self,
        request: MemoryContextRequest,
        *,
        messages: ConversationMessageStore,
        memories: MemoryRepository | None = None,
        index: MemoryIndex | None = None,
    ) -> MemoryContext:
        """Assemble the recent window, summary, and recall for one question.

        The stores and the vector index are per call because the application
        owns its database and vector resources; only the policy, the memory
        chat model, and the embedder are cached.
        """
        service = self._owner._memory_context_service()
        builder = await service.builder(messages=messages, memories=memories, index=index)
        return await builder.build(_owner(request), request.question)

    async def recall_anchored(
        self,
        request: MemoryContextRequest,
        context: MemoryContext,
        *,
        memories: MemoryRepository,
        index: MemoryIndex | None = None,
        anchor_entity_ids: Sequence[str] = (),
    ) -> MemoryContext:
        """Re-run only recall, anchored on the graph entities retrieval surfaced.

        This is the second half of a turn that already has a context: document
        search has run against ``context.standalone_query`` and reported which
        knowledge-graph entities its results sit on, and those entities are
        better evidence of what the user means than the question alone. Only
        the recall step repeats. The standalone query is reused verbatim, so
        there is no second rewrite, no second summarization, and
        ``summary_written`` cannot flip. The type hint the first pass ranked
        with travels on the context too, so it is reused rather than re-asked
        for. Dropping it here would silently re-rank on different terms than
        the pass whose result is being refined.

        Anchors only ever re-rank the same candidate set, so the call is
        skipped outright when there are no anchors, when recall is disabled,
        or when the first recall returned nothing (no store, or nothing to
        find). Anchoring is an improvement, not a requirement, so any failure
        keeps the unanchored context, logged. So does an anchored recall that
        came back empty, since recall degrades to ``()`` on a store failure and
        re-ranking must never *lose* memories the prompt already had.
        """
        anchors = tuple(dict.fromkeys(item.strip() for item in anchor_entity_ids if item.strip()))
        service = self._owner._memory_context_service()
        policy = service.policy
        if not anchors or not policy.enabled or not policy.recall_top_k or not context.recalled:
            return context
        from harborrag_memory import MemoryRecall

        try:
            recall = MemoryRecall(
                policy=policy,
                memories=memories,
                index=index or await service.index(),
                embedder=await service.embedder(),
            )
            recalled = await recall.recall(
                _owner(request),
                context.standalone_query,
                anchor_entity_ids=anchors,
                wanted_types=context.wanted_types,
            )
        except Exception as error:  # noqa: BLE001
            log_failure(
                logger,
                "memory.anchored_recall_failed",
                error,
                FailureKind.EXPECTED,
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                outcome=_UNANCHORED_OUTCOME,
            )
            return context
        if not recalled:
            log_event(
                logger,
                "memory.anchored_recall_empty",
                level=logging.WARNING,
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                outcome=_UNANCHORED_OUTCOME,
            )
            return context
        return replace(context, recalled=recalled, anchor_entity_ids=anchors)

    async def extract(
        self,
        request: MemoryContextRequest,
        *,
        messages: Sequence[ConversationMessage],
        memories: MemoryRepository,
        index: MemoryIndex | None = None,
    ) -> tuple[Memory, ...]:
        """Distil durable facts from one finished exchange, add-only.

        ``messages`` are the exchange's own messages, the same records that
        were written to conversation history, so extracted memories carry
        real provenance back to them. Returns ``()`` without a model call when
        the policy is disabled, extraction is switched off, or no memory chat
        model could be built.
        """
        service = self._owner._memory_context_service()
        if not service.policy.enabled or not service.settings.memory_extraction_enabled:
            return ()
        model = await service.model()
        if model is None:
            return ()
        from harborrag_memory import MemoryExtractor

        extractor = MemoryExtractor(
            policy=service.policy,
            memories=memories,
            model=model,
            index=index or await service.index(),
            embedder=await service.embedder(),
            entities=await service.entity_resolver(),
        )
        return await extractor.extract(_owner(request), messages=messages)
