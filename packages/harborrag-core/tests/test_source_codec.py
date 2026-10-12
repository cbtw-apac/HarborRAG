from __future__ import annotations

import copy
import dataclasses
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from harborrag_core.sources import (
    BlobRef,
    CheckpointMark,
    Credential,
    DocKey,
    ItemFailure,
    SourceRecord,
    SyncWindow,
    TimeRange,
    Tombstone,
    decode_event,
    decode_mark,
    decode_window,
    encode_event,
    encode_mark,
    encode_window,
)
from harborrag_core.sources.testing import FakeCheckpoint

WHEN = datetime(2026, 10, 12, 9, 30, tzinfo=timezone(timedelta(hours=7)))
KEY = DocKey("conn/1", "jira_issue", "100%")

EVENTS = [
    SourceRecord(
        key=KEY,
        parent=None,
        title="PTC-1",
        body="# PTC-1\n\nbody",
        version="2026-10-12T02:30:00.000+0000",
        updated_at=WHEN,
        metadata={"labels": ["a", "b"], "people": {"assignee": "Ana"}, "n": 1.5, "none": None},
        blob=None,
        children={KEY.sibling("jira_comment", "2"), KEY.sibling("jira_comment", "1")},
    ),
    SourceRecord(
        key=KEY.sibling("jira_attachment", "9"),
        parent=KEY,
        title="cv.pdf",
        body="",
        version="9",
        updated_at=WHEN,
        blob=BlobRef("attachment/9", "cv.pdf", 2048, "application/pdf"),
    ),
    Tombstone(KEY.sibling("jira_comment", "3")),
    ItemFailure("download_failed", "timeout", key=KEY),
    ItemFailure("window_failed", range=TimeRange(WHEN, WHEN + timedelta(hours=1))),
    ItemFailure("too_large", key=KEY, skipped=True),
]


@pytest.mark.parametrize("event", EVENTS, ids=lambda event: type(event).__name__)
def test_events_round_trip_through_json(event: object) -> None:
    encoded = encode_event(event)  # type: ignore[arg-type]
    wire = json.loads(json.dumps(encoded, sort_keys=True))
    assert decode_event(wire) == event


def test_equal_records_encode_to_equal_json() -> None:
    record = EVENTS[0]
    assert isinstance(record, SourceRecord)
    again = dataclasses.replace(record, children=set(record.children or ()))
    assert json.dumps(encode_event(record)) == json.dumps(encode_event(again))


def test_window_and_mark_round_trip() -> None:
    window = SyncWindow(WHEN, WHEN + timedelta(days=1), {"42@main": "abc123"})
    assert decode_window(json.loads(json.dumps(encode_window(window)))) == window
    mark = CheckpointMark(FakeCheckpoint(window_start=WHEN, window_end=WHEN, offset=3), {"a": "b"})
    decoded = decode_mark(json.loads(json.dumps(encode_mark(mark))), FakeCheckpoint)
    assert decoded == mark


def test_values_support_deepcopy_and_asdict() -> None:
    window = SyncWindow(WHEN, WHEN, {"k": "v"})
    for value in (window, EVENTS[0], Credential({"token": "t"}, 1)):
        assert copy.deepcopy(value) == value
        dataclasses.asdict(value)  # type: ignore[call-overload]


def test_unknown_event_types_are_rejected() -> None:
    with pytest.raises(ValueError):
        decode_event({"type": "mystery"})
    with pytest.raises(TypeError):
        encode_event(CheckpointMark(FakeCheckpoint(window_start=WHEN, window_end=WHEN)))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="strings"):
        SyncWindow(WHEN, WHEN, {"k": 1})  # type: ignore[dict-item]
    assert datetime.fromisoformat(encode_window(SyncWindow(WHEN, WHEN))["start"]).tzinfo
    assert UTC
