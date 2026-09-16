"""Strict public contracts for bounded agent completions."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from harborrag_app.api.schemas import ApiModel
from harborrag_app.api.v1.chat.schemas import (
    ChatCompletionResponse,
    ChatToolCallResponse,
    CompletionRequest,
)


class AgentSessionCreateRequest(ApiModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"tenant": "DEFAULT"}]})

    tenant: str = Field(
        default="DEFAULT",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


class AgentSessionResponse(ApiModel):
    session_id: str
    greeting: str


class AgentCompletionRequest(CompletionRequest):
    """A bounded agent turn; omit session_id to create an agent session."""

    mode: Literal["agent"] = "agent"
    graph_search: bool | None = False


class AgentResumeRequest(AgentSessionCreateRequest):
    # The run id travels in the path, so the body only re-states the session
    # the interrupted run belongs to and the budget to finish it under.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "tenant": "DEFAULT",
                    "session_id": "session-0b9c1f2e3d4a5b6c7d8e9f0a1b2c3d4e",
                    "max_steps": 4,
                }
            ]
        }
    )

    session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    graph_search: bool = False
    max_steps: int = Field(default=4, ge=1, le=8)
    # Optional project scope; must exist within ``tenant`` (otherwise ``404``).
    project_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


class AgentMessageResponse(ApiModel):
    role: Literal["assistant"]
    content: str


class AgentToolCallResponse(ApiModel):
    step: int = Field(ge=1)
    tool: str
    ok: bool


class AgentCompletionResponse(ChatCompletionResponse):
    """The shared completion result with required agent execution metadata.

    Keep citations and accounting identical for initial and resumed runs.
    Duplicating this schema previously rejected the runtime's citation fields.
    """

    mode: Literal["agent"] = "agent"
    run_id: str
    stop_reason: str
    turns: int = Field(ge=1)
    tool_call_count: int = Field(ge=0)
    tool_calls: list[ChatToolCallResponse] = Field(...)
