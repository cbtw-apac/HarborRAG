from __future__ import annotations

import logging

import pytest

from harborrag_core.contracts.errors import (
    HarborAuthorizationUnavailableError,
    HarborLimitExceededError,
)
from harborrag_core.indexing import FilterOperator, VectorFilter, VectorFilterCondition
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


class OverBudgetPermissions:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def allowed_document_ids(self, *args, **kwargs):
        raise self.error

    async def authorized_document_ids(self, *args, **kwargs):
        raise AssertionError("scope_filter must not run the final check")


def _reader_context() -> StorageOperationContext:
    access = AccessContext(principal_id="reader", tenant_id="DEFAULT")
    return StorageOperationContext.for_access(
        access, operation_kind="search", idempotency_key="test"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters",
    [
        None,
        VectorFilter(
            must=[
                VectorFilterCondition(
                    field="source_scope_id", operator=FilterOperator.EQUALS, value="scope-1"
                )
            ]
        ),
    ],
)
async def test_over_budget_scope_degrades_to_the_callers_filters(
    filters: VectorFilter | None, caplog: pytest.LogCaptureFixture
) -> None:
    """Above the enumeration budget the prefilter is dropped, not the search.

    validate() still re-checks every candidate against canonical ACLs, so the
    degraded path cannot return a document the reader may not see.
    """

    permissions = RetrievalPermissions(
        OverBudgetPermissions(  # type: ignore[arg-type]
            HarborLimitExceededError(
                "authorized document enumeration exceeds the configured budget"
            )
        )
    )

    with caplog.at_level(logging.INFO, logger="harborrag.runtime.retrieval.permissions"):
        assert await permissions.scope_filter(filters, _reader_context()) == filters

    records = [r for r in caplog.records if getattr(r, "reason", None)]
    assert [(r.levelno, r.reason) for r in records] == [
        (logging.INFO, "permission_scope_over_budget")
    ]


@pytest.mark.asyncio
async def test_any_other_scope_failure_still_denies_with_authorization_unavailable() -> None:
    permissions = RetrievalPermissions(
        OverBudgetPermissions(RuntimeError("boom"))  # type: ignore[arg-type]
    )
    with pytest.raises(HarborAuthorizationUnavailableError, match="AUTHORIZATION_UNAVAILABLE"):
        await permissions.scope_filter(None, _reader_context())
