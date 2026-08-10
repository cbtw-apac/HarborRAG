"""SQLAlchemy control-plane repositories grouped by capability."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.schemas import (
    ProjectRow,
    SourceRow,
)
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.domain.project import Project
from harborrag_core.domain.source_config import SourceConfig

from .mapping import project_to_domain, source_to_domain, utc_now
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


@dataclass(slots=True)
class SqlSourceRepository:
    """SourceRepositoryPort over the sources table."""

    sessions: SessionFactory

    async def list(
        self,
        project_id: str | None = None,
        *,
        tenant_ids: frozenset[str] | None,
    ) -> list[SourceConfig]:
        """Sources visible to ``tenant_ids`` (None: unrestricted), optionally scoped to a project."""
        statement = sa.select(SourceRow).order_by(SourceRow.id)
        if project_id is not None:
            statement = statement.where(SourceRow.project_id == project_id)
        if tenant_ids is not None:
            statement = statement.where(SourceRow.tenant_id.in_(tenant_ids))
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            return [source_to_domain(row) for row in rows]

    async def get(
        self, source_id: str, *, tenant_ids: frozenset[str] | None
    ) -> SourceConfig | None:
        """One source by id within ``tenant_ids``, or None."""
        async with self.sessions() as session:
            row = await session.get(SourceRow, source_id)
            if row is None or (tenant_ids is not None and row.tenant_id not in tenant_ids):
                return None
            return source_to_domain(row)

    async def create(self, source: SourceConfig) -> SourceConfig:
        """Insert a new sources row; config must already carry secret_refs."""
        async with self.sessions.begin() as session:
            session.add(
                SourceRow(
                    id=source.id,
                    tenant_id=source.tenant_id,
                    project_id=source.project_id,
                    source_type=source.source_type,
                    name=source.name,
                    config_json=dict(source.config),
                    secret_refs=list(source.secret_refs),
                    schedule=source.schedule,
                    status=source.status,
                )
            )
        return source

    async def update(self, source: SourceConfig) -> SourceConfig:
        """Overwrite the configurable fields of an existing source."""
        async with self.sessions.begin() as session:
            row = await session.get(SourceRow, source.id)
            if row is None:
                raise KeyError(source.id)
            if row.tenant_id != source.tenant_id:
                raise HarborConflictError("source tenant identity is immutable")
            row.name = source.name
            row.source_type = source.source_type
            row.config_json = dict(source.config)
            row.secret_refs = list(source.secret_refs)
            row.schedule = source.schedule
            row.status = source.status
        return source

    async def delete(self, source_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Delete the source row within ``tenant_ids``."""
        statement = sa.delete(SourceRow).where(SourceRow.id == source_id)
        if tenant_ids is not None:
            statement = statement.where(SourceRow.tenant_id.in_(tenant_ids))
        async with self.sessions.begin() as session:
            await session.execute(statement)
