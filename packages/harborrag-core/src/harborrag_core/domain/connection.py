"""Connection aggregate: one configured data source inside a project.

Handles:
- the connection identity (tenant, project, ``connection_id``) and its ``ref``
- the immutable ``source_key`` ingestion keys documents and scopes by
- the optimistic-concurrency ``version`` behind the HTTP ``ETag``
- ``ConnectionBuilder``, the one place defaults and actor stamps are applied

Does NOT validate credentials or config against a source type's schema; the
runtime source-type registry owns that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Self
from uuid import uuid4

from harborrag_core.base import utc_now

from .validation import require_id, require_tenant_id, validate_secret_free_config

ConnectionStatus = Literal["active", "needs_reauth", "error", "deleting"]
ConnectionPutOutcome = Literal["created", "overwritten"]

ATTENTION_STATUSES: frozenset[str] = frozenset({"needs_reauth", "error"})
CONNECTION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
CONNECTION_ID_MAX_LENGTH = 63
SYSTEM_ACTOR = "system"

_SLUG_INVALID = re.compile(r"[^a-z0-9_-]+")
_SLUG_EDGES = re.compile(r"^[^a-z0-9]+|[-_]+$")


def require_connection_id(value: str) -> None:
    """Reject a ``connection_id`` outside ``^[a-z0-9][a-z0-9_-]{0,62}$``.

    Args:
        value: Candidate connection id.

    Raises:
        ValueError: The id does not match the pattern.
    """
    if not CONNECTION_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "connection_id must match ^[a-z0-9][a-z0-9_-]{0,62}$: lowercase letters, "
            "digits, '-' and '_', starting with a letter or digit"
        )


def slugify_connection_id(name: str, *, fallback: str = "connection") -> str:
    """Derive a valid ``connection_id`` from a display name.

    Args:
        name: Free-form display name.
        fallback: Slug used when ``name`` has no usable characters.

    Returns:
        A slug matching ``CONNECTION_ID_PATTERN``.
    """
    slug = _SLUG_INVALID.sub("-", name.strip().lower())
    slug = _SLUG_EDGES.sub("", slug)[:CONNECTION_ID_MAX_LENGTH].rstrip("-_")
    return slug or fallback


def unique_connection_id(base: str, taken: set[str]) -> str:
    """Suffix ``base`` with ``-2``, ``-3``… until it is free in ``taken``.

    Args:
        base: A slug already matching ``CONNECTION_ID_PATTERN``.
        taken: Connection ids already used in the project.

    Returns:
        The first free candidate, still within the pattern's length limit.
    """
    if base not in taken:
        return base
    counter = 2
    while True:
        suffix = f"-{counter}"
        candidate = f"{base[: CONNECTION_ID_MAX_LENGTH - len(suffix)]}{suffix}"
        if candidate not in taken:
            return candidate
        counter += 1


def default_source_key(project_id: str, connection_id: str) -> str:
    """Return the ``source_key`` a newly created connection receives."""
    return f"{project_id}/{connection_id}"


@dataclass(slots=True)
class Connection:
    """A configured data source attached to a project.

    ``credentials`` holds where and how to connect, ``config`` what to sync and
    ``tuning`` per-connection overrides of connector defaults. Secret fields in
    ``credentials`` are stored as secret references only; ``secret_refs`` lists
    every reference the row owns.
    """

    id: str
    tenant_id: str
    project_id: str
    connection_id: str
    source_key: str
    source_type: str
    name: str
    credentials: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    tuning: dict[str, Any] = field(default_factory=dict)
    secret_refs: list[str] = field(default_factory=list)
    status: ConnectionStatus = "active"
    status_detail: dict[str, Any] | None = None
    schedule: str | None = None
    version: int = 1
    created_by: str = SYSTEM_ACTOR
    updated_by: str = SYSTEM_ACTOR
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        require_id(self.id, label="Connection")
        require_tenant_id(self.tenant_id)
        require_id(self.project_id, label="Project")
        require_connection_id(self.connection_id)
        require_id(self.source_key, label="Source key")
        require_id(self.source_type, label="Source type")
        if self.version < 1:
            raise ValueError("connection version starts at 1")
        validate_secret_free_config(self.credentials)
        validate_secret_free_config(self.config)
        validate_secret_free_config(self.tuning)

    @property
    def ref(self) -> str:
        """``<project_id>/<connection_id>``, the public handle in filters and documents."""
        return f"{self.project_id}/{self.connection_id}"

    @property
    def needs_attention(self) -> bool:
        """True when the status asks an operator to act."""
        return self.status in ATTENTION_STATUSES


class ConnectionBuilder:
    """Assemble a ``Connection`` with defaults, identity and actor stamps applied once.

    A builder started with ``for_new`` mints the internal id and the default
    ``source_key``; one started with ``from_existing`` keeps the stored identity
    and creation stamps so an overwrite can only change mutable fields.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        project_id: str,
        connection_id: str,
        existing: Connection | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._connection_id = connection_id
        self._existing = existing
        self._source_type = existing.source_type if existing else ""
        self._name: str | None = existing.name if existing else None
        self._source_key: str | None = existing.source_key if existing else None
        self._credentials: dict[str, Any] = dict(existing.credentials) if existing else {}
        self._config: dict[str, Any] = dict(existing.config) if existing else {}
        self._tuning: dict[str, Any] = dict(existing.tuning) if existing else {}
        self._secret_refs: list[str] = list(existing.secret_refs) if existing else []
        self._status: ConnectionStatus = existing.status if existing else "active"
        self._status_detail = existing.status_detail if existing else None
        self._schedule = existing.schedule if existing else None
        self._actor = SYSTEM_ACTOR

    @classmethod
    def for_new(cls, *, tenant_id: str, project_id: str, connection_id: str) -> Self:
        """Start a builder for a connection that does not exist yet."""
        return cls(tenant_id=tenant_id, project_id=project_id, connection_id=connection_id)

    @classmethod
    def from_existing(cls, connection: Connection) -> Self:
        """Start a builder that overwrites ``connection`` and keeps its identity."""
        return cls(
            tenant_id=connection.tenant_id,
            project_id=connection.project_id,
            connection_id=connection.connection_id,
            existing=connection,
        )

    def source_type(self, value: str) -> Self:
        """Set the canonical source type."""
        self._source_type = value
        return self

    def name(self, value: str | None) -> Self:
        """Set the display name; ``None`` falls back to the ``connection_id``."""
        self._name = value
        return self

    def source_key(self, value: str) -> Self:
        """Pin the ``source_key`` of a new connection, e.g. an imported YAML key.

        Raises:
            ValueError: The builder overwrites an existing connection.
        """
        if self._existing is not None and value != self._existing.source_key:
            raise ValueError("source_key is immutable once a connection exists")
        self._source_key = value
        return self

    def credentials(self, value: dict[str, Any]) -> Self:
        """Replace the stored credentials (secret fields already as references)."""
        self._credentials = dict(value)
        return self

    def config(self, value: dict[str, Any]) -> Self:
        """Replace what to sync, as a whole."""
        self._config = dict(value)
        return self

    def tuning(self, value: dict[str, Any]) -> Self:
        """Replace the per-connection tuning overrides."""
        self._tuning = dict(value)
        return self

    def secret_refs(self, value: list[str]) -> Self:
        """Replace the list of secret references the row owns."""
        self._secret_refs = list(value)
        return self

    def status(self, value: ConnectionStatus, detail: dict[str, Any] | None = None) -> Self:
        """Set the status and its optional machine-readable detail."""
        self._status = value
        self._status_detail = detail
        return self

    def schedule(self, value: str | None) -> Self:
        """Set the legacy cron schedule kept for ``/api/v1/sources``."""
        self._schedule = value
        return self

    def actor(self, subject: str) -> Self:
        """Record who is writing; it becomes ``updated_by`` and, on create, ``created_by``."""
        self._actor = subject or SYSTEM_ACTOR
        return self

    def build(self, *, now: datetime | None = None) -> Connection:
        """Return the assembled connection.

        Args:
            now: Timestamp for the write; defaults to the current UTC time.

        Returns:
            A validated ``Connection``. ``version`` is the stored one for an
            overwrite; the repository advances it when the write commits.

        Raises:
            ValueError: A domain invariant fails, e.g. an invalid ``connection_id``.
        """
        stamp = now or utc_now()
        existing = self._existing
        return Connection(
            id=existing.id if existing else f"src_{uuid4().hex}",
            tenant_id=self._tenant_id,
            project_id=self._project_id,
            connection_id=self._connection_id,
            source_key=self._source_key
            or default_source_key(self._project_id, self._connection_id),
            source_type=self._source_type,
            name=self._name or self._connection_id,
            credentials=self._credentials,
            config=self._config,
            tuning=self._tuning,
            secret_refs=self._secret_refs,
            status=self._status,
            status_detail=self._status_detail,
            schedule=self._schedule,
            version=existing.version if existing else 1,
            created_by=existing.created_by if existing else self._actor,
            updated_by=self._actor,
            created_at=existing.created_at if existing else stamp,
            updated_at=stamp,
        )
