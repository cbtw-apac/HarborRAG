"""A refused memory snapshot leaves no coroutine behind that nobody awaits."""

from __future__ import annotations

import gc
import warnings

import pytest

from harborrag_core.ports.memory import MemoryQuery
from harborrag_memory import MemoryManager, MemoryOwner
from harborrag_memory.errors import MemoryConfigurationError
from harborrag_memory.tiers import ShortTermMemory
from harborrag_memory.tiers.in_memory import InMemoryConversationMemory

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
@pytest.mark.parametrize("short_term", [True, False], ids=["short-term", "no-short-term"])
async def test_snapshot_without_long_term_memory_creates_no_unawaited_coroutine(
    short_term: bool,
) -> None:
    owner = MemoryOwner(tenant_id="T", principal_id="P", user_id="U", session_id="S")
    tier = ShortTermMemory(InMemoryConversationMemory()) if short_term else None
    manager = MemoryManager(short_term=tier)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(MemoryConfigurationError, match="long-term memory is not configured"):
            await manager.snapshot(owner, query=MemoryQuery(owner=owner))
        gc.collect()

    assert not [warning for warning in caught if "never awaited" in str(warning.message)]
