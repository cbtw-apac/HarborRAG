"""JSON-safe encoding of sync windows and connector events.

Windows cross Temporal and events are staged durably, so both need a stable
wire form that does not depend on a generic converter's handling of
datetimes, frozensets, or frozen mappings. Datetimes become ISO 8601
strings, keys become their ``DocKey`` string form, and children become a
sorted list so equal records encode to equal JSON.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from .events import (
    BlobRef,
    CheckpointMark,
    DocKey,
    ItemFailure,
    SourceRecord,
    SyncWindow,
    TimeRange,
    Tombstone,
)

type JsonObject = dict[str, Any]
type StagedEvent = SourceRecord | Tombstone | ItemFailure


def _key(value: DocKey | None) -> str | None:
    return None if value is None else str(value)


def _parse_key(value: object) -> DocKey | None:
    return None if value is None else DocKey.parse(str(value))


def _when(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def encode_window(window: SyncWindow) -> JsonObject:
    return {
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "state": dict(window.state),
    }


def decode_window(data: Mapping[str, Any]) -> SyncWindow:
    return SyncWindow(_when(data["start"]), _when(data["end"]), dict(data.get("state") or {}))


def _encode_blob(blob: BlobRef | None) -> JsonObject | None:
    if blob is None:
        return None
    return {
        "locator": blob.locator,
        "filename": blob.filename,
        "size": blob.size,
        "media_type": blob.media_type,
    }


def _decode_blob(data: Mapping[str, Any] | None) -> BlobRef | None:
    if data is None:
        return None
    return BlobRef(data["locator"], data.get("filename"), data.get("size"), data.get("media_type"))


def _encode_record(record: SourceRecord) -> JsonObject:
    children = None
    if record.children is not None:
        children = sorted(str(child) for child in record.children)
    return {
        "type": "record",
        "key": str(record.key),
        "parent": _key(record.parent),
        "title": record.title,
        "body": record.body,
        "version": record.version,
        "updated_at": record.updated_at.isoformat(),
        "metadata": _plain(record.metadata),
        "blob": _encode_blob(record.blob),
        "children": children,
    }


def _decode_record(data: Mapping[str, Any]) -> SourceRecord:
    children = data.get("children")
    key = _parse_key(data["key"])
    assert key is not None
    return SourceRecord(
        key=key,
        parent=_parse_key(data.get("parent")),
        title=data["title"],
        body=data["body"],
        version=data["version"],
        updated_at=_when(data["updated_at"]),
        metadata=data.get("metadata") or {},
        blob=_decode_blob(data.get("blob")),
        children=None if children is None else frozenset(DocKey.parse(c) for c in children),
    )


def _encode_failure(failure: ItemFailure) -> JsonObject:
    window = None
    if failure.range is not None:
        window = {"start": failure.range.start.isoformat(), "end": failure.range.end.isoformat()}
    return {
        "type": "failure",
        "reason": failure.reason,
        "detail": failure.detail,
        "retryable": failure.retryable,
        "key": _key(failure.key),
        "range": window,
        "skipped": failure.skipped,
    }


def _decode_failure(data: Mapping[str, Any]) -> ItemFailure:
    window = data.get("range")
    return ItemFailure(
        reason=data["reason"],
        detail=data.get("detail", ""),
        retryable=bool(data.get("retryable", True)),
        key=_parse_key(data.get("key")),
        range=None if window is None else TimeRange(_when(window["start"]), _when(window["end"])),
        skipped=bool(data.get("skipped", False)),
    )


def encode_event(event: StagedEvent) -> JsonObject:
    """Encode a record, tombstone, or failure for staging."""

    if isinstance(event, SourceRecord):
        return _encode_record(event)
    if isinstance(event, Tombstone):
        return {"type": "tombstone", "key": str(event.key)}
    if isinstance(event, ItemFailure):
        return _encode_failure(event)
    raise TypeError(f"cannot stage {type(event).__name__}")


def decode_event(data: Mapping[str, Any]) -> StagedEvent:
    kind = data.get("type")
    if kind == "record":
        return _decode_record(data)
    if kind == "tombstone":
        key = _parse_key(data["key"])
        assert key is not None
        return Tombstone(key)
    if kind == "failure":
        return _decode_failure(data)
    raise ValueError(f"unknown staged event type {kind!r}")


def encode_mark(mark: CheckpointMark[Any]) -> JsonObject:
    """Encode a checkpoint mark the way a heartbeat stores it."""

    return {
        "type": "checkpoint",
        "checkpoint": mark.checkpoint.model_dump(mode="json"),
        "state": dict(mark.state),
    }


def decode_mark[CT: BaseModel](data: Mapping[str, Any], model: type[CT]) -> CheckpointMark[CT]:
    return CheckpointMark(model.model_validate(data["checkpoint"]), dict(data.get("state") or {}))


def _plain(value: Any) -> Any:
    """Frozen containers back to plain JSON containers."""

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value
