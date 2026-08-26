"""Scope-isolation tests for the memory port's ``visible_to`` predicate.

These are the security-relevant contract for ``harborrag-memory``: every
adapter's ``search`` must agree with this pure function, so it is exercised
directly, independent of any storage backend.
"""

from __future__ import annotations

import pytest

from harborrag_core.ports.memory import MemoryOwner, MemoryScope, scope_owner_fields, visible_to

_TENANT_A_RUN = MemoryOwner(
    tenant_id="tenant-a",
    project_id="proj-1",
    principal_id="user-1",
    session_id="session-1",
    run_id="run-1",
)


@pytest.mark.whitebox
def test_run_scope_requires_every_field_through_run_id() -> None:
    assert scope_owner_fields(MemoryScope.RUN) == (
        "tenant_id",
        "principal_id",
        "session_id",
        "run_id",
    )


@pytest.mark.whitebox
def test_run_scoped_memory_is_not_visible_to_a_different_run() -> None:
    caller = MemoryOwner(
        tenant_id="tenant-a",
        project_id="proj-1",
        principal_id="user-1",
        session_id="session-1",
        run_id="run-2",
    )
    assert not visible_to(MemoryScope.RUN, _TENANT_A_RUN, caller)


@pytest.mark.whitebox
def test_run_scoped_memory_is_visible_to_its_own_run() -> None:
    assert visible_to(MemoryScope.RUN, _TENANT_A_RUN, _TENANT_A_RUN)


@pytest.mark.whitebox
def test_user_scoped_memory_does_not_cross_tenants() -> None:
    same_user_other_tenant = MemoryOwner(tenant_id="tenant-b", principal_id="user-1")
    memory_owner = MemoryOwner(tenant_id="tenant-a", principal_id="user-1")
    assert not visible_to(MemoryScope.USER, memory_owner, same_user_other_tenant)


@pytest.mark.whitebox
def test_user_scoped_memory_does_not_cross_users_in_the_same_tenant() -> None:
    memory_owner = MemoryOwner(tenant_id="tenant-a", principal_id="user-1")
    other_user = MemoryOwner(tenant_id="tenant-a", principal_id="user-2")
    assert not visible_to(MemoryScope.USER, memory_owner, other_user)


@pytest.mark.whitebox
def test_tenant_scoped_memory_is_visible_to_any_principal_in_the_tenant() -> None:
    memory_owner = MemoryOwner(tenant_id="tenant-a")
    other_principal = MemoryOwner(tenant_id="tenant-a", principal_id="user-9", run_id="run-9")
    assert visible_to(MemoryScope.TENANT, memory_owner, other_principal)


@pytest.mark.whitebox
def test_global_scoped_memory_is_visible_across_tenants() -> None:
    memory_owner = MemoryOwner(tenant_id="tenant-a")
    caller = MemoryOwner(tenant_id="tenant-b")
    assert visible_to(MemoryScope.GLOBAL, memory_owner, caller)


@pytest.mark.whitebox
def test_caller_missing_a_required_field_never_matches() -> None:
    memory_owner = MemoryOwner(tenant_id="tenant-a", principal_id="user-1", session_id="session-1")
    caller_without_session = MemoryOwner(tenant_id="tenant-a", principal_id="user-1")
    assert not visible_to(MemoryScope.SESSION, memory_owner, caller_without_session)
