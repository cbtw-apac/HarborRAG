from __future__ import annotations

from harborrag_core.schemas.storage import StorageOperationContext


def make_context(tenant: str = "tenant-a") -> StorageOperationContext:
    return StorageOperationContext.system(tenant_id=tenant)
