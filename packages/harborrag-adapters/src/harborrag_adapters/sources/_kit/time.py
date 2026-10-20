"""One parser for the timestamps source systems send.

Sources disagree on the ISO 8601 dialect: ``Z`` or ``+0000`` offsets, and
anywhere from zero to seven fractional digits. Every value is returned as an
aware UTC datetime. A value with no offset is ambiguous (guessing local time
shifts sync windows), so it is treated as unreadable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


def parse_source_timestamp(value: str | None) -> datetime | None:
    """Parse a source timestamp to aware UTC, or ``None`` if unreadable or naive."""

    if value is None:
        return None
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment.astimezone(UTC)


def epoch_ms(moment: datetime) -> int:
    """Milliseconds since the Unix epoch. Raises ``ValueError`` for a naive datetime."""

    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("epoch_ms needs an aware datetime")
    return (moment - _EPOCH) // _MILLISECOND
