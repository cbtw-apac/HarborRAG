from __future__ import annotations

import pytest

from harborrag_core.contracts.errors import HarborAuthorizationUnavailableError
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.retrieval.permissions import RetrievalPermissions


class UnavailablePermissions:
    async def allowed_document_ids(self, *args, **kwargs):
        raise TimeoutError("permission database timed out")

    async def authorized_document_ids(self, *args, **kwargs):
        raise TimeoutError("permission database timed out")


@pytest.mark.asyncio
async def test_acl_lookup_failure_is_not_an_empty_search() -> None:
    access = AccessContext(principal_id="reader", tenant_id="DEFAULT")
    context = StorageOperationContext.for_access(
        access, operation_kind="search", idempotency_key="test"
    )
    permissions = RetrievalPermissions(UnavailablePermissions())  # type: ignore[arg-type]
    with pytest.raises(HarborAuthorizationUnavailableError, match="AUTHORIZATION_UNAVAILABLE"):
        await permissions.scope_filter(None, context)


@pytest.mark.asyncio
async def test_shared_scope_does_not_enumerate_acl_ids() -> None:
    access = AccessContext(principal_id="reader", tenant_id="DEFAULT", corpus_mode="tenant_shared")
    context = StorageOperationContext.for_access(
        access, operation_kind="search", idempotency_key="test"
    )
    permissions = RetrievalPermissions(UnavailablePermissions())  # type: ignore[arg-type]
    assert await permissions.scope_filter(None, context) is None
