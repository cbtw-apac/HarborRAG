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
from .synthesis import synthesis_instruction
from .token_budget import (
    MIN_SYNTHESIS_COMPLETION_TOKENS,
    SYNTHESIS_RESERVE_TOKENS,
    TokenBudgetExhausted,
    completion_token_limit,
    exhausted_response,
    over_token_budget,
)
from .tool_execution import (
    MAX_TOOL_CALLS_PER_TURN,
    rejected_execution,
    tool_definition,
    turn_replies,
)


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

    @property
    def lifecycle(self) -> AgentRunLifecycle:
        return self._lifecycle

    def memory_identity(self, options: AgentRunOptions) -> ConversationIdentity | None:
        if self._memory is None:
            return None
        return ConversationIdentity(
            options.tenant_id,
            options.principal_id,
            options.session_id,
            options.owner_id,
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
        except Exception as error:
            await self._lifecycle.record_failure(context, state, error)
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
            if over_token_budget(context, state):
                return AgentStopReason.TOKEN_BUDGET_EXCEEDED, None, calls_made
            state.step += 1
        return AgentStopReason.MAX_STEPS, None, calls_made

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

        state.conversation.append(HarborChatMessage.developer(synthesis_instruction(stop_reason)))
        # This call must carry its own bound: it runs precisely when the run's
        # own guard has already expired (timeout) or is otherwise stopping
        # early, so reusing `context.guard` here would either hang forever
        # (no timeout configured) or fail instantly (deadline already past).
        synthesis_guard = ExecutionGuard(timeout_seconds=context.options.synthesis_timeout_seconds)
        synthesis_guard.start()
        try:
            # A budget stop still gets a real (tool-free) synthesis turn with
            # whatever headroom the tool turns reserved for it; only when even
            # the minimum synthesis cap cannot fit do we fall back to text.
            completion_limit = completion_token_limit(
                context, state, (), minimum=MIN_SYNTHESIS_COMPLETION_TOKENS
            )
        except TokenBudgetExhausted:
            response = exhausted_response(
                context.identity.run_id,
                "The agent token budget was exhausted before a final synthesis model call "
                "could be made safely.",
            )
            state.conversation.append(response.message)
            return response, calls_made
        response = await self._executor.complete(
            state.conversation,
            context.options,
            tools=(),
            guard=synthesis_guard,
            completion_token_limit=completion_limit,
        )
        state.usage = add_usage(state.usage, response.usage)
        state.conversation.append(response.message)
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
            # Keep the checkpointed transcript complete: it must end with the
            # assistant's answer, not the last tool result.
            state.conversation.append(response.message)
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
            completion_limit = completion_token_limit(
                context, state, tool_definitions, reserve=SYNTHESIS_RESERVE_TOKENS
            )
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
            await self._record_turn_replies(
                context, state, response, rejected, "tool call timed out"
            )
            return StepOutcome(calls_made=1, stop_reason=AgentStopReason.TIMEOUT)
        except Exception:
            await self._record_turn_replies(context, state, response, rejected, "tool call failed")
            raise

        replies = rejected | dict(zip((call.id for call in accepted), results, strict=True))
        await self._record_turn_replies(context, state, response, replies, "tool call failed")

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

    async def _record_turn_replies(
        self,
        context: RunContext,
        state: LoopState,
        response: HarborChatResponse,
        replies: dict[str, tuple[HarborChatMessage, AgentToolExecution]],
        fallback_error: str,
    ) -> None:
        """Record one reply per tool call the model issued, dangling none.

        ``fallback_error`` answers admitted calls with no prepared reply --
        the case when execution aborted (gather timeout, tool-phase
        exception) before results existed -- so the synthesis request and
        any later resume still see a well-formed conversation.
        """

        for message, execution in turn_replies(
            response.tool_calls, step=state.step, replies=replies, fallback_error=fallback_error
        ):
            await self._record_tool_result(context, state, message, execution)

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
