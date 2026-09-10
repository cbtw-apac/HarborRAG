"""Durable per-request model-usage accounting for the chat and agent surfaces.

Chat was the one model family whose spend nobody could attribute: the tokens
were reported to the caller and then thrown away. One ``ModelUsageRecord`` per
finished turn fixes that, keyed by tenant *and* end user so a service
principal fronting several people can still be billed per human.

Recording is best effort in the strongest sense: the provider has already been
paid by the time this runs, so an accounting failure is logged with
identifiers only and swallowed. It must never be the reason a turn fails.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from harborrag_core.models.chat import (
    HarborChatResponse,
    HarborChatStreamChunk,
    HarborChatUsage,
)
from harborrag_core.ports.usage import ModelUsageRecord, ModelUsageRepository, UsageSurface

from .identity import MemoryIdentity

logger = logging.getLogger("harborrag.app.workflow_control.memory")


@dataclass(frozen=True, slots=True)
class ModelCall:
    """What one model call reported about itself, however it was delivered.

    A completed call is described by its response; a stream that died
    mid-flight is described by the last chunk that carried provider identity
    and usage. Both reduce to the same footprint, so a partial answer whose
    tokens were reported is still accounted for.
    """

    logical_model: str
    provider: str
    provider_model: str
    usage: HarborChatUsage
    estimated_cost_usd: float | None = None
    finish_reason: str | None = None

    @classmethod
    def from_response(cls, response: HarborChatResponse) -> ModelCall:
        """Describe a completed call from the response the provider returned."""

        return cls(
            logical_model=response.logical_model,
            provider=response.provider,
            provider_model=response.provider_model,
            usage=response.usage,
            estimated_cost_usd=response.estimated_cost_usd,
            finish_reason=str(response.finish_reason),
        )

    @classmethod
    def from_stream(
        cls,
        chunk: HarborChatStreamChunk | None,
        *,
        usage: HarborChatUsage | None,
        finish_reason: str | None = None,
    ) -> ModelCall | None:
        """Describe a streamed call from the chunks that named its model.

        ``None`` when the stream reported no model identity or no usage: those
        tokens are simply unknown, and a zero-token record would be
        indistinguishable from a free request in the totals.
        """

        if chunk is None or usage is None:
            return None
        return cls(
            logical_model=chunk.logical_model,
            provider=chunk.provider,
            provider_model=chunk.provider_model,
            usage=usage,
            finish_reason=finish_reason,
        )


async def record_model_usage(
    repository: ModelUsageRepository | None,
    identity: MemoryIdentity,
    call: ModelCall | None,
    *,
    surface: UsageSurface,
    run_id: str | None = None,
) -> bool:
    """Write one usage record; report whether it landed, and never raise.

    ``None`` for ``call`` means the provider reported no usage at all (a
    stream cut before its first usage chunk), and nothing is written: a record
    of zero tokens is indistinguishable from a free request and would corrupt
    the totals it exists to support.
    """

    if repository is None or call is None:
        return False
    try:
        await repository.record(_record(identity, call, surface=surface, run_id=run_id))
    except Exception:  # noqa: BLE001 - accounting must not fail a paid-for turn
        logger.warning(
            "Recording model usage failed for tenant=%s user=%s session=%s surface=%s; "
            "the turn stands and the spend is unaccounted",
            identity.tenant_id,
            identity.user_id,
            identity.session_id,
            surface,
            exc_info=True,
        )
        return False
    return True


def _record(
    identity: MemoryIdentity,
    call: ModelCall,
    *,
    surface: UsageSurface,
    run_id: str | None,
) -> ModelUsageRecord:
    usage = call.usage
    return ModelUsageRecord(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        principal_id=identity.principal_id,
        surface=surface,
        logical_model=call.logical_model,
        provider=call.provider,
        provider_model=call.provider_model,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens or usage.prompt_tokens + usage.completion_tokens,
        session_id=identity.session_id,
        run_id=run_id,
        estimated_cost_usd=call.estimated_cost_usd,
        finish_reason=call.finish_reason,
    )


__all__ = ["ModelCall", "record_model_usage"]
