"""Durable and advisory lifecycle handling for agent runs."""

from __future__ import annotations

import contextlib
import logging
import os
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from harborrag_core.contracts.errors import (
    HarborConnectionError,
    HarborDeadlineExceeded,
    HarborRateLimitError,
    HarborUnavailableError,
)
from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatUsage
from harborrag_core.models.errors import HarborModelError
from harborrag_core.ports.agent_runs import (
    AgentCheckpoint,
    AgentRunIdentity,
    AgentRunRepository,
    AgentRunStatus,
    AgentStopReason,
)

from .events import AgentEvent, emit
from .execution import ChatAndToolExecutor
from .loop_state import LoopState, RunContext
from .schemas import AgentRunOptions, AgentRunResult

logger = logging.getLogger("harborrag.engine.agent")

# Lease horizon for runs with no wall-clock timeout: a live worker refreshes
# the lease on every persisted step, so this only has to outlast one step.
DEFAULT_AGENT_RUN_LEASE_SECONDS = 300.0
# Slack added on top of a run's own deadline so a lease never lapses while the
# run it fences can still legitimately be executing.
_LEASE_GRACE_SECONDS = 30.0
# Errors whose cause is expected to clear on its own; a run that failed on
# one of these is safe to resume from its last checkpoint.
_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    HarborConnectionError,
    HarborDeadlineExceeded,
    HarborRateLimitError,
    HarborUnavailableError,
    TimeoutError,
    ConnectionError,
)


def new_lease_owner() -> str:
    """Identify this executing process uniquely enough to fence a run lease."""

    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"


def lease_seconds(options: AgentRunOptions) -> float:
    """How long a lease taken by a run with ``options`` stays valid unrefreshed.

    A run bounded by ``timeout_seconds`` cannot legitimately outlive its own
    deadline plus the synthesis bound, so the lease covers exactly that (plus
    grace); an unbounded run falls back to a fixed horizon that every
    persisted step refreshes.
    """

    if options.timeout_seconds is None:
        return DEFAULT_AGENT_RUN_LEASE_SECONDS
    synthesis = options.synthesis_timeout_seconds or 0.0
    return options.timeout_seconds + synthesis + _LEASE_GRACE_SECONDS


def is_retryable_failure(error: BaseException) -> bool:
    """Classify whether a run that died on ``error`` may be resumed later.

    Provider errors carry their own ``retryable`` verdict; core transport
    errors (connection, deadline, rate limit, unavailable backend) and plain
    timeouts are transient by nature. Everything else -- programming errors,
    validation, checkpoint version conflicts -- is not.
    """

    if isinstance(error, HarborModelError):
        return bool(error.retryable)
    return isinstance(error, _TRANSIENT_ERRORS)


@dataclass(frozen=True, slots=True)
class CheckpointOutcome:
    """Terminal-only checkpoint fields; a RUNNING save uses the empty default.

    ``stop_reason``/``response`` accompany COMPLETED, ``failure_retryable``
    accompanies FAILED (see :func:`is_retryable_failure`).
    """

    stop_reason: AgentStopReason | None = None
    response: HarborChatResponse | None = None
    failure_retryable: bool = False


class AgentRunLifecycle:
    """Persist authoritative checkpoints and publish advisory projections."""

    def __init__(
        self,
        executor: ChatAndToolExecutor,
        runs: AgentRunRepository | None,
        *,
        lease_owner: str | None = None,
    ) -> None:
        self._executor = executor
        self._runs = runs
        self._lease_owner = lease_owner or new_lease_owner()

    @property
    def lease_owner(self) -> str:
        return self._lease_owner

    async def create(
        self,
        identity: AgentRunIdentity,
        options: AgentRunOptions,
        messages: Sequence[HarborChatMessage],
        created_at: datetime,
    ) -> None:
        """Insert the initial RUNNING checkpoint, already leased to this worker."""

        if self._runs is None:
            return
        await self._runs.create(
            AgentCheckpoint(
                identity=identity,
                status=AgentRunStatus.RUNNING,
                step=0,
                version=1,
                messages=tuple(messages),
                executions=(),
                usage=HarborChatUsage(),
                stop_reason=None,
                response=None,
                created_at=created_at,
                updated_at=created_at,
                lease_owner=self._lease_owner,
                lease_expires_at=created_at + timedelta(seconds=lease_seconds(options)),
            )
        )

    async def record_failure(
        self, context: RunContext, state: LoopState, error: BaseException
    ) -> None:
        """Best-effort checkpoint and event for an unexpected loop exception."""

        with contextlib.suppress(Exception):
            await self.persist(
                context,
                state,
                AgentRunStatus.FAILED,
                CheckpointOutcome(failure_retryable=is_retryable_failure(error)),
            )
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
            CheckpointOutcome(stop_reason=stop_reason, response=final_response),
        )
        try:
            await self._executor.remember(
                context.conversation_identity,
                context.current_user_message,
                final_response,
                run_id=run_id,
            )
        except Exception as error:  # noqa: BLE001 - memory is advisory; the run stays complete
            # The checkpoint above is authoritative and already committed, so
            # the run succeeds -- but a silently missing memory turn degrades
            # every later turn in the session, so it must be loud.
            logger.error(
                "agent run %s memory update failed (tenant=%s session=%s principal=%s): %s",
                run_id,
                context.identity.tenant_id,
                context.identity.session_id,
                context.identity.principal_id,
                error,
                exc_info=error,
                extra={
                    "run_id": run_id,
                    "tenant_id": context.identity.tenant_id,
                    "session_id": context.identity.session_id,
                    "principal_id": context.identity.principal_id,
                    "error_type": type(error).__name__,
                },
            )
            with contextlib.suppress(Exception):
                await emit(
                    context.events,
                    AgentEvent(
                        "run.memory_failed",
                        run_id,
                        {"step": state.step, "error_type": type(error).__name__},
                    ),
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
        outcome: CheckpointOutcome = CheckpointOutcome(),
    ) -> None:
        """Save one checkpoint; a RUNNING save refreshes this worker's lease.

        Terminal statuses release the lease (both columns cleared) so a
        stale-but-live worker's expired lease never blocks a later resume.
        """

        if self._runs is None:
            return
        now = datetime.now(UTC)
        running = status is AgentRunStatus.RUNNING
        await self._runs.save_step(
            AgentCheckpoint(
                identity=context.identity,
                status=status,
                step=state.step,
                version=state.version,
                messages=tuple(state.conversation),
                executions=tuple(state.executions),
                usage=state.usage,
                stop_reason=outcome.stop_reason,
                response=outcome.response,
                created_at=context.created_at,
                updated_at=now,
                failure_retryable=outcome.failure_retryable,
                lease_owner=self._lease_owner if running else None,
                lease_expires_at=(
                    now + timedelta(seconds=lease_seconds(context.options)) if running else None
                ),
            )
        )
        state.version += 1


__all__ = [
    "DEFAULT_AGENT_RUN_LEASE_SECONDS",
    "AgentRunLifecycle",
    "CheckpointOutcome",
    "is_retryable_failure",
    "lease_seconds",
    "new_lease_owner",
]
