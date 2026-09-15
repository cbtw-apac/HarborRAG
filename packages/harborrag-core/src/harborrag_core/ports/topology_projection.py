"""Rebuildable, isolated semantic topology projection boundary."""

from typing import Protocol

from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.records import DocumentTopologyBuild


class TopologyProjectionPort(Protocol):
    async def write(
        self, build: DocumentTopologyBuild, *, context: StorageOperationContext
    ) -> None: ...

    async def verify(
        self, build: DocumentTopologyBuild, *, context: StorageOperationContext
    ) -> bool: ...

    async def delete_build(self, build_id: str, *, context: StorageOperationContext) -> None: ...
