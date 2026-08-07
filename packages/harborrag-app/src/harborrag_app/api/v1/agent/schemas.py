"""Strict public contracts for bounded agent completions."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from harborrag_app.api.schemas import ApiModel
from harborrag_app.api.v1.chat.schemas import ChatUsageResponse


class AgentSessionCreateRequest(ApiModel):
    tenant: str = Field(
        default="DEFAULT",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


class AgentSessionResponse(ApiModel):
    session_id: str
    greeting: str


class AgentCompletionRequest(AgentSessionCreateRequest):
    session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    prompt: str = Field(min_length=1, max_length=65_536)
    graph_search: bool = False
    max_steps: int = Field(default=4, ge=1, le=8)
    stream: bool = False


class AgentResumeRequest(AgentSessionCreateRequest):
    session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    graph_search: bool = False
    max_steps: int = Field(default=4, ge=1, le=8)


class AgentMessageResponse(ApiModel):
    role: Literal["assistant"]
    content: str


class AgentToolCallResponse(ApiModel):
    step: int = Field(ge=1)
    tool: str
    ok: bool


class AgentCompletionResponse(ApiModel):
    id: str
    run_id: str
    model: str
    provider: str
    provider_model: str
    message: AgentMessageResponse
    finish_reason: str
    stop_reason: str
    usage: ChatUsageResponse
    turns: int = Field(ge=1)
    tool_call_count: int = Field(ge=0)
    tool_calls: list[AgentToolCallResponse]
    session_id: str
