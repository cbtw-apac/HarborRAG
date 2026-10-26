"""Fail-closed request-scope admission, before answer generation or SSE headers."""

from __future__ import annotations

import json
import logging
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from harborrag_core.contracts.errors import HarborUnavailableError, HarborValidationError
from harborrag_core.models.chat import HarborChatMessage, HarborChatMetadata, HarborChatRequest
from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.usage import ModelUsageRecord
from harborrag_runtime.chat import ChatPrompt

from ..memory.access import MemoryAccess
from .preparation import ChatTurnResources

logger = logging.getLogger("harborrag.app.workflow_control.chat.query_scope")


class ScopeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["knowledge", "conversation", "unsupported"]


class QueryScopeValidator(Protocol):
    async def validate_completion_scope(
        self, query: str, access: MemoryAccess, *, model: str | None, mode: Literal["rag", "agent"]
    ) -> None: ...


async def require_query_scope(
    resources: ChatTurnResources,
    query: str,
    access: MemoryAccess,
    *,
    model: str | None,
    mode: Literal["rag", "agent"],
) -> None:
    history: list[dict[str, str]] = []
    if access.session_id is not None:
        turns = await resources.memory.recent(
            ConversationIdentity(
                tenant_id=access.tenant_id,
                principal_id=access.principal_id,
                session_id=access.session_id,
                user_id=access.user_id,
            ),
            limit=3,
        )
        history = [
            {"user": turn.user_content[:4000], "assistant": turn.assistant_content[:4000]}
            for turn in turns
        ]
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
                    surface="agent" if mode == "agent" else "chat",
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
    if decision.scope == "unsupported":
        raise HarborValidationError(
            "This assistant answers questions about indexed knowledge and this conversation. "
            "It cannot create unrelated content; ask about a source or provide relevant material.",
            {"reason": "out_of_scope"},
        )
