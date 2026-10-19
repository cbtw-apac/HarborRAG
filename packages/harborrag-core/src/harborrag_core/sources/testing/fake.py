"""An in-memory source connector that honours the whole port.

It is the reference the contract suite is proven against, and a stand-in for
real connectors in runtime tests. Top-level items are read in
``(updated_at, key)`` order, a page at a time, with a checkpoint after each
page; children are emitted with their parent, whatever their own update time.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, ValidationError

from ..acl import SourceAcl
from ..config import ConnectorConfig, FieldClass
from ..events import (
    BlobRef,
    CheckpointMark,
    ConnectorEvent,
    DocKey,
    ItemFailure,
    SourceRecord,
    SyncWindow,
    Tombstone,
)
from ..port import (
    Capabilities,
    DeletionMode,
    DiscoveryMode,
    PreflightCheck,
    PreflightResult,
    SourceAuthProvider,
    SourceHttpClient,
    SyncPolicy,
)

FAKE_PARENT = "fake_item"
FAKE_CHILD = "fake_child"


@dataclass(frozen=True)
class FakeItem:
    """One item in the fake source. A child names its parent.

    ``acl=None`` means the item is readable by exactly the readers of its source:
    the fake emits an inherited ACL in realm ``fake``, scope ``fake:<source_id>``.
    """

    key: DocKey
    updated_at: datetime
    body: str = ""
    version: str = "1"
    parent: DocKey | None = None
    blob: bytes | None = None
    acl: SourceAcl | None = None


class FakeConfig(ConnectorConfig):
    page_size: Annotated[int, FieldClass.COSMETIC] = 2


class FakeCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    window_start: datetime
    window_end: datetime
    offset: int = 0


class FakeSourceConnector:
    """In-memory connector; ``failing`` keys yield an ``ItemFailure`` instead."""

    capabilities: ClassVar[Capabilities] = Capabilities(
        discovery=DiscoveryMode.TIMESTAMP,
        kinds=frozenset({FAKE_PARENT, FAKE_CHILD}),
        deletions=frozenset({DeletionMode.CHILD_DIFF, DeletionMode.RECONCILE}),
        blobs=True,
        policy=SyncPolicy(),
    )

    def __init__(
        self,
        items: Iterable[FakeItem] = (),
        *,
        page_size: int = 2,
        failing: Iterable[DocKey] = (),
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self._items = {item.key: item for item in items}
        self._page_size = page_size
        self._failing = frozenset(failing)

    @classmethod
    async def create(
        cls,
        *,
        source_id: str,
        auth: SourceAuthProvider,
        http: SourceHttpClient,
        config: ConnectorConfig,
        logger: logging.Logger,
    ) -> Self:
        del source_id, auth, http, logger
        page_size = config.page_size if isinstance(config, FakeConfig) else 2
        return cls(page_size=page_size)

    async def preflight(self) -> PreflightResult:
        return PreflightResult((PreflightCheck("items", True, f"{len(self._items)} items"),))

    def parse_checkpoint(self, raw: object) -> FakeCheckpoint | None:
        try:
            return FakeCheckpoint.model_validate(raw)
        except ValidationError:
            return None

    async def fetch(
        self, window: SyncWindow, checkpoint: FakeCheckpoint | None
    ) -> AsyncIterator[ConnectorEvent[FakeCheckpoint]]:
        if checkpoint is not None and (checkpoint.window_start, checkpoint.window_end) != (
            window.start,
            window.end,
        ):
            checkpoint = None
        offset = checkpoint.offset if checkpoint else 0
        pending = self._top_level(window)[offset:]
        for start in range(0, len(pending), self._page_size):
            for item in pending[start : start + self._page_size]:
                for event in self._emit(item):
                    yield event
            offset += len(pending[start : start + self._page_size])
            yield CheckpointMark(
                FakeCheckpoint(window_start=window.start, window_end=window.end, offset=offset)
            )
        yield CheckpointMark(
            FakeCheckpoint(window_start=window.start, window_end=window.end, offset=offset),
            {"watermark": window.end.isoformat()},
        )

    async def fetch_keys(
        self, keys: Sequence[DocKey]
    ) -> AsyncIterator[ConnectorEvent[FakeCheckpoint]]:
        for key in keys:
            item = self._items.get(key)
            if item is None:
                yield Tombstone(key)
            elif key in self._failing:
                yield ItemFailure("injected", key=key)
            else:
                yield self._record(item)

    async def list_keys(self) -> AsyncIterator[DocKey]:
        for key in sorted(self._items):
            yield key

    async def open_blob(self, ref: BlobRef) -> AsyncIterator[bytes]:
        item = self._items.get(DocKey.parse(ref.locator))
        if item is None or item.blob is None:
            raise KeyError(ref.locator)
        yield item.blob

    def _top_level(self, window: SyncWindow) -> list[FakeItem]:
        chosen = [
            item
            for item in self._items.values()
            if item.parent is None and window.contains(item.updated_at)
        ]
        return sorted(chosen, key=lambda item: (item.updated_at, item.key))

    def _children(self, key: DocKey) -> list[FakeItem]:
        return sorted(
            (item for item in self._items.values() if item.parent == key),
            key=lambda item: item.key,
        )

    def _emit(self, item: FakeItem) -> list[ConnectorEvent[Any]]:
        if item.key in self._failing:
            return [ItemFailure("injected", key=item.key)]
        events: list[ConnectorEvent[Any]] = [self._record(item)]
        for child in self._children(item.key):
            if child.key in self._failing:
                events.append(ItemFailure("injected", key=child.key))
            else:
                events.append(self._record(child))
        return events

    def _record(self, item: FakeItem) -> SourceRecord:
        children = None
        if item.parent is None:
            children = frozenset(child.key for child in self._children(item.key))
        blob = None
        if item.blob is not None:
            blob = BlobRef(str(item.key), size=len(item.blob))
        acl = item.acl
        if acl is None:
            acl = SourceAcl.inherited(realm="fake", scope=f"fake:{item.key.source_id}")
        return SourceRecord(
            key=item.key,
            parent=item.parent,
            title=item.key.external_id,
            body=item.body,
            version=item.version,
            updated_at=item.updated_at,
            metadata={"kind": item.key.kind},
            blob=blob,
            children=children,
            acl=acl,
        )
