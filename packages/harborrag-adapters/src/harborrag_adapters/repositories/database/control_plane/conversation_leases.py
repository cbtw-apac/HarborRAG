"""Atomic session sequence allocation and renewable turn ownership."""

from __future__ import annotations

import math
from datetime import UTC, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from harborrag_adapters.repositories.database.control_plane.mapping import utc_now
from harborrag_adapters.repositories.database.control_plane.schemas_agent_memory import (
    ConversationSessionRow,
)
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ports.conversation import ConversationIdentity


def session_filter(identity: ConversationIdentity) -> tuple[ColumnElement[bool], ...]:
    """Require tenant and human ownership for every session mutation."""

    return (
        ConversationSessionRow.tenant_id == identity.tenant_id,
        ConversationSessionRow.user_id == identity.user_id,
        ConversationSessionRow.session_id == identity.session_id,
    )


async def reserve_sequence(
    session: AsyncSession, identity: ConversationIdentity, count: int, token: str | None = None
) -> int:
    """Reserve a contiguous range while locking the session row transactionally."""

    end = await session.scalar(
        sa.update(ConversationSessionRow)
        .where(*session_filter(identity), writable_lease(token))
        .values(
            next_message_seq=ConversationSessionRow.next_message_seq + count,
            updated_at=utc_now(),
        )
        .returning(ConversationSessionRow.next_message_seq)
        .execution_options(synchronize_session=False)
    )
    if end is None:
        await reject_busy_session(session, identity)
        raise ValueError("conversation session does not exist")
    return int(end) - count


def writable_lease(token: str | None) -> ColumnElement[bool]:
    """Require a live matching fencing token, or an unclaimed session for unleased writes."""

    now = sa.func.current_timestamp()
    if token is not None:
        return sa.and_(
            ConversationSessionRow.turn_lease_token == token,
            ConversationSessionRow.turn_lease_expires_at > now,
        )
    return sa.or_(
        ConversationSessionRow.turn_lease_token.is_(None),
        ConversationSessionRow.turn_lease_expires_at <= now,
    )


async def reject_busy_session(session: AsyncSession, identity: ConversationIdentity) -> None:
    """Distinguish a missing session from a failed lease guard without leaking ownership."""

    if await session.scalar(sa.select(sa.exists().where(*session_filter(identity)))):
        raise HarborConflictError("conversation has an active completion or its lease was lost")


async def lock_for_write(
    session: AsyncSession, identity: ConversationIdentity, token: str | None
) -> bool:
    """Lock the session before destructive mutations on SQLite and PostgreSQL."""

    found = await session.scalar(
        sa.update(ConversationSessionRow)
        .where(*session_filter(identity), writable_lease(token))
        .values(next_message_seq=ConversationSessionRow.next_message_seq)
        .returning(ConversationSessionRow.session_id)
        .execution_options(synchronize_session=False)
    )
    if found is None:
        await reject_busy_session(session, identity)
    return found is not None


async def claim_lease(
    session: AsyncSession,
    identity: ConversationIdentity,
    *,
    token: str,
    lease_seconds: float,
    renew: bool,
) -> bool:
    """Compare and swap a lease without holding a database transaction during inference."""

    if not token or not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("conversation lease requires a token and positive finite duration")
    # Lease time comes from the shared database, so skew between worker clocks
    # cannot revoke another worker's ownership. SQLite returns a naive UTC value.
    now = await session.scalar(sa.select(sa.func.current_timestamp()))
    assert now is not None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    available = (
        sa.and_(
            ConversationSessionRow.turn_lease_token == token,
            ConversationSessionRow.turn_lease_expires_at > now,
        )
        if renew
        else sa.or_(
            ConversationSessionRow.turn_lease_token.is_(None),
            ConversationSessionRow.turn_lease_expires_at <= now,
        )
    )
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            sa.update(ConversationSessionRow)
            .where(*session_filter(identity), available)
            .values(
                turn_lease_token=token,
                turn_lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
        ),
    )
    return result.rowcount > 0


async def release_lease(
    session: AsyncSession, identity: ConversationIdentity, *, token: str
) -> None:
    """Release only this lease; an expired owner cannot release its successor."""

    await session.execute(
        sa.update(ConversationSessionRow)
        .where(*session_filter(identity), ConversationSessionRow.turn_lease_token == token)
        .values(turn_lease_token=None, turn_lease_expires_at=None)
    )
