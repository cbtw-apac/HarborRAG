"""Listing, reading, renaming, and deleting the conversations one human owns.

Every operation here is keyed by the authenticated caller's own
``MemoryAccess``: the transport builds it from verified claims, never from
request fields, so "my conversations" is enforced by the storage predicate
rather than by a route remembering to filter. ``user_id`` is what owns a
conversation, so one service credential fronting several people cannot pool
their histories into a single listing.

Deletion is deliberately not implemented here. Erasing a conversation must
also remove the memories and vector points that conversation produced, and
``MemoryAdministrationService.erase_session`` already does exactly that, so
this service delegates to it instead of growing a second erasure path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import HarborNotFoundError, HarborValidationError
from harborrag_core.ports.conversation import (
    ConversationIdentity,
    decode_conversation_cursor,
    encode_conversation_cursor,
    normalize_conversation_title,
)

from .presenters import conversation_message_data, conversation_summary_data

if TYPE_CHECKING:
    from harborrag_core.ports.conversation import ConversationHistoryRepository

    from .access import MemoryAccess
    from .administration import MemoryAdministrationService

_NOT_FOUND = "Conversation session was not found"


class ConversationDirectoryService:
    """The caller's own conversation directory: list, read, rename, delete."""

    def __init__(
        self,
        repository: ConversationHistoryRepository,
        administration: MemoryAdministrationService,
    ) -> None:
        self._repository = repository
        self._administration = administration

    async def list_conversations(
        self,
        access: MemoryAccess,
        *,
        cursor: str | None = None,
        limit: int = 20,
        title_query: str | None = None,
    ) -> dict[str, object]:
        """One page of the caller's sessions, newest activity first.

        Args:
            access: The caller's own conversation key.
            cursor: ``next_cursor`` of the previous page.
            limit: Page size.
            title_query: Keep titles that contain this text, ignoring case.

        Returns:
            ``items`` of sessions and ``next_cursor``, ``None`` on the last page.

        Raises:
            HarborValidationError: The cursor is malformed or not the caller's.
        """

        try:
            page = await self._repository.list_conversations(
                tenant_id=access.tenant_id,
                user_id=access.user_id,
                cursor=cursor,
                limit=limit,
                title_query=(title_query or "").strip() or None,
            )
        except ValueError as exc:
            raise HarborValidationError(str(exc), {"field": "cursor"}) from exc
        return {
            "items": [conversation_summary_data(row) for row in page.conversations],
            "next_cursor": page.next_cursor,
        }

    async def messages(
        self,
        access: MemoryAccess,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        """One page of a session's history, newest first.

        One extra message is read so the last page never advertises a cursor
        that would only lead to an empty page.

        Args:
            access: The caller's own conversation key, with its session.
            cursor: ``next_cursor`` of the previous page.
            limit: Page size.

        Returns:
            ``items`` of messages and ``next_cursor``, ``None`` on the last page.

        Raises:
            HarborNotFoundError: The session does not exist or is not the caller's.
            HarborValidationError: The cursor is malformed or not from this session.
        """

        identity = self._identity(access)
        if not await self._repository.exists(identity):
            raise HarborNotFoundError(_NOT_FOUND)
        try:
            before = None if cursor is None else decode_conversation_cursor(cursor)
            found = await self._repository.history_page(
                identity, before_message_id=before, limit=limit + 1
            )
        except ValueError as exc:
            raise HarborValidationError(str(exc), {"field": "cursor"}) from exc
        page = found[:limit]
        has_more = len(found) > limit
        return {
            "items": [
                conversation_message_data(message, session_id=identity.session_id)
                for message in page
            ],
            "next_cursor": encode_conversation_cursor(page[-1].message_id) if has_more else None,
        }

    async def rename(self, access: MemoryAccess, *, title: str) -> dict[str, object]:
        """Retitle the caller's conversation; blank clears the title."""

        renamed = await self._repository.rename_conversation(
            self._identity(access),
            title=title,
        )
        if not renamed:
            raise HarborNotFoundError(_NOT_FOUND)
        return {
            "session_id": access.session_id,
            "title": normalize_conversation_title(title),
        }

    async def delete(self, access: MemoryAccess) -> dict[str, object]:
        """Erase the conversation, its memories, and their vector points."""

        report = await self._administration.erase_session(
            access.owner(),
            actor=access.principal_id,
        )
        return {"session_id": access.session_id, **report.as_dict()}

    @staticmethod
    def _identity(access: MemoryAccess) -> ConversationIdentity:
        """The conversation isolation key; a missing session is a 404, not a 500."""

        if access.session_id is None:
            raise HarborNotFoundError(_NOT_FOUND)
        return ConversationIdentity(
            access.tenant_id,
            access.principal_id,
            access.session_id,
            access.user_id,
        )


__all__ = ["ConversationDirectoryService"]
