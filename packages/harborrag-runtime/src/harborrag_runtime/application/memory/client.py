"""Conversation-directory and memory-administration use cases.

Both surfaces answer "what does this deployment remember about me, and make it
stop": the conversation directory reads and retitles the caller's own
conversations, and administration reads and erases what was extracted from
them. They forward from one mixin because they share the same caller-derived
access key and the same erasure path -- deleting a conversation is the memory
erasure, not a second one that could drift from it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..schemas import AppResponse
from .extraction import drain_extraction_queue
from .presenters import memory_data

if TYPE_CHECKING:
    from harborrag_core.ports.memory import MemoryScope
    from harborrag_memory.access import MemoryAccess
    from harborrag_memory.administration import MemoryAdministrationService

    from .directory import ConversationDirectoryService
    from .extraction import MemoryExtractionQueue


class MemoryAdminClientMixin:
    """Forward conversation, memory, erasure, and extraction use cases."""

    _memory_admin: MemoryAdministrationService
    _conversation_directory: ConversationDirectoryService
    _extraction: MemoryExtractionQueue | None

    @property
    def has_memory_extraction(self) -> bool:
        """Report whether a background extraction queue is configured."""
        return self._extraction is not None

    async def start_memory_extraction(self) -> None:
        """Start the background extraction workers (API lifespan only)."""
        if self._extraction is not None:
            await self._extraction.start()

    async def drain_memory_extraction(self) -> None:
        """Finish queued extraction work before the process exits."""
        await drain_extraction_queue(self._extraction)

    async def list_memories(
        self,
        access: MemoryAccess,
        *,
        scope: MemoryScope | None = None,
        limit: int = 20,
    ) -> AppResponse:
        """List the caller's long-term memories, optionally of one scope, as an ``AppResponse``."""
        memories = await self._memory_admin.list_memories(
            access.owner(),
            scopes=() if scope is None else (scope,),
            limit=limit,
        )
        return AppResponse(True, {"memories": [memory_data(memory) for memory in memories]})

    async def delete_memory(
        self,
        access: MemoryAccess,
        memory_id: str,
        *,
        shared_scopes: frozenset[MemoryScope] = frozenset(),
    ) -> AppResponse:
        """Delete one long-term memory the caller owns, or a shared scope in ``shared_scopes``."""
        await self._memory_admin.delete_memory(
            access.owner(), memory_id, shared_scopes=shared_scopes
        )
        return AppResponse(True, {"memory_id": memory_id, "deleted": True})

    async def erase_memory_session(self, access: MemoryAccess) -> AppResponse:
        """Erase one session's memories and report what was removed."""
        report = await self._memory_admin.erase_session(
            access.owner(),
            actor=access.principal_id,
        )
        return AppResponse(True, {"session_id": access.session_id, **report.as_dict()})

    async def erase_memory_user(self, access: MemoryAccess, user_id: str) -> AppResponse:
        """Erase every memory of ``user_id`` and report what was removed."""
        report = await self._memory_admin.erase_user(
            access.target(user_id),
            actor=access.principal_id,
        )
        return AppResponse(True, {"user_id": user_id, **report.as_dict()})

    async def list_conversations(
        self,
        access: MemoryAccess,
        *,
        cursor: str | None = None,
        limit: int = 20,
        title_query: str | None = None,
    ) -> AppResponse:
        """Return one page of the caller's own sessions, newest activity first."""
        return AppResponse(
            True,
            await self._conversation_directory.list_conversations(
                access,
                cursor=cursor,
                limit=limit,
                title_query=title_query,
            ),
        )

    async def conversation_messages(
        self,
        access: MemoryAccess,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> AppResponse:
        """Return one page of one session's history, newest first."""
        return AppResponse(
            True,
            await self._conversation_directory.messages(access, cursor=cursor, limit=limit),
        )

    async def rename_conversation(self, access: MemoryAccess, *, title: str) -> AppResponse:
        """Retitle one of the caller's conversations; blank clears the title."""
        return AppResponse(True, await self._conversation_directory.rename(access, title=title))

    async def delete_conversation(self, access: MemoryAccess) -> AppResponse:
        """Erase one conversation, its memories and their vector points."""
        return AppResponse(True, await self._conversation_directory.delete(access))
