"""Per-step execution engine for the bounded agent loop.

``AgentService`` (in ``service.py``) handles setup: resolving identity,
conversation history, and the initial checkpoint. Everything after that --
model turns, tool execution, guard checks, and per-step checkpointing --
lives here so ``run()`` and ``resume()`` share one implementation. Model and
tool-provider calls themselves are delegated to ``ChatAndToolExecutor``.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from harborrag_core.invariants import require
from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatTool
from harborrag_core.ports.agent_runs import (
    AgentCheckpoint,
    AgentRunRepository,
    AgentRunStatus,
    AgentStopReason,
    AgentToolExecution,
)
from harborrag_engine.conversation import ConversationIdentity, ConversationMemory

from .events import AgentEvent, emit
from .execution import ChatAndToolExecutor
from .helpers import add_usage
from .loop_state import LoopState, RunContext, StepOutcome
from .protocols import AgentChatModel, AgentToolProvider
from .schemas import AgentRunOptions, AgentRunResult
from .tool_execution import MAX_TOOL_CALLS_PER_TURN, rejected_execution, tool_definition

_SYNTHESIS_INSTRUCTIONS: dict[AgentStopReason, str] = {
    AgentStopReason.MAX_STEPS: (
        "The tool-call budget is exhausted. Answer now using only the evidence already "
        "returned by tools. State clearly when the evidence is insufficient."
    ),
    AgentStopReason.TIMEOUT: (
        "The time budget for tool use is exhausted. Answer now using only the evidence "
        "already returned by tools. State clearly when the evidence is insufficient."
    ),
    AgentStopReason.REPEATED_TOOL_CALL: (
        "The same tool call was repeated with identical arguments, so further tool use "
        "is blocked. Answer now using only the evidence already returned by tools. State "
        "clearly when the evidence is insufficient."
    ),
}


class AgentLoopRunner:
    """Drive one agent run/resume attempt: model turns, tools, checkpointing."""

    def __init__(
        self,
        chat: AgentChatModel,
        tools: AgentToolProvider,
        *,
        memory: ConversationMemory | None,
        runs: AgentRunRepository | None,
    ) -> None:
        self._executor = ChatAndToolExecutor(chat, tools, memory=memory)
        self._memory = memory
        self._runs = runs

    def memory_identity(self, options: AgentRunOptions) -> ConversationIdentity | None:
        if self._memory is None:
            return None
        return ConversationIdentity(
            options.tenant_id,
            options.principal_id,
            options.session_id,
        )

    async def execute(self, context: RunContext, state: LoopState) -> AgentRunResult:
        options = context.options
        specs = self._executor.available_specs(options.tenant_id, graph_search=options.graph_search)
        tool_definitions = tuple(tool_definition(spec, options.graph_search) for spec in specs)
        allowed_names = {spec.name for spec in specs}

        await emit(
            context.events, AgentEvent("run.started", context.identity.run_id, {"step": state.step})
        )
        try:
            stop_reason, final_response, calls_made = await self._run_until_stop(
                context, state, tool_definitions, allowed_names
            )
        except Exception:
            await self._record_failure(context, state)
            raise

        final_response, calls_made = await self._ensure_final_response(
            context, state, stop_reason, final_response, calls_made
        )
        return await self._complete_run(context, state, stop_reason, final_response, calls_made)

    async def _run_until_stop(
        self,
        context: RunContext,
        state: LoopState,
        tool_definitions: tuple[HarborChatTool, ...],
        allowed_names: set[str],
    ) -> tuple[AgentStopReason, HarborChatResponse | None, int]:
        """Run steps until one reports a stop reason, or the step budget runs out."""

        calls_made = state.step - 1
        while state.step <= context.options.max_steps:
            outcome = await self._run_step(context, state, tool_definitions, allowed_names)
            calls_made += outcome.calls_made
            if outcome.stop_reason is not None:
                return outcome.stop_reason, outcome.final_response, calls_made
            state.step += 1
        return AgentStopReason.MAX_STEPS, None, calls_made

    async def _record_failure(self, context: RunContext, state: LoopState) -> None:
        """Best-effort checkpoint + event on an unexpected loop exception.

        Both are advisory, not authoritative: the caller's exception is what
        actually reports the failure, so a secondary error here must never
        replace it.
        """

        with contextlib.suppress(Exception):
            await self._persist(context, state, AgentRunStatus.FAILED)
        with contextlib.suppress(Exception):
            await emit(
                context.events,
                AgentEvent("run.failed", context.identity.run_id, {"step": state.step}),
            )

    async def _ensure_final_response(
        self,
        context: RunContext,
        state: LoopState,
        stop_reason: AgentStopReason,
        final_response: HarborChatResponse | None,
        calls_made: int,
    ) -> tuple[HarborChatResponse, int]:
        """Return the model's own final answer, or synthesize one when stopped early."""

        if stop_reason is AgentStopReason.FINAL_ANSWER:
            return require(final_response, "agent loop ended without a response"), calls_made

        state.conversation.append(HarborChatMessage.developer(_SYNTHESIS_INSTRUCTIONS[stop_reason]))
        response = await self._executor.complete(state.conversation, context.options, tools=())
        state.usage = add_usage(state.usage, response.usage)
        return response, calls_made + 1

    async def _complete_run(
        self,
        context: RunContext,
        state: LoopState,
        stop_reason: AgentStopReason,
        final_response: HarborChatResponse,
        calls_made: int,
    ) -> AgentRunResult:
        run_id = context.identity.run_id
        await self._executor.remember(
            context.conversation_identity, context.current_user_message, final_response.text
        )
        await self._persist(
            context,
            state,
            AgentRunStatus.COMPLETED,
            stop_reason=stop_reason,
            response=final_response,
        )
        await emit(
            context.events,
            AgentEvent(
                "run.completed", run_id, {"step": state.step, "stop_reason": stop_reason.value}
            ),
        )
        return AgentRunResult(
            run_id, final_response, tuple(state.executions), calls_made, state.usage, stop_reason
        )

    async def _run_step(
        self,
        context: RunContext,
        state: LoopState,
        tool_definitions: tuple[HarborChatTool, ...],
        allowed_names: set[str],
    ) -> StepOutcome:
        await emit(
            context.events,
            AgentEvent("agent.step.started", context.identity.run_id, {"step": state.step}),
        )

        if context.guard.timed_out():
            return StepOutcome(calls_made=0, stop_reason=AgentStopReason.TIMEOUT)

        response = await self._request_turn(context, state, tool_definitions)
        if response is None:
            return StepOutcome(calls_made=0, stop_reason=AgentStopReason.TIMEOUT)

        if not response.tool_calls:
            return StepOutcome(
                calls_made=1, stop_reason=AgentStopReason.FINAL_ANSWER, final_response=response
            )

        return await self._dispatch_tool_calls(context, state, response, allowed_names)

    async def _request_turn(
        self,
        context: RunContext,
        state: LoopState,
        tool_definitions: tuple[HarborChatTool, ...],
    ) -> HarborChatResponse | None:
        """Ask the model for one turn; ``None`` means the guard's deadline hit mid-call."""

        try:
            response = await self._executor.complete(
                state.conversation, context.options, tools=tool_definitions, guard=context.guard
            )
        except TimeoutError:
            return None
        state.usage = add_usage(state.usage, response.usage)
        return response

    async def _dispatch_tool_calls(
        self,
        context: RunContext,
        state: LoopState,
        response: HarborChatResponse,
        allowed_names: set[str],
    ) -> StepOutcome:
        run_id = context.identity.run_id
        step = state.step
        guard = context.guard

        state.conversation.append(response.message)
        admitted = response.tool_calls[:MAX_TOOL_CALLS_PER_TURN]
        overflow = response.tool_calls[MAX_TOOL_CALLS_PER_TURN:]
        for call in admitted:
            await emit(
                context.events,
                AgentEvent("tool.started", run_id, {"step": step, "tool": call.function.name}),
            )

        try:
            results = await self._executor.execute_tool_calls(
                admitted,
                step=step,
                options=context.options,
                allowed_names=allowed_names,
                guard=guard,
            )
        except TimeoutError:
            return StepOutcome(calls_made=1, stop_reason=AgentStopReason.TIMEOUT)

        repeated = False
        for message, execution in results:
            await self._record_tool_result(context, state, message, execution)
            if guard.observe_tool_call(execution.tool, execution.arguments_digest):
                repeated = True

        for call in overflow:
            message, execution = rejected_execution(
                call, step=step, error="tool call budget exceeded for this turn"
            )
            await self._record_tool_result(context, state, message, execution)

        await emit(
            context.events,
            AgentEvent(
                "agent.step.completed",
                run_id,
                {"step": step, "tool_calls": len(results) + len(overflow)},
            ),
        )
        await self._persist(context, state, AgentRunStatus.RUNNING)

        if repeated:
            return StepOutcome(calls_made=1, stop_reason=AgentStopReason.REPEATED_TOOL_CALL)
        return StepOutcome(calls_made=1)

    async def _record_tool_result(
        self,
        context: RunContext,
        state: LoopState,
        message: HarborChatMessage,
        execution: AgentToolExecution,
    ) -> None:
        state.conversation.append(message)
        state.executions.append(execution)
        await emit(
            context.events,
            AgentEvent(
                "tool.completed",
                context.identity.run_id,
                {"step": state.step, "tool": execution.tool, "ok": execution.ok},
            ),
        )

    async def _persist(
        self,
        context: RunContext,
        state: LoopState,
        status: AgentRunStatus,
        *,
        stop_reason: AgentStopReason | None = None,
        response: HarborChatResponse | None = None,
    ) -> None:
        if self._runs is None:
            return
        await self._runs.save_step(
            AgentCheckpoint(
                identity=context.identity,
                status=status,
                step=state.step,
                version=state.version,
                messages=tuple(state.conversation),
                executions=tuple(state.executions),
                usage=state.usage,
                stop_reason=stop_reason,
                response=response,
                created_at=context.created_at,
                updated_at=datetime.now(UTC),
            )
        )
        state.version += 1


__all__ = ["AgentLoopRunner"]
