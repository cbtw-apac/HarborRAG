"""Per-step execution engine for the bounded agent loop.

``AgentService`` (in ``service.py``) handles setup: resolving identity,
conversation history, and the initial checkpoint. Everything after that --
model turns, tool execution, guard checks, and per-step checkpointing --
lives here so ``run()`` and ``resume()`` share one implementation. Model and
tool-provider calls themselves are delegated to ``ChatAndToolExecutor``.
"""

from __future__ import annotations

import asyncio
import contextlib

from harborrag_core.invariants import require
from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatTool
from harborrag_core.ports.agent_runs import (
    AgentRunRepository,
    AgentRunStatus,
    AgentStopReason,
    AgentToolExecution,
)
from harborrag_engine.conversation import ConversationIdentity, ConversationMemory

from .events import AgentEvent, emit
from .execution import ChatAndToolExecutor
from .guard import ExecutionGuard, digest_arguments
from .helpers import add_usage
from .loop_state import LoopState, RunContext, StepOutcome
from .protocols import AgentChatModel, AgentToolProvider
from .run_lifecycle import AgentRunLifecycle
from .schemas import AgentRunOptions, AgentRunResult
from .token_budget import TokenBudgetExhausted, completion_token_limit, exhausted_response
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
    AgentStopReason.TOKEN_BUDGET_EXCEEDED: (
        "The total token budget for this run is exhausted. Answer now using only the "
        "evidence already returned by tools. State clearly when the evidence is "
        "insufficient."
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
        self._lifecycle = AgentRunLifecycle(self._executor, runs)

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
            final_response, calls_made = await self._ensure_final_response(
                context, state, stop_reason, final_response, calls_made
            )
            return await self._lifecycle.complete(
                context, state, stop_reason, final_response, calls_made
            )
        except asyncio.CancelledError:
            # Cancellation is a normal terminal outcome, not an unexpected
            # crash. Shield the best-effort checkpoint from the caller's
            # cancellation so a later resume cannot replay stale RUNNING work.
            with contextlib.suppress(BaseException):
                await asyncio.shield(self._lifecycle.record_cancellation(context, state))
            raise
        except Exception:
            await self._lifecycle.record_failure(context, state)
            raise

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
            if self._over_token_budget(context, state):
                return AgentStopReason.TOKEN_BUDGET_EXCEEDED, None, calls_made
            state.step += 1
        return AgentStopReason.MAX_STEPS, None, calls_made

    @staticmethod
    def _over_token_budget(context: RunContext, state: LoopState) -> bool:
        """Cap the run's own accumulated usage, independent of per-call limits.

        Per-call caps (``MAX_TOOL_CALLS_PER_TURN`` x ``MAX_TOOL_RESULT_CHARS``,
        ``max_steps``) are each individually bounded, but nothing previously
        checked their sum -- a full-width run could still accumulate several
        million characters of resent conversation and tool output. This is
        the backstop on the aggregate, checked once per completed step.
        """

        budget = context.options.max_total_tokens
        return budget is not None and (state.usage.total_tokens or 0) >= budget

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
        if stop_reason is AgentStopReason.TOKEN_BUDGET_EXCEEDED:
            return (
                exhausted_response(
                    context.identity.run_id,
                    "The agent token budget was exhausted before another model call could be "
                    "made safely.",
                ),
                calls_made,
            )

        state.conversation.append(HarborChatMessage.developer(_SYNTHESIS_INSTRUCTIONS[stop_reason]))
        # This call must carry its own bound: it runs precisely when the run's
        # own guard has already expired (timeout) or is otherwise stopping
        # early, so reusing `context.guard` here would either hang forever
        # (no timeout configured) or fail instantly (deadline already past).
        synthesis_guard = ExecutionGuard(timeout_seconds=context.options.synthesis_timeout_seconds)
        synthesis_guard.start()
        try:
            completion_limit = completion_token_limit(context, state, ())
        except TokenBudgetExhausted:
            return (
                exhausted_response(
                    context.identity.run_id,
                    "The agent stopped before final synthesis because the remaining token budget "
                    "could not safely fit another model call.",
                ),
                calls_made,
            )
        response = await self._executor.complete(
            state.conversation,
            context.options,
            tools=(),
            guard=synthesis_guard,
            completion_token_limit=completion_limit,
        )
        state.usage = add_usage(state.usage, response.usage)
        return response, calls_made + 1

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

        try:
            response = await self._request_turn(context, state, tool_definitions)
        except TokenBudgetExhausted:
            return StepOutcome(calls_made=0, stop_reason=AgentStopReason.TOKEN_BUDGET_EXCEEDED)
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
            completion_limit = completion_token_limit(context, state, tool_definitions)
            response = await self._executor.complete(
                state.conversation,
                context.options,
                tools=tool_definitions,
                guard=context.guard,
                completion_token_limit=completion_limit,
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
        accepted = []
        rejected: dict[str, tuple[HarborChatMessage, AgentToolExecution]] = {}
        repeated = False
        for call in admitted:
            arguments = call.function.parsed_arguments
            digest = digest_arguments(
                arguments
                if isinstance(arguments, dict)
                else {"__unparsed__": call.function.arguments}
            )
            if guard.observe_tool_call(call.function.name, digest):
                repeated = True
                rejected[call.id] = rejected_execution(
                    call,
                    step=step,
                    error="repeated tool call limit exceeded",
                )
                continue
            accepted.append(call)
            await emit(
                context.events,
                AgentEvent("tool.started", run_id, {"step": step, "tool": call.function.name}),
            )

        try:
            results = await self._executor.execute_tool_calls(
                accepted,
                step=step,
                options=context.options,
                allowed_names=allowed_names,
                guard=guard,
            )
        except TimeoutError:
            return StepOutcome(calls_made=1, stop_reason=AgentStopReason.TIMEOUT)

        result_iterator = iter(results)
        for call in admitted:
            if call.id in rejected:
                message, execution = rejected[call.id]
            else:
                message, execution = next(result_iterator)
            await self._record_tool_result(context, state, message, execution)

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
        await self._lifecycle.persist(context, state, AgentRunStatus.RUNNING)

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


__all__ = ["AgentLoopRunner"]
