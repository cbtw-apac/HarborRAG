"""What a chat turn reads: the connections of its projects, narrowed by the caller.

A session reads its own project; a one-off question reads the projects it names; a
turn with neither reads the whole tenant. Retrieval is narrowed to the source scopes
those connections were ingested under, and a scope with nothing published fails
before any model is called.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from harborrag_core.contracts.errors import HarborNoIndexedContentError, HarborUnavailableError
from harborrag_core.ports.control_plane import ConnectionRepositoryPort

from .errors import ConnectionOutOfScopeError, UnknownReferenceError

SOURCE_SCOPE_FILTER = "source_scope_id"


@dataclass(frozen=True, slots=True)
class ConnectionSources:
    """One connection's public ref, the source scopes it was ingested under, and whether
    any of its documents is published."""

    ref: str
    source_scope_ids: tuple[str, ...]
    published: bool


class ConnectionCatalog(Protocol):
    """The connections of a project and the source scopes they were ingested under."""

    async def project_connections(
        self, tenant_id: str, project_id: str
    ) -> tuple[ConnectionSources, ...]:
        """Every connection of ``project_id`` with its source scopes and published state."""
        ...


class ConnectionIndex(Protocol):
    """Source scopes and published state of connections, keyed by their ``source_key``."""

    async def source_scope_ids_by_connection(
        self, *, tenant_id: str, connection_ids: Sequence[str]
    ) -> dict[str, list[str]]: ...

    async def published_connection_ids(
        self, *, tenant_id: str, connection_ids: Sequence[str]
    ) -> set[str]: ...


class ControlPlaneConnectionCatalog:
    """``ConnectionCatalog`` over the control-plane connections and the ingestion records.

    A project costs three reads whatever its number of connections: the connections, their
    source scopes, and whether each has a published document.
    """

    def __init__(
        self,
        connections: Callable[[], ConnectionRepositoryPort],
        index: Callable[[], Awaitable[ConnectionIndex]],
    ) -> None:
        self._connections = connections
        self._index = index

    async def project_connections(
        self, tenant_id: str, project_id: str
    ) -> tuple[ConnectionSources, ...]:
        """Every connection of ``project_id`` with its source scopes and published state.

        Raises:
            HarborUnavailableError: No control-plane database is configured.
        """
        rows, _ = await self._connections().list(
            tenant_ids=frozenset({tenant_id}), project_id=project_id
        )
        if not rows:
            return ()
        keys = [row.source_key for row in rows]
        index = await self._index()
        scopes = await index.source_scope_ids_by_connection(
            tenant_id=tenant_id, connection_ids=keys
        )
        published = await index.published_connection_ids(tenant_id=tenant_id, connection_ids=keys)
        return tuple(
            ConnectionSources(
                ref=row.ref,
                source_scope_ids=tuple(scopes.get(row.source_key, ())),
                published=row.source_key in published,
            )
            for row in rows
        )


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """The settings level and the evidence filters one turn retrieves with.

    ``project_id`` is the project whose settings level applies; ``None`` reads the
    tenant level.
    """

    project_id: str | None = None
    filters: dict[str, object] = field(default_factory=dict)


async def resolve_scope(
    catalog: ConnectionCatalog | None,
    *,
    tenant_id: str,
    projects: Sequence[str],
    connections: Sequence[str],
    filters: Mapping[str, object],
) -> RetrievalScope:
    """Narrow retrieval to the connections a turn may read.

    Args:
        catalog: Where the projects' connections are looked up.
        tenant_id: The tenant the turn runs in.
        projects: The session's project, or a one-off question's projects; empty for
            the whole tenant.
        connections: Connection refs (``<project_id>/<connection_id>``) the caller
            narrows to; empty for every connection of ``projects``.
        filters: The caller's evidence filters.

    Returns:
        The settings level and the evidence filters, the source scopes included.

    Raises:
        ConnectionOutOfScopeError: A connection, or a ``source_scope_id`` filter, falls
            outside ``projects``.
        UnknownReferenceError: A connection does not exist in the tenant.
        HarborNoIndexedContentError: Nothing is published in the selected connections.
        HarborUnavailableError: The catalog is not configured.
    """
    level = projects[0] if len(projects) == 1 else None
    if not connections and not projects:
        return RetrievalScope(level, dict(filters))
    selected = await _selected(catalog, tenant_id, projects, connections)
    scopes = sorted({scope for connection in selected for scope in connection.source_scope_ids})
    if not scopes or not any(connection.published for connection in selected):
        where = ", ".join(projects) or ", ".join(connections)
        raise HarborNoIndexedContentError(f"Nothing is indexed yet in {where}")
    narrowed = _narrow(scopes, filters.get(SOURCE_SCOPE_FILTER))
    return RetrievalScope(level, {**filters, SOURCE_SCOPE_FILTER: narrowed})


async def _selected(
    catalog: ConnectionCatalog | None,
    tenant_id: str,
    projects: Sequence[str],
    connections: Sequence[str],
) -> list[ConnectionSources]:
    if catalog is None:
        raise HarborUnavailableError("the connection catalog is not configured")
    if not connections:
        return [
            connection
            for project_id in projects
            for connection in await catalog.project_connections(tenant_id, project_id)
        ]
    outside = [ref for ref in connections if projects and ref.partition("/")[0] not in projects]
    if outside:
        raise ConnectionOutOfScopeError(
            f"connections fall outside the scope's projects: {', '.join(outside)}",
            {"field": "connections"},
        )
    known: dict[str, ConnectionSources] = {}
    for project_id in dict.fromkeys(ref.partition("/")[0] for ref in connections):
        for connection in await catalog.project_connections(tenant_id, project_id):
            known[connection.ref] = connection
    missing = [ref for ref in connections if ref not in known]
    if missing:
        raise UnknownReferenceError(
            f"connections not found: {', '.join(missing)}", {"field": "connections"}
        )
    return [known[ref] for ref in dict.fromkeys(connections)]


def _narrow(scopes: list[str], requested: object) -> list[str]:
    if requested is None:
        return scopes
    wanted = {requested} if isinstance(requested, str) else set(requested)  # type: ignore[call-overload]
    narrowed = [scope for scope in scopes if scope in wanted]
    if not narrowed:
        raise ConnectionOutOfScopeError(
            "source_scope_id falls outside the scope's connections", {"field": "filters"}
        )
    return narrowed
