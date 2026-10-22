"""Which tenants HarborRAG knows about.

There is no tenant registry: a tenant comes into being when the first ingestion
is submitted for it. ``TenantDirectory`` hides how "known" is decided, so a
registry can replace that rule later without touching its callers.
"""

from __future__ import annotations

from typing import Protocol

from harborrag_core.schemas.storage import StorageOperationContext


class TenantDirectory(Protocol):
    async def exists(self, tenant_id: str, *, context: StorageOperationContext) -> bool:
        """True once at least one ingestion task has been submitted for ``tenant_id``.

        Tenant discovery is global by nature, so ``context`` must be platform-scoped.
        """
        ...


__all__ = ["TenantDirectory"]
