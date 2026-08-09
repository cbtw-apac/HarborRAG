"""Durable and advisory lifecycle handling for agent runs."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from harborrag_core.models.chat import HarborChatResponse
from harborrag_core.ports.agent_runs import (
    AgentCheckpoint,
    AgentRunRepository,
    AgentRunStatus,
    AgentStopReason,
)

from .events import AgentEvent, emit
from .execution import ChatAndToolExecutor
from .loop_state import LoopState, RunContext
from .schemas import AgentRunResult


class AgentRunLifecycle:
    """Persist authoritative checkpoints and publish advisory projections."""

    def __init__(
        self,
        executor: ChatAndToolExecutor,
        runs: AgentRunRepository | None,
    ) -> None:
        self._executor = executor
        self._runs = runs

    async def record_failure(self, context: RunContext, state: LoopState) -> None:
        """Best-effort checkpoint and event for an unexpected loop exception."""

        with contextlib.suppress(Exception):
            await self.persist(context, state, AgentRunStatus.FAILED)
        with contextlib.suppress(Exception):
            await emit(
                context.events,
                AgentEvent("run.failed", context.identity.run_id, {"step": state.step}),
            )

    async def record_cancellation(self, context: RunContext, state: LoopState) -> None:
        """Best-effort durable cancellation convergence before propagation."""

        with contextlib.suppress(Exception):
            await self.persist(context, state, AgentRunStatus.CANCELLED)
        with contextlib.suppress(Exception):
            await emit(
                context.events,
                AgentEvent("run.cancelled", context.identity.run_id, {"step": state.step}),
            )

    async def complete(
        self,
        context: RunContext,
        state: LoopState,
        stop_reason: AgentStopReason,
        final_response: HarborChatResponse,
        calls_made: int,
    ) -> AgentRunResult:
        """Commit completion before updating advisory memory and event sinks."""

        run_id = context.identity.run_id
        await self.persist(
            context,
            state,
            AgentRunStatus.COMPLETED,
            stop_reason=stop_reason,
            response=final_response,
        )
        with contextlib.suppress(Exception):
            await self._executor.remember(
                context.conversation_identity,
                context.current_user_message,
                final_response.text,
            )
        with contextlib.suppress(Exception):
            await emit(
                context.events,
                AgentEvent(
                    "run.completed",
                    run_id,
                    {"step": state.step, "stop_reason": stop_reason.value},
                ),
            )
        return AgentRunResult(
            run_id,
            final_response,
            tuple(state.executions),
            calls_made,
            state.usage,
            stop_reason,
        )

    async def persist(
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


__all__ = ["AgentRunLifecycle"]
