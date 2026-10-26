"""Fixtures for the per-turn conversation-context tests."""

from __future__ import annotations

import pytest
from context_test_fakes import (
    EmbedderFake,
    EntityResolverFake,
    MemoryIndexFake,
    MemoryRepositoryFake,
    MessageStoreFake,
)

from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import MemoryOwner


@pytest.fixture
def owner() -> MemoryOwner:
    return MemoryOwner(
        tenant_id="tenant-1",
        project_id="handbook",
        user_id="user-1",
        principal_id="principal-1",
        session_id="s-1",
    )


@pytest.fixture
def identity(owner: MemoryOwner) -> ConversationIdentity:
    return ConversationIdentity(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id or "",
        session_id=owner.session_id or "",
        user_id=owner.user_id or owner.principal_id or "",
    )


@pytest.fixture
def store() -> MessageStoreFake:
    return MessageStoreFake()


@pytest.fixture
def memories() -> MemoryRepositoryFake:
    return MemoryRepositoryFake()


@pytest.fixture
def index(memories: MemoryRepositoryFake) -> MemoryIndexFake:
    return MemoryIndexFake(repository=memories)


@pytest.fixture
def embedder() -> EmbedderFake:
    return EmbedderFake()


@pytest.fixture
def resolver() -> EntityResolverFake:
    return EntityResolverFake()
