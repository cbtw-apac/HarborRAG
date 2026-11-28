"""The application operations port: one method per ``/v1`` operationId.

HTTP routes and CLI commands only parse input, call one of these methods and serialize
what it returns; each method checks its own role and tenant through ``Scope``. Nothing here
imports a web framework.

Start from: Operations.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from harborrag_app.operations.etags import Preconditions
from harborrag_app.operations.principal import Principal
from harborrag_app.operations.results import Accepted, Saved, Tagged
from harborrag_app.operations.schemas.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatSessionCreateRequest,
    MessageList,
    Session,
    SessionList,
)
from harborrag_app.operations.schemas.connections import (
    ConnectionChipPage,
    ConnectionOut,
    ConnectionPage,
    ConnectionTestOut,
    ConnectionTestRequest,
    ConnectionWriteRequest,
    IngestionOut,
    IngestionStartRequest,
    JobAcceptedOut,
    ProjectOut,
    ProjectPage,
    ProjectWriteRequest,
    SourceTypePage,
)
from harborrag_app.operations.schemas.ingestions import (
    FailurePage,
    FailureQuery,
    FailureRetryAccepted,
    FailureRetryRequest,
    IngestionDocumentPage,
    IngestionDocumentQuery,
    IngestionListQuery,
    IngestionPage,
    RetryFailuresRequest,
)
from harborrag_app.operations.schemas.me import MeOut
from harborrag_app.operations.schemas.schedules import (
    ScheduleListQuery,
    ScheduleOut,
    SchedulePage,
    SchedulePauseRequest,
    ScheduleWriteRequest,
)
from harborrag_app.operations.schemas.settings import SettingsOut, SettingsPatchOut
from harborrag_app.operations.scope import ConnectionRef, Scope
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.connection import ConnectionStatus
from harborrag_runtime.application.control_plane.connection_models import WriteOrigin


@dataclass(frozen=True, slots=True)
class ChatCompletion:
    """One chat answer, whole or as a stream.

    Args:
        body: The completion for ``stream=false``.
        events: The server-sent event frames for ``stream=true``.
        replayed: Whether the answer was replayed for a repeated ``Idempotency-Key``.
    """

    body: ChatCompletionResponse | None
    events: AsyncIterator[bytes] | None
    replayed: bool


class Operations(Protocol):
    """Every ``/v1`` operation, named after its route.

    Each method refuses a caller below the role ``OPERATIONS`` lists for it, and one that
    does not hold the scope's tenant, before it reads or writes anything.
    """

    async def me(self, principal: Principal, *, tenant_id: str | None) -> MeOut:
        """Describe the caller's credential; ``tenant_id`` is ``None`` when none was picked."""
        ...

    async def list_source_types(self, scope: Scope, *, category: str | None) -> SourceTypePage:
        """List the installed source types, optionally of one category."""
        ...

    async def list_connections(  # noqa: PLR0913 - the route's list filters plus paging
        self,
        scope: Scope,
        *,
        project_id: str | None,
        source_type: str | None,
        status: ConnectionStatus | None,
        scheduled: bool | None,
        limit: int,
        cursor: str | None,
    ) -> ConnectionPage:
        """List the tenant's connections, the ones that need attention first."""
        ...

    async def get_connection(self, scope: Scope, ref: ConnectionRef) -> Tagged[ConnectionOut]:
        """Read one connection with its ETag; secrets read back as their status."""
        ...

    async def put_connection(  # noqa: PLR0913 - body, preconditions and write origin
        self,
        scope: Scope,
        ref: ConnectionRef,
        body: ConnectionWriteRequest,
        *,
        if_match: str | None,
        if_none_match: str | None,
        origin: WriteOrigin = "api",
    ) -> Saved[ConnectionOut]:
        """Create or replace a connection; ``if_none_match="*"`` makes it create-only."""
        ...

    async def delete_connection(
        self, scope: Scope, ref: ConnectionRef, *, purge: bool
    ) -> JobAcceptedOut:
        """Start removing a connection in the background."""
        ...

    async def test_connection(
        self,
        scope: Scope,
        ref: ConnectionRef,
        body: ConnectionTestRequest | None,
        *,
        origin: WriteOrigin = "api",
    ) -> ConnectionTestOut:
        """Check form values, or the saved connection when ``body`` is ``None``; saves nothing."""
        ...

    async def list_projects(
        self, scope: Scope, *, q: str | None, limit: int, cursor: str | None
    ) -> ProjectPage:
        """List the tenant's projects, optionally filtered by name."""
        ...

    async def get_project(self, scope: Scope, project_id: str) -> Tagged[ProjectOut]:
        """Read one project with its ETag."""
        ...

    async def put_project(
        self,
        scope: Scope,
        project_id: str,
        body: ProjectWriteRequest,
        *,
        if_match: str | None,
        if_none_match: str | None,
    ) -> Saved[ProjectOut]:
        """Create or update a project by the id the caller chooses."""
        ...

    async def delete_project(
        self, scope: Scope, project_id: str, *, cascade: bool
    ) -> JobAcceptedOut | None:
        """Delete an empty project at once (``None``), or start a cascade in the background."""
        ...

    async def list_searchable_connections(
        self, scope: Scope, project_id: str
    ) -> ConnectionChipPage:
        """List the connections a reader can search in a project."""
        ...

    async def start_ingestion(
        self, scope: Scope, body: IngestionStartRequest
    ) -> Accepted[IngestionOut]:
        """Start an ingestion of one connection."""
        ...

    async def list_ingestions(self, scope: Scope, query: IngestionListQuery) -> IngestionPage:
        """List ingestion runs."""
        ...

    async def get_ingestion(self, scope: Scope, ingestion_id: str) -> IngestionOut:
        """Read one ingestion run."""
        ...

    async def list_ingestion_documents(
        self, scope: Scope, ingestion_id: str, query: IngestionDocumentQuery
    ) -> IngestionDocumentPage:
        """List the documents one ingestion run processed."""
        ...

    async def stream_ingestion(
        self, scope: Scope, ingestion_id: str, *, after_seq: int | None
    ) -> AsyncGenerator[HarborEvent, None] | None:
        """Follow one ingestion run's events after ``after_seq``."""
        ...

    async def pause_ingestion(self, scope: Scope, ingestion_id: str) -> IngestionOut:
        """Pause a running ingestion."""
        ...

    async def resume_ingestion(self, scope: Scope, ingestion_id: str) -> IngestionOut:
        """Resume a paused ingestion, or one whose pause has not been applied yet."""
        ...

    async def cancel_ingestion(self, scope: Scope, ingestion_id: str) -> IngestionOut:
        """Cancel an ingestion."""
        ...

    async def retry_ingestion_failures(
        self, scope: Scope, ingestion_id: str, body: RetryFailuresRequest | None
    ) -> Accepted[IngestionOut]:
        """Retry the failed items of one ingestion run."""
        ...

    async def list_failures(self, scope: Scope, query: FailureQuery) -> FailurePage:
        """List failed items across ingestion runs."""
        ...

    async def retry_failures(self, scope: Scope, body: FailureRetryRequest) -> FailureRetryAccepted:
        """Retry the chosen failed items."""
        ...

    async def list_schedules(self, scope: Scope, query: ScheduleListQuery) -> SchedulePage:
        """List the tenant's schedules, newest first, each with its latest slot."""
        ...

    async def get_schedule(self, scope: Scope, schedule_id: str) -> Tagged[ScheduleOut]:
        """Read one schedule with its last 20 slots and its ETag."""
        ...

    async def put_schedule(
        self,
        scope: Scope,
        schedule_id: str,
        body: ScheduleWriteRequest,
        *,
        preconditions: Preconditions,
    ) -> Saved[ScheduleOut]:
        """Create or overwrite a schedule; ``If-None-Match: *`` makes it create-only."""
        ...

    async def delete_schedule(self, scope: Scope, schedule_id: str) -> None:
        """Remove a schedule; a run it started keeps going."""
        ...

    async def pause_schedule(
        self, scope: Scope, schedule_id: str, body: SchedulePauseRequest | None
    ) -> Tagged[ScheduleOut]:
        """Pause a schedule, with its ETag; pausing a paused one returns it unchanged."""
        ...

    async def resume_schedule(self, scope: Scope, schedule_id: str) -> Tagged[ScheduleOut]:
        """Resume a schedule and clear its failure count, with its ETag."""
        ...

    async def trigger_schedule(self, scope: Scope, schedule_id: str) -> Accepted[IngestionOut]:
        """Start one run now with the schedule's input, even while it is paused."""
        ...

    async def read_settings(
        self, scope: Scope, *, level: str | None, effective: bool
    ) -> Tagged[SettingsOut]:
        """Read the settings of one level, or their effective values."""
        ...

    async def patch_settings(  # noqa: PLR0913 - body, level and the patch's flags
        self,
        scope: Scope,
        body: Mapping[str, object],
        *,
        level: str | None,
        dry_run: bool,
        reindex: bool,
        if_match: str | None,
    ) -> Tagged[SettingsPatchOut]:
        """Change settings of one level."""
        ...

    async def settings_schema(self, scope: Scope) -> dict[str, Any]:
        """Return the JSON Schema of the settings."""
        ...

    async def create_chat_completion(
        self,
        scope: Scope,
        body: ChatCompletionRequest,
        *,
        idempotency_key: str | None,
        trace_id: str,
    ) -> ChatCompletion:
        """Answer one question, inside a session or as a one-off."""
        ...

    async def create_chat_session(self, scope: Scope, body: ChatSessionCreateRequest) -> Session:
        """Start a chat session."""
        ...

    async def list_chat_sessions(
        self, scope: Scope, *, cursor: str | None, limit: int, q: str | None
    ) -> SessionList:
        """List the caller's chat sessions, optionally filtered by title."""
        ...

    async def list_conversation_messages(
        self, scope: Scope, session_id: str, *, cursor: str | None, limit: int
    ) -> MessageList:
        """List the messages of one of the caller's sessions."""
        ...
