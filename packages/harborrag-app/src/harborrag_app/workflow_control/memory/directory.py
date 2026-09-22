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
    normalize_conversation_title,
)

from .presenters import conversation_message_data, conversation_summary_data

if TYPE_CHECKING:
    from harborrag_core.ports.conversation import (
        ConversationHistoryRepository,
        ConversationKind,
    )

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
        kind: ConversationKind | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        """One page of the caller's conversations, newest activity first."""

        try:
            page = await self._repository.list_conversations(
                tenant_id=access.tenant_id,
                user_id=access.user_id,
                kind=kind,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as exc:
            raise HarborValidationError(str(exc)) from exc
        return {
            "conversations": [conversation_summary_data(row) for row in page.conversations],
            "next_cursor": page.next_cursor,
        }

    async def messages(
        self,
        access: MemoryAccess,
        *,
        after: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        """One page of a conversation's messages, oldest first.

        One extra message is read so the last page never advertises a cursor
        that would only lead to an empty page.
        """

        identity = self._identity(access)
        if not await self._repository.exists(identity):
            raise HarborNotFoundError(_NOT_FOUND)
        try:
            found = await self._repository.messages_after(
                identity,
                after_message_id=after,
                limit=limit + 1,
            )
        except ValueError as exc:
            raise HarborValidationError(str(exc)) from exc
        page = found[:limit]
        return {
            "messages": [conversation_message_data(message) for message in page],
            "next_cursor": page[-1].message_id if len(found) > limit else None,
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
