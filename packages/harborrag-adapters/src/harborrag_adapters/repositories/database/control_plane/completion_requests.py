"""Database-backed atomic completion claims and terminal response replay.

Each new claim also deletes a bounded batch of the tenant's requests past key
retention, so stored answers do not outlive ``KEY_RETENTION_SECONDS`` for long.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql.elements import ColumnElement

from harborrag_adapters.repositories.database.control_plane.mapping import utc_now
from harborrag_adapters.repositories.database.control_plane.schemas_completion_requests import (
    CompletionRequestRow,
)
from harborrag_adapters.repositories.database.control_plane.schemas_conversation_memory import (
    ConversationSessionRow,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.ports.completion_requests import (
    KEY_RETENTION_SECONDS,
    STALE_CLAIM_SECONDS,
    CompletionClaim,
    CompletionClaimStatus,
)

logger = logging.getLogger("harborrag.adapters.control_plane.completion_requests")

_PURGE_BATCH = 100
"""Most expired requests one new claim deletes; a backlog drains one batch per claim."""


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
        except IntegrityError:
            # Inspect after rolling back the insert. Only the expected unique
            # owner/key collision becomes a replay; other integrity errors
            # retain their original failure.
            async with self.sessions() as session:
                row = await session.get(CompletionRequestRow, (tenant_id, user_id, key))
                if row is None:
                    raise
                if await self._reclaim_expired(
                    tenant_id=tenant_id, user_id=user_id, key=key, request_hash=request_hash
                ):
                    return CompletionClaim("claimed")
                await session.refresh(row)
                if row.request_hash != request_hash:
                    return CompletionClaim("conflict")
                if row.status == "in_progress":
                    return await self._claim_if_abandoned(
                        tenant_id=tenant_id, user_id=user_id, key=key, request_hash=request_hash
                    )
                return CompletionClaim(cast("CompletionClaimStatus", row.status), row.response_json)
        await self._purge_expired(tenant_id)
        return CompletionClaim("claimed")

    async def _purge_expired(self, tenant_id: str) -> None:
        """Delete up to ``_PURGE_BATCH`` of the tenant's requests past key retention.

        The rows are the ones ``_reclaim_expired`` would start over, so a live
        ``in_progress`` claim is never deleted. Rows another purge holds are skipped.
        A failure is logged and never fails the claim that triggered it.
        """

        expired = _expired(tenant_id)
        batch = (
            sa.select(
                CompletionRequestRow.tenant_id,
                CompletionRequestRow.user_id,
                CompletionRequestRow.key,
            )
            .where(*expired)
            .limit(_PURGE_BATCH)
            .with_for_update(skip_locked=True)
        )
        owner_key = sa.tuple_(
            CompletionRequestRow.tenant_id, CompletionRequestRow.user_id, CompletionRequestRow.key
        )
        try:
            async with self.sessions.begin() as session:
                await session.execute(
                    sa.delete(CompletionRequestRow).where(*expired, owner_key.in_(batch))
                )
        except SQLAlchemyError as exc:
            logger.warning(
                "Expired completion requests were not purged for tenant=%s error=%s",
                tenant_id,
                type(exc).__name__,
            )

    async def erase_completions(self, *, tenant_id: str, user_id: str) -> int:
        """Delete every request of one end user, stored answers included."""

        async with self.sessions.begin() as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    sa.delete(CompletionRequestRow).where(
                        CompletionRequestRow.tenant_id == tenant_id,
                        CompletionRequestRow.user_id == user_id,
                    )
                ),
            )
        return result.rowcount

    async def _reclaim_expired(
        self, *, tenant_id: str, user_id: str, key: str, request_hash: str
    ) -> bool:
        """Start the key over for this request once its claim is past retention.

        A live ``in_progress`` claim is never expired, however old: only one its
        worker abandoned is. The conditional update arbitrates concurrent retries.
        """

        now = utc_now()
        async with self.sessions.begin() as session:
            taken = await session.scalar(
                sa.update(CompletionRequestRow)
                .where(
                    *_expired(tenant_id),
                    CompletionRequestRow.user_id == user_id,
                    CompletionRequestRow.key == key,
                )
                .values(
                    request_hash=request_hash,
                    status="in_progress",
                    response_json=None,
                    session_id=None,
                    created_at=now,
                    updated_at=now,
                )
                .returning(CompletionRequestRow.key)
            )
        return taken is not None

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


def _expired(tenant_id: str) -> tuple[ColumnElement[bool], ...]:
    """Match the tenant's requests past retention whose claim is not live."""

    now = utc_now()
    return (
        CompletionRequestRow.tenant_id == tenant_id,
        CompletionRequestRow.created_at < now - timedelta(seconds=KEY_RETENTION_SECONDS),
        sa.or_(
            CompletionRequestRow.status != "in_progress",
            CompletionRequestRow.updated_at < now - timedelta(seconds=STALE_CLAIM_SECONDS),
        ),
    )
