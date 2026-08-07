"""Authenticated HTTP application service for bounded multi-turn agents."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import cast

from harborrag_app.workflow_control.errors import failure_response
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import HarborConfigurationError, HarborNotFoundError
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest, HarborChatResponse
from harborrag_runtime.agent import (
    AgentEvent,
    AgentEventSink,
    AgentRunOptions,
    AgentRunRepository,
    AgentRunResult,
    AgentService,
)
from harborrag_runtime.agent.tools import RuntimeAgentToolProvider
from harborrag_runtime.chat import ChatFacade, ChatPrompt
from harborrag_runtime.memory import ConversationIdentity, ConversationRepository
from harborrag_runtime.sdk import HarborRAG

from .options import AgentExecutionOptions

type RuntimeProvider = Callable[[], HarborRAG]

logger = logging.getLogger("harborrag.app.workflow_control.agent")


@dataclass(frozen=True, slots=True)
class _DefaultPromptChat:
    facade: ChatFacade

    async def complete(self, request: HarborChatRequest) -> HarborChatResponse:
        return await self.facade.complete(request, prompt=ChatPrompt.DEFAULT)


def _run_options(
    tenant_id: str,
    principal_id: str,
    options: AgentExecutionOptions,
) -> AgentRunOptions:
    return AgentRunOptions(
        tenant_id=tenant_id,
        principal_id=principal_id,
        session_id=options.session_id,
        graph_search=options.graph_search,
        max_steps=options.max_steps,
    )


def _result_data(result: AgentRunResult, *, session_id: str) -> dict[str, object]:
    response = result.response
    return {
        "id": response.id,
        "run_id": result.run_id,
        "model": response.logical_model,
        "provider": response.provider,
        "provider_model": response.provider_model,
        "message": {"role": "assistant", "content": response.text},
        "finish_reason": str(response.finish_reason),
        "stop_reason": result.stop_reason.value,
        "usage": result.usage.model_dump(mode="json"),
        "turns": result.turns,
        "tool_call_count": len(result.executions),
        "tool_calls": [
            {
                "step": execution.step,
                "tool": execution.tool,
                "ok": execution.ok,
            }
            for execution in result.executions
        ],
        "session_id": session_id,
    }


@dataclass(frozen=True, slots=True)
class _StreamItem:
    """One entry on the producer/consumer queue bridging the callback-style
    ``AgentEventSink`` into an async generator: either a progress event, the
    terminal result, or a terminal failure -- exactly one of the latter two
    always ends the stream.
    """

    kind: str
    payload: object


class AgentApplicationService:
    """Execute the shared engine agent over runtime-native retrieval tools."""

    def __init__(
        self,
        runtime_provider: RuntimeProvider,
        *,
        memory: ConversationRepository,
        runs: AgentRunRepository,
    ) -> None:
        self._runtime_provider = runtime_provider
        self._memory = memory
        self._runs = runs

    def _agent_service(self) -> AgentService:
        runtime = self._runtime_provider()
        return AgentService(
            _DefaultPromptChat(runtime.chat),
            RuntimeAgentToolProvider(runtime),
            memory=self._memory,
            runs=self._runs,
        )

    async def _require_session(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        session_id: str,
    ) -> None:
        identity = ConversationIdentity(tenant_id, principal_id, session_id)
        if not await self._memory.exists(identity):
            raise HarborNotFoundError("Conversation session was not found")

    async def _run_agent(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
        events: AgentEventSink | None = None,
    ) -> AgentRunResult:
        return await self._agent_service().run(
            (HarborChatMessage.user(query),),
            _run_options(tenant_id, principal_id, options),
            events=events,
        )

    async def complete(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AppResponse:
        await self._require_session(
            tenant_id=tenant_id, principal_id=principal_id, session_id=options.session_id
        )
        try:
            result = await self._run_agent(
                query, tenant_id=tenant_id, principal_id=principal_id, options=options
            )
            return AppResponse(True, _result_data(result, session_id=options.session_id))
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            return failure_response(logger, exc, "run agent completion")

    async def stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AsyncIterator[dict[str, object]]:
        """Yield ``{"kind": ...}`` events: many ``event``, exactly one terminal
        ``result`` or ``error``. A transport adapts these into SSE frames.

        Progress events come from the engine's ``AgentEventSink`` callback,
        which runs inside the background task driving ``AgentService.run``;
        they are bridged onto this generator through a queue so the callback
        style never leaks past this method. If the caller stops iterating
        (an SSE client disconnects), the ``finally`` block cancels that task
        instead of letting it keep running -- and spending tokens -- for a
        response nobody reads.
        """

        try:
            await self._require_session(
                tenant_id=tenant_id, principal_id=principal_id, session_id=options.session_id
            )
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            failure = failure_response(logger, exc, "prepare agent run stream")
            yield {"kind": "error", "error": failure.error}
            return

        queue: asyncio.Queue[_StreamItem] = asyncio.Queue()

        async def sink(event: AgentEvent) -> None:
            await queue.put(_StreamItem("event", event))

        async def produce() -> None:
            try:
                result = await self._run_agent(
                    query,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    options=options,
                    events=sink,
                )
                await queue.put(_StreamItem("result", result))
            except Exception as exc:  # noqa: BLE001 - reported in-band, not raised
                await queue.put(_StreamItem("error", exc))

        task = asyncio.create_task(produce())
        try:
            while True:
                item = await queue.get()
                if item.kind == "event":
                    event = cast("AgentEvent", item.payload)
                    yield {
                        "kind": "event",
                        "event": {
                            "name": event.kind,
                            "run_id": event.run_id,
                            "data": dict(event.data),
                        },
                    }
                    continue
                if item.kind == "result":
                    result = cast("AgentRunResult", item.payload)
                    yield {
                        "kind": "result",
                        "result": _result_data(result, session_id=options.session_id),
                    }
                    return
                failure = failure_response(
                    logger,
                    cast("Exception", item.payload),
                    "run agent completion stream",
                )
                yield {"kind": "error", "error": failure.error}
                return
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task

    async def resume(
        self,
        run_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AppResponse:
        try:
            result = await self._agent_service().resume(
                run_id, _run_options(tenant_id, principal_id, options)
            )
            return AppResponse(True, _result_data(result, session_id=options.session_id))
        except (HarborNotFoundError, HarborConfigurationError):
            # Known, mapped domain errors (unresumable/unknown run, no checkpoint
            # backend configured) propagate for the transport layer to translate
            # into the right status code, matching `complete()`'s session-not-found
            # check -- only unexpected execution failures become AppResponse(False).
            raise
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            return failure_response(logger, exc, "resume agent run")


__all__ = ["AgentApplicationService"]
