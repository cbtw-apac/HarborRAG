from __future__ import annotations

from typing import Any


class AsyncConnection:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FalkorDBWithoutDirectClose:
    def __init__(self, **options: Any) -> None:
        del options
        self.connection = AsyncConnection()

    def select_graph(self, name: str) -> object:
        del name
        return object()

    async def list_graphs(self) -> list[str]:
        return []


class FakeAsyncQdrantClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.get_collections_calls = 0

    async def get_collections(self) -> list[str]:
        self.get_collections_calls += 1
        return []

    async def close(self) -> None:
        self.closed = True
