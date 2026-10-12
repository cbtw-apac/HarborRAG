"""In-memory ``CredentialStore`` with real compare-and-swap semantics."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from ..credentials import Credential


class InMemoryCredentialStore:
    def __init__(self, initial: Mapping[tuple[str, str], Mapping[str, str]] | None = None) -> None:
        self._items = {key: Credential(values) for key, values in (initial or {}).items()}
        self._lock = asyncio.Lock()
        self.swaps = 0

    async def get(self, connection_id: str, name: str) -> Credential:
        return self._items[(connection_id, name)]

    async def compare_and_swap(
        self,
        connection_id: str,
        name: str,
        *,
        expected_version: int,
        values: Mapping[str, str],
    ) -> Credential | None:
        async with self._lock:
            current = self._items.get((connection_id, name))
            if (current.version if current else 0) != expected_version:
                return None
            stored = Credential(values, expected_version + 1)
            self._items[(connection_id, name)] = stored
            self.swaps += 1
            return stored
