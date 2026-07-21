from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from harborrag_adapters.repositories.state.sql import SQLLeaseStore
from harborrag_core.schemas.storage import StorageOperationContext
from sqlalchemy.exc import IntegrityError


class FakeResult:
    def __init__(self, *, rowcount: int = 0, scalar: int | None = None) -> None:
        self.rowcount = rowcount
        self._scalar = scalar

    def scalar_one(self) -> int:
        assert self._scalar is not None
        return self._scalar


class LeaseSession:
    def __init__(self, *, counter_exists: bool, lease_conflict: bool) -> None:
        self.counter_exists = counter_exists
        self.lease_conflict = lease_conflict
        self.operations: list[tuple[str, str]] = []

    async def execute(self, statement: Any) -> FakeResult:
        table = statement.get_final_froms()[0].name if statement.is_select else statement.table.name
        operation = (
            "update" if statement.is_update else "insert" if statement.is_insert else "select"
        )
        self.operations.append((operation, table))

        if table == "harbor_lease_fencing" and operation == "update":
            return FakeResult(rowcount=1 if self.counter_exists else 0)
        if table == "harbor_lease_fencing" and operation == "select":
            return FakeResult(scalar=2)
        if table == "harbor_leases" and operation == "update":
            assert "expires_at <=" in str(statement)
            return FakeResult(rowcount=0)
        if table == "harbor_leases" and operation == "insert" and self.lease_conflict:
            raise IntegrityError(str(statement), {}, RuntimeError("unique conflict"))
        return FakeResult(rowcount=1)


class _FakeBackend:
    _telemetry = None


def make_store(session: LeaseSession) -> SQLLeaseStore:
    store = SQLLeaseStore(_FakeBackend())  # type: ignore[arg-type]

    @asynccontextmanager
    async def session_context():
        yield session

    store._session = session_context  # type: ignore[method-assign]
    return store


@pytest.mark.asyncio
async def test_first_lease_claim_uses_atomic_insert_path() -> None:
    session = LeaseSession(counter_exists=False, lease_conflict=False)
    lease = await make_store(session).acquire(
        "resource",
        "owner",
        timedelta(seconds=30),
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert lease is not None
    assert lease.fencing_token == 1
    assert session.operations == [
        ("update", "harbor_lease_fencing"),
        ("insert", "harbor_lease_fencing"),
        ("update", "harbor_leases"),
        ("insert", "harbor_leases"),
    ]


@pytest.mark.asyncio
async def test_unique_lease_conflict_returns_no_lease() -> None:
    session = LeaseSession(counter_exists=True, lease_conflict=True)
    lease = await make_store(session).acquire(
        "resource",
        "owner-b",
        timedelta(seconds=30),
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert lease is None
