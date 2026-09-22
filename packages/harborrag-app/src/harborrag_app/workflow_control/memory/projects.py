"""Validate the optional project a chat or agent turn is scoped to."""

from __future__ import annotations

from typing import Any

from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.ports.control_plane import ProjectRepositoryPort

PROJECT_NOT_FOUND = "Project was not found"


def project_lookup(composition: Any) -> ProjectRepositoryPort | None:
    """The control-plane project repository, or None when no database is wired."""

    control_plane = getattr(composition, "control_plane", None)
    projects: ProjectRepositoryPort | None = getattr(control_plane, "projects", None)
    return projects


async def require_project(
    projects: ProjectRepositoryPort | None,
    project_id: str | None,
    *,
    tenant_id: str,
) -> None:
    """Reject a ``project_id`` that does not exist within ``tenant_id``.

    Mirrors the unknown-session contract: a foreign project is indistinguishable
    from a missing one (404), and without a control-plane database no project
    can be confirmed, so every ``project_id`` is unknown there.
    """

    if project_id is None:
        return
    if projects is None:
        raise HarborNotFoundError(PROJECT_NOT_FOUND)
    project = await projects.get(project_id, tenant_ids=frozenset({tenant_id}))
    if project is None:
        raise HarborNotFoundError(PROJECT_NOT_FOUND)


__all__ = ["PROJECT_NOT_FOUND", "project_lookup", "require_project"]
