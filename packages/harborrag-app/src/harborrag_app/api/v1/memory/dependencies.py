"""Narrow application-service dependency for memory-administration routes."""

from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_app.workflow_control.memory import MemoryAccess
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.ports.memory import MemoryScope


class MemoryAdminService(Protocol):
    async def list_memories(
        self,
        access: MemoryAccess,
        *,
        scope: MemoryScope | None = None,
        limit: int = 20,
    ) -> AppResponse: ...

    async def delete_memory(self, access: MemoryAccess, memory_id: str) -> AppResponse: ...

    async def erase_memory_session(self, access: MemoryAccess) -> AppResponse: ...

    async def erase_memory_user(self, access: MemoryAccess, user_id: str) -> AppResponse: ...


def memory_admin_service(request: Request) -> MemoryAdminService:
    return cast(MemoryAdminService, request.app.state.app_service)


MemoryAdminServiceDependency = Annotated[MemoryAdminService, Depends(memory_admin_service)]
