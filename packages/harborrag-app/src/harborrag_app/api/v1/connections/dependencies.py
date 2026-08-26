"""Application-service dependency for connection catalog routes."""

from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request


class ConnectionCatalogService(Protocol):
    """Narrow application facade required by the connection transport."""

    async def list_connections(self) -> dict[str, object]: ...


def connection_catalog_service(request: Request) -> ConnectionCatalogService:
    return cast(ConnectionCatalogService, request.app.state.app_service)


ConnectionCatalogDependency = Annotated[
    ConnectionCatalogService,
    Depends(connection_catalog_service),
]
