"""Connector classes the registry tests import lazily by module path."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel

from harborrag_adapters.sources.registry import connector
from harborrag_core.sources import ConnectorConfig, FieldClass
from harborrag_core.sources.testing import FakeCheckpoint, FakeConfig, FakeSourceConnector

IMPORTED = True


@connector("fake", config=FakeConfig, checkpoint=FakeCheckpoint)
class DeclaredFake(FakeSourceConnector):
    pass


class _Untagged(ConnectorConfig):
    page_size: int = 1


class _Unversioned(BaseModel):
    offset: int = 0


@connector("broken", config=_Untagged, checkpoint=_Unversioned)
class BrokenFake(FakeSourceConnector):
    async def fetch_keys(self, keys):  # type: ignore[no-untyped-def, override]
        return []  # a coroutine, not an async generator


class Tagged(ConnectorConfig):
    page_size: Annotated[int, FieldClass.COSMETIC] = 1


@connector("misnamed", config=Tagged, checkpoint=FakeCheckpoint)
class Misnamed(FakeSourceConnector):
    pass


class Undeclared(FakeSourceConnector):
    pass
