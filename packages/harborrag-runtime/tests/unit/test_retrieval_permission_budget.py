"""A principal who can read a large corpus is served from it, not refused."""

from __future__ import annotations

import asyncio

import pytest

from harborrag_core.contracts import HarborConflictError
from harborrag_core.contracts.errors import HarborAuthorizationUnavailableError
from harborrag_core.indexing import FilterOperator, VectorFilter, VectorFilterCondition
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.retrieval.permissions import RetrievalPermissions

ACCESS = AccessContext(principal_id="reader", tenant_id="DEFAULT")
CONTEXT = StorageOperationContext.for_access(
    ACCESS, operation_kind="search", idempotency_key="test"
)


class OverBudget:
    """The adapter's behaviour once the readable set is larger than the budget."""

    def __init__(self, scopes=("HR-2025",), scopes_error: Exception | None = None):
        self.scopes = scopes
        self.scopes_error = scopes_error

    async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
        raise HarborConflictError("authorized document enumeration exceeds the configured budget")

    async def allowed_source_scope_ids(self, tenant_id, *, access, limit=10000):
        if self.scopes_error is not None:
            raise self.scopes_error
        return self.scopes


class WithinBudget(OverBudget):
    async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
        return ("doc-1", "doc-2")


@pytest.mark.asyncio
async def test_over_budget_falls_back_to_the_readable_source_scopes() -> None:
    caller = VectorFilter(must=[VectorFilterCondition(field="issue_key", value="HR-1")])

    scoped = await RetrievalPermissions(OverBudget()).scope_filter(caller, CONTEXT)

    assert [(c.field, c.operator, c.value) for c in scoped.must] == [
        ("issue_key", FilterOperator.EQUALS, "HR-1"),
        ("source_scope_id", FilterOperator.IN, ["HR-2025"]),
    ]


@pytest.mark.asyncio
async def test_within_budget_the_readable_documents_are_listed() -> None:
    scoped = await RetrievalPermissions(WithinBudget()).scope_filter(None, CONTEXT)
    assert [(c.field, c.value) for c in scoped.must] == [("document_id", ["doc-1", "doc-2"])]


@pytest.mark.asyncio
async def test_no_readable_scope_denies_rather_than_widens() -> None:
    scoped = await RetrievalPermissions(OverBudget(scopes=())).scope_filter(None, CONTEXT)
    assert [(c.field, c.value) for c in scoped.must] == [("source_scope_id", [])]


@pytest.mark.asyncio
async def test_a_failing_fallback_is_still_authorization_unavailable() -> None:
    permissions = RetrievalPermissions(OverBudget(scopes_error=TimeoutError("db")))
    with pytest.raises(HarborAuthorizationUnavailableError, match="AUTHORIZATION_UNAVAILABLE"):
        await permissions.scope_filter(None, CONTEXT)


class CountingOverBudget(OverBudget):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.document_calls = 0
        self.scope_calls = 0

    async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
        self.document_calls += 1
        return await super().allowed_document_ids(tenant_id, access=access, limit=limit)

    async def allowed_source_scope_ids(self, tenant_id, *, access, limit=10000):
        self.scope_calls += 1
        return await super().allowed_source_scope_ids(tenant_id, access=access, limit=limit)


@pytest.mark.asyncio
async def test_a_second_over_budget_request_skips_the_document_enumeration() -> None:
    now = [100.0]
    repository = CountingOverBudget()
    permissions = RetrievalPermissions(repository, clock=lambda: now[0])

    for _ in range(3):
        scoped = await permissions.scope_filter(None, CONTEXT)
        assert [(c.field, c.value) for c in scoped.must] == [("source_scope_id", ["HR-2025"])]

    # Only the first request paid for the discarded 10k-row enumeration.
    assert (repository.document_calls, repository.scope_calls) == (1, 3)
    # Another principal is not affected by this one's corpus size.
    other = StorageOperationContext.for_access(
        AccessContext(principal_id="someone-else", tenant_id="DEFAULT"),
        operation_kind="search",
        idempotency_key="test",
    )
    await permissions.scope_filter(None, other)
    assert repository.document_calls == 2
    # Once the memory expires the documents are tried again, in case the set shrank.
    now[0] += 61
    await permissions.scope_filter(None, CONTEXT)
    assert repository.document_calls == 3


class SlowEnumeration(OverBudget):
    """The over-budget enumeration uses most of its own time budget."""

    async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
        await asyncio.sleep(0.15)
        return await super().allowed_document_ids(tenant_id, access=access, limit=limit)

    async def allowed_source_scope_ids(self, tenant_id, *, access, limit=10000):
        await asyncio.sleep(0.15)
        return await super().allowed_source_scope_ids(tenant_id, access=access, limit=limit)


@pytest.mark.asyncio
async def test_the_scope_fallback_has_its_own_time_budget(monkeypatch) -> None:
    from harborrag_runtime.retrieval import permissions as module

    monkeypatch.setattr(module, "_LOOKUP_TIMEOUT_SECONDS", 0.2)
    scoped = await RetrievalPermissions(SlowEnumeration()).scope_filter(None, CONTEXT)
    assert [(c.field, c.value) for c in scoped.must] == [("source_scope_id", ["HR-2025"])]
