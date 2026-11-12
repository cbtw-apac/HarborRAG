"""Port that every application service implements for the HTTP and CLI transports.

`BaseAppService` holds the contract; optional capabilities default to a no-op so a
concrete service overrides only what it supports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Mapping

from harborrag_memory.access import MemoryAccess

from .chat.options import ChatExecutionOptions
from .schemas import AppResponse


class BaseAppService(ABC):
    """Application facade shared by the HTTP and CLI transports."""

    @abstractmethod
    def health(self) -> AppResponse:
        """Return the service's health without contacting external systems."""

        raise NotImplementedError

    async def runtime_health(self) -> AppResponse:
        """Return live runtime health where the selected service supports it."""

        return self.health()

    async def tenants_with_data(self) -> list[str]:
        """List the tenants holding a project or a connection where the service supports it."""

        return []

    async def recover_pending_submissions(self, *, limit: int = 100) -> int:
        """Recover durable workflow starts where the concrete service supports it."""

        del limit
        return 0

    async def recover_pending_control_plane_effects(self, *, limit: int = 100) -> int:
        """Recover durable control-plane effects where the service supports it."""

        del limit
        return 0

    async def sync_ingestion_progress(self) -> int:
        """One progress-bridge poll tick where the concrete service supports it."""

        return 0

    async def start_memory_extraction(self) -> None:
        """Start background memory extraction where the service supports it."""

        return None

    async def drain_memory_extraction(self) -> None:
        """Finish queued memory extraction where the service supports it."""

        return None

    async def create_chat_session(  # noqa: PLR0913 - one component of the new session per argument
        self,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
        title: str | None = None,
        project_id: str | None = None,
    ) -> AppResponse:
        """Create a chat session, scoped to ``project_id`` for its whole life."""

        raise NotImplementedError

    async def list_conversations(
        self,
        access: MemoryAccess,
        *,
        cursor: str | None = None,
        limit: int = 20,
        title_query: str | None = None,
    ) -> AppResponse:
        """One page of the caller's own sessions, newest activity first."""

        raise NotImplementedError

    async def conversation_messages(
        self,
        access: MemoryAccess,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> AppResponse:
        """One page of one session's history, newest first."""

        raise NotImplementedError

    async def rename_conversation(self, access: MemoryAccess, *, title: str) -> AppResponse:
        """Retitle one of the caller's conversations; blank clears the title."""

        raise NotImplementedError

    async def delete_conversation(self, access: MemoryAccess) -> AppResponse:
        """Erase one conversation, its memories, and their vector points."""

        raise NotImplementedError

    async def chat_session_exists(
        self,
        session_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        user_id: str | None = None,
    ) -> bool:
        """Report whether the caller owns the chat session ``session_id``."""

        raise NotImplementedError

    async def chat_completion(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AppResponse:
        """Generate one retrieval-grounded chat completion."""

        raise NotImplementedError

    def chat_stream(
        self,
        query: str,
        *,
        tenant_id: str,
        principal_id: str,
        options: ChatExecutionOptions,
    ) -> AsyncGenerator[dict[str, object], None]:
        """Stream one retrieval-grounded chat completion as ``{"kind": ...}`` events.

        An ``AsyncGenerator`` rather than a bare iterator: the transport closes
        the stream when its client goes away, and the implementation persists
        the text it already delivered while handling that close.
        """

        raise NotImplementedError

    async def run_ingestion(  # noqa: PLR0913 - the inline run options
        self,
        *,
        tenant_id: str,
        connector_name: str,
        run_id: str,
        connection_id: str | None = None,
        source_scope_id: str | None = None,
        path: str | None = None,
        pattern: str | None = None,
        recursive: bool = True,
        updated_after: str | None = None,
        max_artifacts: int | None = None,
        include_attachments: bool = True,
        filters: Mapping[str, object] | None = None,
        force_reprocess: bool = False,
    ) -> AppResponse:
        """Execute one ingestion inline (direct mode) and return its final result."""

        raise NotImplementedError

    async def get_task(self, task_id: str) -> dict[str, object]:
        """Public task document for ``task_id``; raises IngestionNotFoundError when absent."""

        raise NotImplementedError
