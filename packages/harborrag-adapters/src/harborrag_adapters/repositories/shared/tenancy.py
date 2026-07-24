from __future__ import annotations

from harborrag_adapters.repositories.errors import (
    HarborStorageAuthorizationError,
    StorageErrorContext,
)
from harborrag_core.schemas.storage import StorageOperationContext


def ensure_tenant(
    record_tenant: str,
    context: StorageOperationContext,
    *,
    error_context: StorageErrorContext,
) -> None:
    """Reject cross-tenant records through the stable storage error contract."""
    if str(record_tenant) != str(context.tenant_id):
        raise HarborStorageAuthorizationError(
            "record tenant_id must match operation context tenant_id",
            context=error_context,
        )
