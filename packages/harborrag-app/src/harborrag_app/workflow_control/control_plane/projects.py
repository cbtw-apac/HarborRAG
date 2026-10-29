"""Project use cases: list, read, create or update, and delete with its connections.

Handles:
- ``put`` on an id the caller chooses, with ETag preconditions
- ``delete``: immediate for an empty project, a background cascade otherwise

Project ids are unique across tenants in 2.0.0 storage, so an id another
tenant holds is reported as ``already_exists``.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any
from uuid import uuid4

from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborConflictError,
    HarborInUseError,
    HarborNotFoundError,
    HarborPreconditionFailedError,
    HarborValidationError,
)
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.connection import CONNECTION_ID_PATTERN, Connection
from harborrag_core.domain.job import Job
from harborrag_core.domain.project import Project
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config.connectors.models import ConnectionValidationError, FieldIssue

from .connection_views import project_view
from .connections import ConnectionRuns, ConnectionService
from .effect_recovery import log_activity

logger = logging.getLogger("harborrag.app.workflow_control.control_plane.projects")


class ProjectIdTakenError(HarborConflictError):
    """The project id is already used by another tenant."""

    error_code = "already_exists"


class ProjectService:
    """Facade over projects and the cascade that removes their connections."""

    def __init__(
        self,
        *,
        control_plane: Callable[[], ControlPlaneRepositories],
        runs: Callable[[], Awaitable[ConnectionRuns]],
        connections: ConnectionService,
    ) -> None:
        self._control_plane = control_plane
        self._runs = runs
        self._connections = connections

    async def list_page(
        self,
        *,
        tenant_id: str,
        query: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return one page of the tenant's projects ordered by id, optionally name-filtered."""
        after = _decode_cursor(cursor) if cursor else ""
        projects = sorted(
            (
                project
                for project in await self._control_plane().projects.list(
                    tenant_ids=frozenset({tenant_id})
                )
                if project.id > after and (not query or query.casefold() in project.name.casefold())
            ),
            key=lambda project: project.id,
        )
        page = projects[:limit]
        next_cursor = _encode_cursor(page[-1].id) if len(projects) > limit else None
        return [await self._view(project) for project in page], next_cursor

    async def get(self, *, tenant_id: str, project_id: str) -> tuple[dict[str, Any], int]:
        """Return a project's view and version.

        Raises:
            HarborNotFoundError: No such project in the tenant.
        """
        project = await self._require(tenant_id, project_id)
        return await self._view(project), project.version

    async def put(  # noqa: PLR0913 - one body plus its preconditions
        self,
        *,
        tenant_id: str,
        project_id: str,
        name: str,
        description: str | None,
        actor: str,
        expected_version: int | None = None,
        create_only: bool = False,
    ) -> tuple[dict[str, Any], bool, int]:
        """Create or update a project.

        Returns:
            The view, whether it was created, and the stored version.

        Raises:
            ConnectionValidationError: ``project_id`` breaks the id pattern.
            ProjectIdTakenError: Another tenant already uses ``project_id``.
            HarborPreconditionFailedError: ``If-Match`` or ``If-None-Match`` does not hold.
        """
        if not CONNECTION_ID_PATTERN.fullmatch(project_id):
            raise ConnectionValidationError(
                [FieldIssue("project_id", "must match ^[a-z0-9][a-z0-9_-]{0,62}$")]
            )
        if not name.strip():
            raise HarborValidationError("name must not be empty")
        projects = self._control_plane().projects
        existing = await projects.get(project_id, tenant_ids=None)
        if existing is not None and existing.tenant_id != tenant_id:
            raise ProjectIdTakenError(f"project id {project_id!r} is already used")
        if existing is None:
            if expected_version is not None:
                raise HarborPreconditionFailedError("project does not exist")
            created = await projects.create(
                Project(
                    id=project_id,
                    name=name,
                    collection=project_id,
                    tenant_id=tenant_id,
                    description=description or "",
                )
            )
            await self._record(created, "created", actor)
            return await self._view(created), True, created.version
        if create_only:
            raise HarborPreconditionFailedError("project already exists")
        updated = await projects.update(
            replace(existing, name=name, description=description or ""),
            expected_version=expected_version,
        )
        await self._record(updated, "updated", actor)
        return await self._view(updated), False, updated.version

    async def delete(
        self, *, tenant_id: str, project_id: str, actor: str, cascade: bool = False
    ) -> str | None:
        """Delete a project; with connections, only as a background cascade.

        Returns:
            None when the empty project was deleted, else the cascade's job id.

        Raises:
            HarborNotFoundError: No such project in the tenant.
            HarborInUseError: It has connections and ``cascade`` is false.
            HarborAlreadyRunningError: One of its connections is ingesting.
        """
        control_plane = self._control_plane()
        project = await self._require(tenant_id, project_id)
        connections = await self._project_connections(project)
        if not connections:
            await control_plane.projects.delete(project_id, tenant_ids=frozenset({tenant_id}))
            await self._record(project, "deleted", actor)
            return None
        if not cascade:
            raise HarborInUseError(
                "the project still has connections; use cascade=true",
                details={"connections": [connection.ref for connection in connections]},
            )
        for connection in connections:
            if await self._connections.connection_busy(tenant_id, connection.source_key):
                raise HarborAlreadyRunningError(
                    f"connection {connection.ref!r} is ingesting",
                    details={"connection": connection.ref},
                )
        job = await control_plane.jobs.save(
            Job(
                id=f"job_{uuid4().hex[:16]}",
                source_id=project_id,
                project_id=project_id,
                job_type="delete_project",
                tenant_id=tenant_id,
                status="running",
                payload={"project": project_id, "actor": actor},
            )
        )
        for connection in connections:
            await control_plane.connections.set_status(
                tenant_id=tenant_id,
                source_key=connection.source_key,
                status="deleting",
                detail={"job_id": job.id, "purge": True},
            )
        await self._record(project, "deleted", actor)
        self._connections.spawn(self.finish_deletion(project, job_id=job.id))
        return job.id

    async def finish_deletion(self, project: Project, *, job_id: str) -> None:
        """Remove every connection of the project, then the project itself."""
        control_plane = self._control_plane()
        for connection in await self._project_connections(project):
            await self._connections.finish_deletion(connection, job_id=job_id, purge=True)
        remaining = await self._project_connections(project)
        job = await control_plane.jobs.get(job_id, tenant_ids=frozenset({project.tenant_id}))
        if remaining:
            if job is not None:
                await control_plane.jobs.save(
                    replace(job, status="failed", last_error="connections remain")
                )
            return
        await control_plane.projects.delete(project.id, tenant_ids=frozenset({project.tenant_id}))
        if job is not None:
            await control_plane.jobs.save(replace(job, status="succeeded", finished_at=utc_now()))

    async def resume_deletions(self) -> int:
        """Finish every project cascade a previous process left running."""
        control_plane = self._control_plane()
        jobs = [
            job
            for job in await control_plane.jobs.list("running", tenant_ids=None)
            if job.job_type == "delete_project"
        ]
        for job in jobs:
            project = await control_plane.projects.get(
                job.project_id, tenant_ids=frozenset({job.tenant_id})
            )
            if project is not None:
                await self.finish_deletion(project, job_id=job.id)
        return len(jobs)

    async def _require(self, tenant_id: str, project_id: str) -> Project:
        """Return the project or raise not found."""
        project = await self._control_plane().projects.get(
            project_id, tenant_ids=frozenset({tenant_id})
        )
        if project is None:
            raise HarborNotFoundError(f"project {project_id!r} not found")
        return project

    async def _project_connections(self, project: Project) -> list[Connection]:
        """Every connection of the project."""
        connections, _ = await self._control_plane().connections.list(
            tenant_ids=frozenset({project.tenant_id}), project_id=project.id
        )
        return connections

    async def _view(self, project: Project) -> dict[str, Any]:
        """Return the API view with connection and document counts."""
        connections = await self._project_connections(project)
        runs = await self._runs()
        documents = 0
        for connection in connections:
            documents += await runs.connection_document_count(
                tenant_id=project.tenant_id, connection_id=connection.source_key
            )
        return project_view(project, connections=len(connections), documents=documents)

    async def _record(self, project: Project, verb: str, actor: str) -> None:
        """Append an audit entry for a project write."""
        await log_activity(
            self._control_plane(),
            ActivityEntry(
                id=f"act_{uuid4().hex}",
                tenant_id=project.tenant_id,
                actor=actor,
                verb=verb,
                entity_type="project",
                entity_id=project.id,
                summary=f"{verb.capitalize()} project {project.id!r}",
            ),
        )


def _encode_cursor(project_id: str) -> str:
    """Opaque cursor after ``project_id``."""
    payload = json.dumps({"after": project_id}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> str:
    """Project id a cursor points after.

    Raises:
        HarborValidationError: The cursor is malformed.
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        return str(payload["after"])
    except (KeyError, TypeError, ValueError) as error:
        raise HarborValidationError("project cursor is invalid") from error
