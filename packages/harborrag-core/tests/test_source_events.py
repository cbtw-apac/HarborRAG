from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from harborrag_core.sources import (
    EPOCH,
    BlobRef,
    CheckpointMark,
    DocKey,
    ItemFailure,
    SourceRecord,
    SyncWindow,
    TimeRange,
)

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def _record(**overrides: object) -> SourceRecord:
    values: dict[str, object] = {
        "key": DocKey("conn", "jira_issue", "10001"),
        "parent": None,
        "title": "PTC-1",
        "body": "# PTC-1",
        "version": "1",
        "updated_at": NOW,
    }
    values.update(overrides)
    return SourceRecord(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    [
        DocKey("conn", "github_file", "42:main:docs/a b/ü%.md"),
        DocKey("c/1", "k", "x/y/z"),
    ],
)
def test_doc_key_round_trips_through_its_string_form(key: DocKey) -> None:
    text = str(key)
    assert text.count("/") == 2
    assert DocKey.parse(text) == key


def test_doc_key_rejects_blank_parts_and_malformed_text() -> None:
    with pytest.raises(ValueError):
        DocKey("conn", " ", "1")
    with pytest.raises(ValueError):
        DocKey.parse("only/two")


def test_window_requires_aware_ordered_bounds_and_freezes_state() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SyncWindow(datetime(2026, 1, 1), NOW)  # noqa: DTZ001 - deliberately naive
    with pytest.raises(ValueError, match="after its end"):
        SyncWindow(NOW, NOW - timedelta(seconds=1))
    state = {"repo@main": "abc"}
    window = SyncWindow(NOW - timedelta(hours=1), NOW, state)
    state["repo@main"] = "changed"
    assert window.state["repo@main"] == "abc"
    with pytest.raises(TypeError):
        window.state["x"] = "y"  # type: ignore[index]


def test_window_is_half_open_and_initial_starts_at_epoch() -> None:
    window = SyncWindow(NOW - timedelta(hours=1), NOW)
    assert window.contains(window.start)
    assert not window.contains(window.end)
    assert SyncWindow.initial(NOW).start == EPOCH


def test_record_rejects_cross_source_parent_and_children() -> None:
    with pytest.raises(ValueError, match="parent"):
        _record(parent=DocKey("other", "jira_issue", "1"))
    with pytest.raises(ValueError, match="children"):
        _record(children={DocKey("other", "jira_comment", "1")})
    with pytest.raises(ValueError, match="own parent"):
        _record(parent=DocKey("conn", "jira_issue", "10001"))


def test_record_freezes_json_metadata_and_rejects_other_values() -> None:
    record = _record(metadata={"labels": ["a"], "nested": {"n": 1}}, children=set())
    assert record.children == frozenset()
    with pytest.raises(TypeError):
        record.metadata["labels"].append("b")
    with pytest.raises(ValueError, match="JSON-compatible"):
        _record(metadata={"when": NOW})
    with pytest.raises(ValueError, match="timezone-aware"):
        _record(updated_at=datetime(2026, 1, 1))  # noqa: DTZ001 - deliberately naive


def test_item_failure_names_exactly_one_target_and_skips_are_final() -> None:
    key = DocKey("conn", "jira_issue", "1")
    with pytest.raises(ValueError, match="exactly one"):
        ItemFailure("boom")
    with pytest.raises(ValueError, match="exactly one"):
        ItemFailure("boom", key=key, range=TimeRange(NOW, NOW))
    assert ItemFailure("too_large", key=key, skipped=True).retryable is False
    assert ItemFailure("timeout", range=TimeRange(EPOCH, NOW)).retryable is True


def test_blob_ref_and_checkpoint_mark_validate() -> None:
    with pytest.raises(ValueError):
        BlobRef("")
    with pytest.raises(ValueError):
        BlobRef("x", size=-1)

    class Checkpoint(BaseModel):
        page: int = 0

    mark = CheckpointMark(Checkpoint(page=2), {"drive": "link"})
    assert mark.state == {"drive": "link"}
