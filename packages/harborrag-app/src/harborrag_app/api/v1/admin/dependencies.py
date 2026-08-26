"""Narrow application-service dependency for projection administration."""

from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request


class ProjectionAdminService(Protocol):
    async def projection_inventory(self, tenant: str) -> dict[str, object]: ...

    async def delete_projections(
        self,
        tenant: str,
        *,
        confirmation: str,
        stores: frozenset[str],
    ) -> dict[str, object]: ...


def projection_admin_service(request: Request) -> ProjectionAdminService:
    return cast(ProjectionAdminService, request.app.state.app_service)


ProjectionAdminServiceDependency = Annotated[
    ProjectionAdminService,
    Depends(projection_admin_service),
]
