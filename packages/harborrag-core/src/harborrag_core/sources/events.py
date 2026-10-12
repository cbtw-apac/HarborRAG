"""Values a source connector yields while reading one sync window.

A connector reads; the runtime decides. Everything here is an immutable value
so the runtime can stage, retry, and compare events without asking the
connector again. Identity is always a :class:`DocKey` built from the source's
own stable ID; URLs travel in metadata and never identify a record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote

from pydantic import BaseModel

from harborrag_core.base import FrozenDict, _deep_freeze

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
"""Start of the first window for a connection that has never synced."""


def _frozen_state(state: Mapping[str, str]) -> FrozenDict:
    for name, value in state.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("cursor state maps strings to strings")
    return FrozenDict(state)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_json(value: object, path: str = "metadata") -> None:
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            _require_json(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _require_json(item, f"{path}[{index}]")
        return
    raise ValueError(f"{path} must be JSON-compatible, got {type(value).__name__}")


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True, slots=True, order=True)
class DocKey:
    """Stable identity of one source item: ``(source_id, kind, external_id)``.

    ``source_id`` is the connection the item came from, ``kind`` names the
    record type (``jira_issue``, ``github_file``), and ``external_id`` is the
    source's own stable ID, never a URL or a renameable key.
    """

    source_id: str
    kind: str
    external_id: str

    def __post_init__(self) -> None:
        _require_text(self.source_id, "source_id")
        _require_text(self.kind, "kind")
        _require_text(self.external_id, "external_id")

    def __str__(self) -> str:
        return "/".join(
            quote(part, safe="") for part in (self.source_id, self.kind, self.external_id)
        )

    @classmethod
    def parse(cls, text: str) -> DocKey:
        """Invert :meth:`__str__`."""

        parts = text.split("/")
        if len(parts) != 3:
            raise ValueError(f"not a document key: {text!r}")
        source_id, kind, external_id = (unquote(part) for part in parts)
        return cls(source_id, kind, external_id)

    def sibling(self, kind: str, external_id: str) -> DocKey:
        """Return a key for another item of the same source."""

        return DocKey(self.source_id, kind, external_id)


@dataclass(frozen=True, slots=True)
class SyncWindow:
    """The slice of source time one run reads, plus committed cursor tokens.

    ``start`` is inclusive with overlap already applied; ``end`` is exclusive
    and already pulled back by the consistency lag. Token sources (delta
    links, head SHAs) ignore the timestamps and read ``state`` instead.
    """

    start: datetime
    end: datetime
    state: Mapping[str, str] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        _require_aware(self.start, "window start")
        _require_aware(self.end, "window end")
        if self.start > self.end:
            raise ValueError("window start must not be after its end")
        object.__setattr__(self, "state", _frozen_state(self.state))

    @classmethod
    def initial(cls, end: datetime, state: Mapping[str, str] | None = None) -> SyncWindow:
        """Window for a connection that has never committed a cursor."""

        return cls(EPOCH, end, state or {})

    def contains(self, moment: datetime) -> bool:
        """Whether ``moment`` falls inside ``[start, end)``."""

        return self.start <= moment < self.end


@dataclass(frozen=True, slots=True)
class BlobRef:
    """Pointer to source bytes the runtime fetches later through ``open_blob``.

    ``locator`` is opaque to everyone but the connector that produced it.
    """

    locator: str
    filename: str | None = None
    size: int | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.locator, "blob locator")
        if self.size is not None and self.size < 0:
            raise ValueError("blob size must not be negative")


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One readable source item, with a deterministic Markdown body.

    ``children``, when set, is the item's *complete* set of child keys; the
    runtime diffs it against stored children and tombstones the ones that
    disappeared. ``None`` means the connector makes no claim about children.
    """

    key: DocKey
    parent: DocKey | None
    title: str
    body: str
    version: str
    updated_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=FrozenDict)
    blob: BlobRef | None = None
    children: frozenset[DocKey] | None = None

    def __post_init__(self) -> None:
        _require_text(self.version, "record version")
        _require_aware(self.updated_at, "record updated_at")
        if self.parent is not None and self.parent.source_id != self.key.source_id:
            raise ValueError("a record's parent must belong to the same source")
        if self.parent == self.key:
            raise ValueError("a record cannot be its own parent")
        if self.children is not None:
            children = frozenset(self.children)
            if any(child.source_id != self.key.source_id for child in children):
                raise ValueError("a record's children must belong to the same source")
            object.__setattr__(self, "children", children)
        _require_json(self.metadata)
        object.__setattr__(self, "metadata", _deep_freeze(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class Tombstone:
    """The source says this item no longer exists."""

    key: DocKey


@dataclass(frozen=True, slots=True)
class TimeRange:
    """A half-open ``[start, end)`` slice of source time."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _require_aware(self.start, "range start")
        _require_aware(self.end, "range end")
        if self.start > self.end:
            raise ValueError("range start must not be after its end")


@dataclass(frozen=True, slots=True)
class ItemFailure:
    """One item, or one slice of time, could not be read.

    Failures are values: the connector yields them and keeps going. ``reason``
    is a short machine code (``download_failed``, ``too_large``); ``detail`` is
    for people. A ``skipped`` item was excluded on purpose (size, type) and is
    never retried.
    """

    reason: str
    detail: str = ""
    retryable: bool = True
    key: DocKey | None = None
    range: TimeRange | None = None
    skipped: bool = False

    def __post_init__(self) -> None:
        _require_text(self.reason, "failure reason")
        if (self.key is None) == (self.range is None):
            raise ValueError("an item failure names exactly one of key or range")
        if self.skipped:
            object.__setattr__(self, "retryable", False)


@dataclass(frozen=True, slots=True)
class CheckpointMark[CT: BaseModel]:
    """Everything yielded before this mark may be made durable, then resumed from.

    ``state`` proposes committed cursor tokens (delta links, head SHAs). The
    runtime merges proposals in order and commits them only after the whole
    run has settled.
    """

    checkpoint: CT
    state: Mapping[str, str] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _frozen_state(self.state))


type ConnectorEvent[CT: BaseModel] = SourceRecord | Tombstone | ItemFailure | CheckpointMark[CT]
