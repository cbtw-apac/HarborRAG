"""SQLAlchemy control-plane repositories grouped by capability."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.schemas import (
    ProjectRow,
)
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.domain.project import Project

from .mapping import project_to_domain, utc_now
from .session import SessionFactory


@dataclass(slots=True)
class SqlProjectRepository:
    """ProjectRepositoryPort over the projects table."""

    sessions: SessionFactory

    async def list(self, *, tenant_ids: frozenset[str] | None) -> list[Project]:
        """Projects visible to ``tenant_ids`` (None: unrestricted), by creation time."""
        statement = sa.select(ProjectRow).order_by(ProjectRow.created_at)
        if tenant_ids is not None:
            statement = statement.where(ProjectRow.tenant_id.in_(tenant_ids))
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            return [project_to_domain(row) for row in rows]

    async def get(self, project_id: str, *, tenant_ids: frozenset[str] | None) -> Project | None:
        """One project by id within ``tenant_ids``, or None."""
        async with self.sessions() as session:
            row = await session.get(ProjectRow, project_id)
            if row is None or (tenant_ids is not None and row.tenant_id not in tenant_ids):
                return None
            return project_to_domain(row)

    async def create(self, project: Project) -> Project:
        """Insert a new projects row from the aggregate."""
        async with self.sessions.begin() as session:
            session.add(
                ProjectRow(
                    id=project.id,
                    tenant_id=project.tenant_id,
                    name=project.name,
                    description=project.description,
                    collection=project.collection,
                    status=project.status,
                    created_at=project.created_at,
                    updated_at=project.updated_at,
                    documents=project.stats.documents,
                    chunks=project.stats.chunks,
                    size_bytes=project.stats.size_bytes,
                    last_sync_at=project.stats.last_sync_at,
                )
            )
        return project

    async def update(self, project: Project) -> Project:
        """Overwrite the mutable fields of an existing project."""
        async with self.sessions.begin() as session:
            row = await session.get(ProjectRow, project.id)
            if row is None:
                raise KeyError(project.id)
            if row.tenant_id != project.tenant_id:
                raise HarborConflictError("project tenant identity is immutable")
            row.name = project.name
            row.description = project.description
            row.collection = project.collection
            row.status = project.status
            row.updated_at = utc_now()
            row.documents = project.stats.documents
            row.chunks = project.stats.chunks
            row.size_bytes = project.stats.size_bytes
            row.last_sync_at = project.stats.last_sync_at
        return project

    async def delete(self, project_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Delete the project row within ``tenant_ids`` (index tombstones are the engine's job)."""
        statement = sa.delete(ProjectRow).where(ProjectRow.id == project_id)
        if tenant_ids is not None:
            statement = statement.where(ProjectRow.tenant_id.in_(tenant_ids))
        async with self.sessions.begin() as session:
            await session.execute(statement)
