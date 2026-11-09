"""Fail-closed request-scope admission, before answer generation or SSE headers."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from harborrag_core.contracts.errors import HarborUnavailableError, HarborValidationError
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatMetadata,
    HarborChatRequest,
    HarborChatResponse,
)
from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.usage import ModelUsageRecord
from harborrag_runtime.chat import ChatPrompt

from ..memory.access import MemoryAccess
from .preparation import ChatTurnResources

logger = logging.getLogger("harborrag.app.workflow_control.chat.query_scope")


class ScopeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["knowledge", "conversation", "unsupported"]


OUT_OF_SCOPE = (
    "This assistant answers questions about indexed knowledge and this conversation. "
    "It cannot create unrelated content; ask about a source or provide relevant material."
)


type ScopeHistory = list[dict[str, str]]


@dataclass(frozen=True, slots=True)
class ScopeCheck:
    """The scope check's model call, and the refusal to answer with when out of scope."""

    response: HarborChatResponse
    refusal: str | None = None


async def require_query_scope(
    resources: ChatTurnResources,
    query: str,
    access: MemoryAccess,
    *,
    model: str | None,
    trace_id: str | None = None,
) -> None:
    """Refuse a request that is neither about indexed knowledge nor about the conversation.

    Raises:
        HarborValidationError: The request is out of scope (``details.reason`` is
            ``out_of_scope``).
        HarborUnavailableError: The model's decision could not be read.
    """
    check = await check_query_scope(
        resources,
        query,
        access,
        history=await scope_history(resources, access),
        model=model,
        trace_id=trace_id,
    )
    if check.refusal is not None:
        raise HarborValidationError(check.refusal, {"reason": "out_of_scope"})


async def scope_history(resources: ChatTurnResources, access: MemoryAccess) -> ScopeHistory:
    """The session's last three exchanges as the scope check reads them; none for a one-off."""

    if access.session_id is None:
        return []
    turns = await resources.memory.recent(
        ConversationIdentity(
            tenant_id=access.tenant_id,
            principal_id=access.principal_id,
            session_id=access.session_id,
            user_id=access.user_id,
        ),
        limit=3,
    )
    return [
        {"user": turn.user_content[:4000], "assistant": turn.assistant_content[:4000]}
        for turn in turns
    ]


async def check_query_scope(  # noqa: PLR0913 - the check's inputs, then the call's options
    resources: ChatTurnResources,
    query: str,
    access: MemoryAccess,
    *,
    history: ScopeHistory,
    model: str | None,
    trace_id: str | None = None,
) -> ScopeCheck:
    """Ask the model whether a request is about indexed knowledge or the conversation.

    Makes the model call and nothing else that can fail before it, so a caller can treat
    the call as started once this is awaited.

    Args:
        resources: The turn's ports.
        query: The question.
        access: The caller, for the call's metadata and the usage record.
        history: The session's last exchanges, from ``scope_history``.
        model: The logical model; ``None`` is the default.
        trace_id: The request's trace id, put on the model call.

    Returns:
        The model call, with the refusal text when the request is out of scope.

    Raises:
        HarborUnavailableError: The model's decision could not be read.
    """
    response = await resources.runtime().chat.complete(
        HarborChatRequest(
            messages=(
                HarborChatMessage.user(json.dumps({"query": query, "recent_history": history})),
            ),
            logical_model=model,
            # Reasoning models can consume the entire smaller output budget
            # before producing even a one-field JSON decision.
            max_tokens=512,
            sensitive=True,
            metadata=HarborChatMetadata(
                trace_id=trace_id,
                tenant_id=access.tenant_id,
                user_id=access.user_id,
                conversation_id=access.session_id,
            ),
        ),
        prompt=ChatPrompt.QUERY_GATE,
    )
    # Admission is a real model call even when rejected. Record it separately;
    # the completion's cost scope remains answer_generation, not admission.
    if resources.usage is not None:
        try:
            await resources.usage.record(
                ModelUsageRecord(
                    tenant_id=access.tenant_id,
                    user_id=access.user_id,
                    principal_id=access.principal_id,
                    session_id=access.session_id,
                    logical_model=response.logical_model,
                    provider=response.provider,
                    provider_model=response.provider_model,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    total_tokens=response.usage.total_tokens,
                    estimated_cost_usd=response.estimated_cost_usd,
                    finish_reason="query_scope_gate",
                )
            )
        except Exception:  # noqa: BLE001 - accounting cannot override admission
            logger.warning("Could not record query-scope usage for tenant=%s", access.tenant_id)
    try:
        decision = ScopeDecision.model_validate_json(response.text)
    except ValidationError:
        raise HarborUnavailableError(
            "Request scope could not be determined; please retry"
        ) from None
    return ScopeCheck(response, OUT_OF_SCOPE if decision.scope == "unsupported" else None)
