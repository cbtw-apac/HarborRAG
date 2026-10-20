"""Source timestamp parsing: every value comes back aware, in UTC, or None."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from harborrag_adapters.sources._kit.time import epoch_ms, parse_source_timestamp


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-12T09:00:00Z", datetime(2026, 10, 12, 9, tzinfo=UTC)),
        ("2026-10-12T09:00:00.000+0000", datetime(2026, 10, 12, 9, tzinfo=UTC)),  # Jira
        ("2026-10-12T11:00:00.000+0200", datetime(2026, 10, 12, 9, tzinfo=UTC)),
        (
            "2026-10-12T09:00:00.1234567Z",
            datetime(2026, 10, 12, 9, 0, 0, 123456, tzinfo=UTC),
        ),  # Graph
        ("2026-10-12T09:00:00", None),
        ("yesterday", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_source_timestamp(raw: str | None, expected: datetime | None) -> None:
    assert parse_source_timestamp(raw) == expected


def test_epoch_ms() -> None:
    assert epoch_ms(datetime(2026, 10, 12, 9, tzinfo=UTC)) == 1_791_795_600_000
    with pytest.raises(ValueError):
        epoch_ms(datetime(2026, 10, 12, 9))
