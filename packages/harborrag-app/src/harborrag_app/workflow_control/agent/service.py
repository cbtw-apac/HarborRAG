"""Authenticated HTTP application service for bounded multi-turn agents."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import cast

from harborrag_app.workflow_control.chat.prompting import history_messages
from harborrag_app.workflow_control.errors import failure_response
from harborrag_app.workflow_control.memory.context import (
    empty_memory_context,
    memory_context_request,
)
from harborrag_app.workflow_control.memory.extraction import (
    MemoryExtractionQueue,
    submit_exchange,
)
from harborrag_app.workflow_control.memory.identity import MemoryIdentity
from harborrag_app.workflow_control.memory.locks import SessionLocks
from harborrag_app.workflow_control.memory.projects import require_project
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import (
    HarborConfigurationError,
    HarborNotFoundError,
    HarborValidationError,
)
from harborrag_core.models.chat import HarborChatMessage
from harborrag_core.ports.control_plane import ProjectRepositoryPort
from harborrag_core.ports.conversation import ConversationHistoryRepository
from harborrag_core.ports.memory import MemoryIndex, MemoryRepository
from harborrag_core.ports.usage import ModelUsageRepository
from harborrag_runtime.agent import (
    AgentEvent,
    AgentEventSink,
    AgentRunRepository,
    AgentRunResult,
    AgentService,
)
from harborrag_runtime.agent.tools import RuntimeAgentToolProvider
from harborrag_runtime.memory import MemoryContext
from harborrag_runtime.sdk import HarborRAG

from .options import AgentExecutionOptions
from .support import DefaultPromptChat, agent_timeout_seconds, result_data, run_options
from .turn import record_run_usage, remembered_exchange

# Re-exported for callers that historically imported the helpers from here.
_run_options = run_options
_result_data = result_data

type RuntimeProvider = Callable[[], HarborRAG]

logger = logging.getLogger("harborrag.app.workflow_control.agent")


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

    def __init__(  # noqa: PLR0913 - one keyword-only collaborator per injected port
        self,
        runtime_provider: RuntimeProvider,
        *,
        memory: ConversationHistoryRepository,
        runs: AgentRunRepository,
        projects: ProjectRepositoryPort | None = None,
        memories: MemoryRepository | None = None,
        index: MemoryIndex | None = None,
        extraction: MemoryExtractionQueue | None = None,
        locks: SessionLocks | None = None,
        memory_tools: bool = False,
        usage: ModelUsageRepository | None = None,
    ) -> None:
        self._runtime_provider = runtime_provider
        self._memory = memory
        self._runs = runs
        self._projects = projects
        self._memories = memories
        self._index = index
        self._extraction = extraction
        self._usage = usage
        # HARBORRAG_MEMORY_AGENT_TOOLS; off unless the deployment opted in.
        self._memory_tools = memory_tools
        # Serializes runs per session so the engine's ``recent -> append`` on
        # conversation memory cannot interleave across concurrent requests.
        self._locks = locks or SessionLocks()

    def _agent_service(
        self,
        identity: MemoryIdentity,
        *,
        model: str | None = None,
    ) -> AgentService:
        """Compose the engine agent over tools bound to this run's caller.

        The memory owner is handed to the tool provider here, from the
        authenticated identity, which is what keeps the agent's memory tools
        unable to read or write as anybody else: nothing the model emits can
        reach it.
        """

        runtime = self._runtime_provider()
        return AgentService(
            DefaultPromptChat(runtime.chat, model),
            RuntimeAgentToolProvider(
                runtime,
                memories=self._memories,
                index=self._index,
                memory_owner=identity.owner(),
                memory_tools_enabled=self._memory_tools,
            ),
            memory=self._memory,
            runs=self._runs,
        )

    async def _require_scope(self, identity: MemoryIdentity) -> None:
        """Both the session and the optional project must exist for this caller."""

        if not await self._memory.exists(identity.conversation(), kind="agent"):
            raise HarborNotFoundError("Conversation session was not found")
        await require_project(self._projects, identity.project_id, tenant_id=identity.tenant_id)

    async def _run_agent(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
        events: AgentEventSink | None = None,
    ) -> AgentRunResult:
        identity = _identity(tenant_id, principal_id, options)
        async with self._locks.hold(identity.conversation()):
            context = await self._context(identity, query)
            result = await self._agent_service(identity, model=options.model).run(
                (HarborChatMessage.user(query),),
                run_options(
                    tenant_id,
                    principal_id,
                    options,
                    history=history_messages(context.messages),
                    memory_summary=context.summary,
                ),
                events=events,
            )
            remembered = await remembered_exchange(
                self._memory, identity, result, extraction=self._extraction
            )
            await record_run_usage(self._usage, identity, result)
        submit_exchange(self._extraction, identity, query, remembered)
        return result

    async def _context(self, identity: MemoryIdentity, query: str) -> MemoryContext:
        """Apply the memory policy; degrade to no history rather than fail the run."""

        try:
            return await self._runtime_provider().memory.build_context(
                memory_context_request(identity, query),
                messages=self._memory,
                memories=self._memories,
                index=self._index,
            )
        except Exception:  # noqa: BLE001 - non-fatal by design
            logger.exception(
                "Conversation memory context failed for tenant=%s session=%s; "
                "running the agent without history",
                identity.tenant_id,
                identity.session_id,
            )
            return empty_memory_context(query)

    async def validate_model(self, model: str | None, *, tenant_id: str) -> None:
        """Reject a model name this tenant may not use, before the run starts.

        Same rule and same authority as the chat surface; a resumed run keeps
        the model it started under and never revalidates.
        """

        await self._runtime_provider().chat.validate_model(model, tenant_id=tenant_id)

    async def complete(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: AgentExecutionOptions,
    ) -> AppResponse:
        await self._require_scope(_identity(tenant_id, principal_id, options))
        try:
            result = await self._run_agent(
                query, tenant_id=tenant_id, principal_id=principal_id, options=options
            )
            return AppResponse(True, _project(result, options))
        except HarborValidationError:
            # A caller-supplied value the transport must report as a 422.
            raise
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
            await self._require_scope(_identity(tenant_id, principal_id, options))
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
                    yield {"kind": "result", "result": _project(result, options)}
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
        identity = _identity(tenant_id, principal_id, options)
        try:
            await require_project(self._projects, identity.project_id, tenant_id=tenant_id)
            async with self._locks.hold(identity.conversation()):
                result = await self._agent_service(identity).resume(
                    run_id, run_options(tenant_id, principal_id, options)
                )
                # A resumed run spends real tokens too, so it is accounted for
                # like a fresh one; the run_id ties both rows to the same run.
                await record_run_usage(self._usage, identity, result)
            return AppResponse(True, _project(result, options))
        except (HarborNotFoundError, HarborConfigurationError):
            # Known, mapped domain errors (unresumable/unknown run, no checkpoint
            # backend configured) propagate for the transport layer to translate
            # into the right status code, matching `complete()`'s session-not-found
            # check -- only unexpected execution failures become AppResponse(False).
            raise
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            return failure_response(logger, exc, "resume agent run")


def _identity(tenant_id: str, principal_id: str, options: AgentExecutionOptions) -> MemoryIdentity:
    return MemoryIdentity.build(
        tenant_id=tenant_id,
        principal_id=principal_id,
        session_id=options.session_id,
        user_id=options.user_id,
        project_id=options.project_id,
    )


def _project(result: AgentRunResult, options: AgentExecutionOptions) -> dict[str, object]:
    return result_data(result, session_id=options.session_id, project_id=options.project_id)


__all__ = ["AgentApplicationService", "agent_timeout_seconds"]
