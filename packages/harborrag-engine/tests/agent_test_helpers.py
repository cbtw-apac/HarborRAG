"""Shared fakes and builders for the agent-loop test suite."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatResponse,
    HarborChatUsage,
    HarborToolCall,
    HarborToolCallFunction,
)
from harborrag_core.ports.agent_runs import AgentCheckpoint, AgentRunIdentity


@dataclass(frozen=True)
class Spec:
    name: str
    description: str = "test tool"
    input_schema: dict = None  # type: ignore[assignment]
    capability: str = "read"

    def __post_init__(self) -> None:
        if self.input_schema is None:
            object.__setattr__(
                self,
                "input_schema",
                {
                    "type": "object",
                    "properties": {
                        "tenant_id": {"type": "string"},
                        "observe_graph": {"type": "boolean"},
                    },
                },
            )


class Tools:
    def __init__(self) -> None:
        self.specs = [
            Spec("vector_search_advanced"),
            Spec("graph_path_search"),
            Spec("agent"),
        ]
        self.calls: list[tuple[str, dict[str, object], str]] = []

    def list_tools(self, tenant_id=None):
        self.listed_for = tenant_id
        return self.specs

    async def call_tool(self, name, arguments=None, *, principal_id="in-process"):
        payload = dict(arguments or {})
        self.calls.append((name, payload, principal_id))
        return {"ok": True, "results": [{"text": f"result from {name}"}]}


class Memory:
    def __init__(self) -> None:
        self.turns = {}

    async def recent(self, identity, *, limit=2):
        return self.turns.get(identity, ())[-limit:]

    async def append(self, identity, turn):
        self.turns[identity] = (*self.turns.get(identity, ()), turn)

    async def clear(self, identity):
        self.turns.pop(identity, None)


class Chat:
    def __init__(self, responses: list[HarborChatResponse]) -> None:
        self.responses = responses
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


class SlowThenFastChat:
    """First call sleeps past its deadline; later calls return immediately."""

    def __init__(self, *, delay: float, response: HarborChatResponse) -> None:
        self._delay = delay
        self._response = response
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(self._delay)
        return self._response


class AlwaysSlowChat:
    """Every call sleeps past its deadline, including the post-timeout synthesis call."""

    def __init__(self, *, delay: float, response: HarborChatResponse) -> None:
        self._delay = delay
        self._response = response
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        await asyncio.sleep(self._delay)
        return self._response


class Runs:
    """In-memory AgentRunRepository fake enforcing optimistic-concurrency versioning."""

    def __init__(self) -> None:
        self.checkpoints: dict[str, AgentCheckpoint] = {}

    async def create(self, checkpoint: AgentCheckpoint) -> None:
        self.checkpoints[checkpoint.identity.run_id] = checkpoint

    async def save_step(self, checkpoint: AgentCheckpoint) -> None:
        current = self.checkpoints.get(checkpoint.identity.run_id)
        if current is None or current.version != checkpoint.version - 1:
            raise HarborConflictError("stale agent-run checkpoint version")
        self.checkpoints[checkpoint.identity.run_id] = checkpoint

    async def get(self, identity: AgentRunIdentity) -> AgentCheckpoint | None:
        checkpoint = self.checkpoints.get(identity.run_id)
        if checkpoint is None:
            return None
        owner = checkpoint.identity
        if (owner.tenant_id, owner.principal_id, owner.session_id) != (
            identity.tenant_id,
            identity.principal_id,
            identity.session_id,
        ):
            return None
        return checkpoint


def response(*, call: tuple[str, str, str] | None = None, text: str = "answer"):
    tool_calls = ()
    content = text
    finish_reason = "stop"
    if call is not None:
        call_id, name, arguments = call
        tool_calls = (
            HarborToolCall(
                id=call_id,
                function=HarborToolCallFunction(
                    name=name,
                    arguments=arguments,
                    parsed_arguments=json.loads(arguments),
                ),
            ),
        )
        content = None
        finish_reason = "tool_calls"
    return HarborChatResponse(
        id="response",
        logical_model="primary",
        provider="mock",
        provider_model="mock-chat",
        deployment="private",
        message=HarborChatMessage.assistant(content, tool_calls=tool_calls),
        finish_reason=finish_reason,
        usage=HarborChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def many_tool_calls_response(count: int) -> HarborChatResponse:
    tool_calls = tuple(
        HarborToolCall(
            id=f"call-{index}",
            function=HarborToolCallFunction(
                name="vector_search_advanced",
                arguments="{}",
                parsed_arguments={},
            ),
        )
        for index in range(count)
    )
    return HarborChatResponse(
        id="response",
        logical_model="primary",
        provider="mock",
        provider_model="mock-chat",
        deployment="private",
        message=HarborChatMessage.assistant(None, tool_calls=tool_calls),
        finish_reason="tool_calls",
        usage=HarborChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )
