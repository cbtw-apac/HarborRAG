"""SQL agent-run checkpoint adapter for the control-plane database."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult

from harborrag_adapters.repositories.database.control_plane.schemas_agent_memory import (
    AgentRunRow,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatUsage
from harborrag_core.ports.agent_runs import (
    AgentCheckpoint,
    AgentRunIdentity,
    AgentRunStatus,
    AgentStopReason,
    AgentToolExecution,
)


def _state_to_json(checkpoint: AgentCheckpoint) -> dict[str, Any]:
    return {
        "messages": [message.model_dump(mode="json") for message in checkpoint.messages],
        "executions": [
            {
                "step": execution.step,
                "call_id": execution.call_id,
                "tool": execution.tool,
                "ok": execution.ok,
                "arguments_digest": execution.arguments_digest,
            }
            for execution in checkpoint.executions
        ],
        "usage": checkpoint.usage.model_dump(mode="json"),
        "response": (
            checkpoint.response.model_dump(mode="json") if checkpoint.response is not None else None
        ),
    }


def _state_from_json(
    data: dict[str, Any],
) -> tuple[
    tuple[HarborChatMessage, ...],
    tuple[AgentToolExecution, ...],
    HarborChatUsage,
    HarborChatResponse | None,
]:
    messages = tuple(HarborChatMessage.model_validate(item) for item in data["messages"])
    executions = tuple(
        AgentToolExecution(
            step=item["step"],
            call_id=item["call_id"],
            tool=item["tool"],
            ok=item["ok"],
            arguments_digest=item["arguments_digest"],
        )
        for item in data["executions"]
    )
    usage = HarborChatUsage.model_validate(data["usage"])
    response = (
        HarborChatResponse.model_validate(data["response"])
        if data["response"] is not None
        else None
    )
    return messages, executions, usage, response


def _stop_reason_value(checkpoint: AgentCheckpoint) -> str | None:
    return checkpoint.stop_reason.value if checkpoint.stop_reason is not None else None


def _row_values(checkpoint: AgentCheckpoint) -> dict[str, Any]:
    """Column values common to both an initial insert and every later update."""

    return {
        "status": checkpoint.status.value,
        "step": checkpoint.step,
        "stop_reason": _stop_reason_value(checkpoint),
        "version": checkpoint.version,
        "state_json": _state_to_json(checkpoint),
        "updated_at": checkpoint.updated_at,
    }


def _row_to_checkpoint(row: AgentRunRow) -> AgentCheckpoint:
    messages, executions, usage, response = _state_from_json(row.state_json)
    return AgentCheckpoint(
        identity=AgentRunIdentity(
            tenant_id=row.tenant_id,
            principal_id=row.principal_id,
            session_id=row.session_id,
            run_id=row.run_id,
        ),
        status=AgentRunStatus(row.status),
        step=row.step,
        version=row.version,
        messages=messages,
        executions=executions,
        usage=usage,
        stop_reason=AgentStopReason(row.stop_reason) if row.stop_reason is not None else None,
        response=response,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@dataclass(slots=True)
class SqlAgentRunRepository:
    """Persist resumable agent-run checkpoints through async SQLAlchemy.

    ``save_step`` is optimistic-concurrency guarded: it only applies when the
    stored row is still at ``checkpoint.version - 1``, so a stale writer (a
    second resume racing the still-running original, or a resume racing a
    step the original run just persisted) is rejected with
    ``HarborConflictError`` rather than silently overwriting newer state.
    """

    sessions: SessionFactory

    async def create(self, checkpoint: AgentCheckpoint) -> None:
        async with self.sessions.begin() as session:
            session.add(
                AgentRunRow(
                    run_id=checkpoint.identity.run_id,
                    tenant_id=checkpoint.identity.tenant_id,
                    principal_id=checkpoint.identity.principal_id,
                    session_id=checkpoint.identity.session_id,
                    created_at=checkpoint.created_at,
                    **_row_values(checkpoint),
                )
            )

    async def save_step(self, checkpoint: AgentCheckpoint) -> None:
        async with self.sessions.begin() as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    sa.update(AgentRunRow)
                    .where(
                        AgentRunRow.run_id == checkpoint.identity.run_id,
                        AgentRunRow.tenant_id == checkpoint.identity.tenant_id,
                        AgentRunRow.principal_id == checkpoint.identity.principal_id,
                        AgentRunRow.session_id == checkpoint.identity.session_id,
                        AgentRunRow.version == checkpoint.version - 1,
                    )
                    .values(**_row_values(checkpoint))
                ),
            )
            if result.rowcount == 0:
                raise HarborConflictError(
                    f"agent run {checkpoint.identity.run_id!r} checkpoint version conflict"
                )

    async def get(self, identity: AgentRunIdentity) -> AgentCheckpoint | None:
        statement = sa.select(AgentRunRow).where(
            AgentRunRow.run_id == identity.run_id,
            AgentRunRow.tenant_id == identity.tenant_id,
            AgentRunRow.principal_id == identity.principal_id,
            AgentRunRow.session_id == identity.session_id,
        )
        async with self.sessions() as session:
            row = await session.scalar(statement)
        return None if row is None else _row_to_checkpoint(row)


__all__ = ["SqlAgentRunRepository"]
