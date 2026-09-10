"""Read back and account for one finished agent run.

Unlike the chat surface, the agent does not need its persistence split: the
engine writes a durable RUNNING checkpoint carrying the user message before
the first model turn, and converges it to FAILED or CANCELLED on the way out,
so an interrupted run is never lost -- it is resumable. What the application
still owns is reading the exchange back for extraction provenance, and
recording what the run spent.
"""

from __future__ import annotations

import logging

from harborrag_core.ports.conversation import ConversationHistoryRepository, ConversationMessage
from harborrag_core.ports.usage import ModelUsageRepository
from harborrag_runtime.agent import AgentRunResult

from ..memory.extraction import MemoryExtractionQueue
from ..memory.identity import MemoryIdentity
from ..memory.usage import ModelCall, record_model_usage

logger = logging.getLogger("harborrag.app.workflow_control.agent")


async def remembered_exchange(
    memory: ConversationHistoryRepository,
    identity: MemoryIdentity,
    result: AgentRunResult,
    *,
    extraction: MemoryExtractionQueue | None,
) -> tuple[ConversationMessage, ...] | None:
    """The run's own history messages, or ``None`` when none were written.

    The engine persists the exchange itself and treats a memory failure as
    advisory, so the app reads the tail of the history back instead of
    rebuilding it: that recovers the real message ids extraction cites as
    provenance and proves the turn was stored. Skipped entirely when no
    extraction queue is wired, so the CLI path pays for no extra read.
    """

    if extraction is None:
        return None
    try:
        messages = await memory.recent_messages(identity.conversation(), limit=2)
    except Exception:  # noqa: BLE001 - extraction is best effort
        logger.warning(
            "Could not read back the agent exchange for tenant=%s session=%s; "
            "skipping long-term extraction",
            identity.tenant_id,
            identity.session_id,
        )
        return None
    if len(messages) != 2 or messages[-1].run_id != result.run_id:
        return None
    return messages


async def record_run_usage(
    usage: ModelUsageRepository | None,
    identity: MemoryIdentity,
    result: AgentRunResult,
) -> bool:
    """Account for the whole run's tokens against the tenant and the human.

    A run spends across several model turns, so the aggregate the engine
    already tracks is the honest footprint; the final response names the model
    that actually served it. Recording never fails the run.
    """

    response = result.response
    return await record_model_usage(
        usage,
        identity,
        ModelCall(
            logical_model=response.logical_model,
            provider=response.provider,
            provider_model=response.provider_model,
            usage=result.usage,
            estimated_cost_usd=response.estimated_cost_usd,
            finish_reason=str(response.finish_reason),
        ),
        surface="agent",
        run_id=result.run_id,
    )


__all__ = ["record_run_usage", "remembered_exchange"]
