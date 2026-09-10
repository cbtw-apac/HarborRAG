"""Strict public contracts for bounded agent completions."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from harborrag_app.api.schemas import ApiModel
from harborrag_app.api.v1.chat.schemas import ChatUsageResponse


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


class AgentCompletionRequest(AgentSessionCreateRequest):
    # Only ``session_id`` and ``prompt`` are required. Without an explicit
    # example the docs generate a value for every optional field from its
    # pattern, and pasting those back names a project and a model that do not
    # exist. The session id must be an agent session, not a chat one.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "tenant": "DEFAULT",
                    "session_id": "session-0b9c1f2e3d4a5b6c7d8e9f0a1b2c3d4e",
                    "prompt": "Connect the release policy to its owning service.",
                    "graph_search": True,
                    "max_steps": 4,
                    "stream": False,
                }
            ]
        }
    )

    session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    prompt: str = Field(min_length=1, max_length=65_536)
    graph_search: bool = False
    max_steps: int = Field(default=4, ge=1, le=8)
    stream: bool = False
    # Optional project scope; must exist within ``tenant`` (otherwise ``404``).
    project_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    # Optional logical model name, bounded exactly as the chat surface bounds
    # it. A resumed run keeps the model it started under, so there is
    # deliberately no equivalent field on ``AgentResumeRequest``.
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


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
    # The validated project the run was scoped to; null when none was given.
    project_id: str | None = None
