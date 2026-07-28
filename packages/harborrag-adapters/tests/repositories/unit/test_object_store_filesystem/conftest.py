from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.repositories.object_store.filesystem import (
    repository as filesystem_module,
)
from harborrag_core.schemas.storage import StorageOperationContext


def make_context(tenant: str = "tenant-a") -> StorageOperationContext:
    return StorageOperationContext(tenant_id=tenant)


@pytest.fixture(autouse=True)
def immediate_filesystem_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid the sandbox's unavailable worker-thread executor in unit tests."""

    async def immediate(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(filesystem_module.asyncio, "to_thread", immediate)
