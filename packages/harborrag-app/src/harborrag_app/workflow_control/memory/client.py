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
    from harborrag_core.ports.conversation import ConversationKind
    from harborrag_core.ports.memory import MemoryScope

    from .access import MemoryAccess
    from .administration import MemoryAdministrationService
    from .directory import ConversationDirectoryService
    from .extraction import MemoryExtractionQueue


class MemoryAdminClientMixin:
    """Forward conversation, memory, erasure, and extraction use cases."""

    _memory_admin: MemoryAdministrationService
    _conversation_directory: ConversationDirectoryService
    _extraction: MemoryExtractionQueue | None

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
        memories = await self._memory_admin.list_memories(
            access.owner(),
            scopes=() if scope is None else (scope,),
            limit=limit,
        )
        return AppResponse(True, {"memories": [memory_data(memory) for memory in memories]})

    async def delete_memory(self, access: MemoryAccess, memory_id: str) -> AppResponse:
        await self._memory_admin.delete_memory(access.owner(), memory_id)
        return AppResponse(True, {"memory_id": memory_id, "deleted": True})

    async def erase_memory_session(self, access: MemoryAccess) -> AppResponse:
        report = await self._memory_admin.erase_session(
            access.owner(),
            actor=access.principal_id,
        )
        return AppResponse(True, {"session_id": access.session_id, **report.as_dict()})

    async def erase_memory_user(self, access: MemoryAccess, user_id: str) -> AppResponse:
        report = await self._memory_admin.erase_user(
            access.target(user_id),
            actor=access.principal_id,
        )
        return AppResponse(True, {"user_id": user_id, **report.as_dict()})

    async def list_conversations(
        self,
        access: MemoryAccess,
        *,
        kind: ConversationKind | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> AppResponse:
        return AppResponse(
            True,
            await self._conversation_directory.list_conversations(
                access,
                kind=kind,
                cursor=cursor,
                limit=limit,
            ),
        )

    async def conversation_messages(
        self,
        access: MemoryAccess,
        *,
        after: str | None = None,
        limit: int = 50,
    ) -> AppResponse:
        return AppResponse(
            True,
            await self._conversation_directory.messages(access, after=after, limit=limit),
        )

    async def rename_conversation(self, access: MemoryAccess, *, title: str) -> AppResponse:
        return AppResponse(True, await self._conversation_directory.rename(access, title=title))

    async def delete_conversation(self, access: MemoryAccess) -> AppResponse:
        return AppResponse(True, await self._conversation_directory.delete(access))


__all__ = ["MemoryAdminClientMixin"]
