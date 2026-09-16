"""Database-backed atomic completion claims and terminal response replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import cast

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.database.control_plane.mapping import utc_now
from harborrag_adapters.repositories.database.control_plane.schemas_agent_memory import (
    ConversationSessionRow,
)
from harborrag_adapters.repositories.database.control_plane.schemas_completion_requests import (
    CompletionRequestRow,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.ports.completion_requests import (
    STALE_CLAIM_SECONDS,
    CompletionClaim,
    CompletionClaimStatus,
)


@dataclass(slots=True)
class SqlCompletionRequestStore:
    """A unique owner/key primary key arbitrates concurrent retries across workers."""

    sessions: SessionFactory

    async def claim_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> CompletionClaim:
        now = utc_now()
        try:
            async with self.sessions.begin() as session:
                session.add(
                    CompletionRequestRow(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        key=key,
                        request_hash=request_hash,
                        status="in_progress",
                        created_at=now,
                        updated_at=now,
                    )
                )
            return CompletionClaim("claimed")
        except IntegrityError:
            # Inspect after rolling back the insert. Only the expected unique
            # owner/key collision becomes a replay; other integrity errors
            # retain their original failure.
            async with self.sessions() as session:
                row = await session.get(CompletionRequestRow, (tenant_id, user_id, key))
                if row is None:
                    raise
                if row.request_hash != request_hash:
                    return CompletionClaim("conflict")
                if row.status == "in_progress":
                    return await self._claim_if_abandoned(
                        tenant_id=tenant_id, user_id=user_id, key=key, request_hash=request_hash
                    )
                return CompletionClaim(cast("CompletionClaimStatus", row.status), row.response_json)

    async def _claim_if_abandoned(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> CompletionClaim:
        """Take over an ``in_progress`` claim whose worker never came back.

        The conditional update is the arbitration: concurrent retries race on
        the same predicate and exactly one observes a changed row, so a live
        claim is never stolen from a worker that is merely slow.
        """

        cutoff = utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS)
        async with self.sessions.begin() as session:
            taken = await session.scalar(
                sa.update(CompletionRequestRow)
                .where(
                    CompletionRequestRow.tenant_id == tenant_id,
                    CompletionRequestRow.user_id == user_id,
                    CompletionRequestRow.key == key,
                    CompletionRequestRow.request_hash == request_hash,
                    CompletionRequestRow.status == "in_progress",
                    CompletionRequestRow.updated_at < cutoff,
                )
                .values(updated_at=utc_now())
                .returning(CompletionRequestRow.key)
            )
        return CompletionClaim("claimed" if taken is not None else "in_progress")

    async def finish_completion(  # noqa: PLR0913 - mirrors the scoped idempotency port
        self,
        *,
        tenant_id: str,
        user_id: str,
        key: str,
        request_hash: str,
        response_json: str | None,
        session_id: str | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            if session_id is not None:
                # Serialize publication against erasure by locking the same
                # session row. A deleted conversation cannot regain replay text.
                conversation = await session.scalar(
                    sa.update(ConversationSessionRow)
                    .where(
                        ConversationSessionRow.tenant_id == tenant_id,
                        ConversationSessionRow.user_id == user_id,
                        ConversationSessionRow.session_id == session_id,
                    )
                    .values(next_message_seq=ConversationSessionRow.next_message_seq)
                    .returning(ConversationSessionRow.session_id)
                )
                if conversation is None:
                    response_json = None
            await session.execute(
                sa.update(CompletionRequestRow)
                .where(
                    CompletionRequestRow.tenant_id == tenant_id,
                    CompletionRequestRow.user_id == user_id,
                    CompletionRequestRow.key == key,
                    CompletionRequestRow.request_hash == request_hash,
                    CompletionRequestRow.status == "in_progress",
                )
                .values(
                    status="completed" if response_json is not None else "failed",
                    response_json=response_json,
                    session_id=session_id,
                    updated_at=utc_now(),
                )
            )

    async def release_completion(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> None:
        """Delete an active claim so an unspent key can be used again."""

        async with self.sessions.begin() as session:
            await session.execute(
                sa.delete(CompletionRequestRow).where(
                    CompletionRequestRow.tenant_id == tenant_id,
                    CompletionRequestRow.user_id == user_id,
                    CompletionRequestRow.key == key,
                    CompletionRequestRow.request_hash == request_hash,
                    CompletionRequestRow.status == "in_progress",
                )
            )
