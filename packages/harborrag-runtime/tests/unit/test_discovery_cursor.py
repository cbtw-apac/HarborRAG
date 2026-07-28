"""Round-tripping and rejection for the durable discovery cursor (A3).

The runtime cursor wraps an opaque provider cursor plus a replay skip so a
resumed discovery activity can re-request a provider page and drop the records
it already persisted. It is written into Temporal history, so a malformed or
truncated value must fail loudly rather than silently resume at the wrong
offset and duplicate or skip artifacts.
"""

from __future__ import annotations

import base64
import json

import pytest

from harborrag_runtime.temporal.activities.discovery import (
    _CURSOR_PREFIX,
    _decode_runtime_cursor,
    _encode_runtime_cursor,
)


def _wrap(payload: object) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
    return _CURSOR_PREFIX + encoded.decode("ascii").rstrip("=")


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "skip"),
    [
        ("provider-token", 0),
        ("provider-token", 7),
        (None, 0),
        (None, 3),
        ("token/with+base64=chars", 1),
        ("t" * 512, 99),
    ],
)
def test_cursor_round_trips(provider: str | None, skip: int) -> None:
    assert _decode_runtime_cursor(_encode_runtime_cursor(provider, skip)) == (provider, skip)


def test_encoded_cursor_is_url_safe_and_unpadded() -> None:
    cursor = _encode_runtime_cursor("provider-token", 2)

    assert cursor.startswith(_CURSOR_PREFIX)
    assert "=" not in cursor
    assert "+" not in cursor.removeprefix(_CURSOR_PREFIX)
    assert "/" not in cursor.removeprefix(_CURSOR_PREFIX)


def test_encoding_is_deterministic() -> None:
    """Temporal replay requires the same inputs to produce the same cursor."""
    assert _encode_runtime_cursor("token", 4) == _encode_runtime_cursor("token", 4)


# --------------------------------------------------------------------------
# Legacy and absent cursors
# --------------------------------------------------------------------------


def test_an_absent_cursor_starts_at_the_beginning() -> None:
    assert _decode_runtime_cursor(None) == (None, 0)


def test_an_unprefixed_cursor_is_treated_as_a_bare_provider_cursor() -> None:
    """Cursors persisted before the wrapper existed must still resume."""
    assert _decode_runtime_cursor("42") == ("42", 0)
    assert _decode_runtime_cursor("opaque-provider-token") == ("opaque-provider-token", 0)


# --------------------------------------------------------------------------
# Rejections
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cursor",
    [
        _CURSOR_PREFIX + "not-base64!!",
        _CURSOR_PREFIX + base64.urlsafe_b64encode(b"not json").decode("ascii").rstrip("="),
        _CURSOR_PREFIX,
    ],
)
def test_undecodable_cursors_are_rejected(cursor: str) -> None:
    with pytest.raises(ValueError, match="invalid runtime discovery cursor"):
        _decode_runtime_cursor(cursor)


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": 42, "skip": 0},
        {"provider": None},
        {"provider": None, "skip": -1},
        {"provider": None, "skip": "3"},
        {"provider": None, "skip": 1.5},
        {"provider": None, "skip": True},
    ],
)
def test_structurally_invalid_cursor_payloads_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="invalid runtime discovery cursor"):
        _decode_runtime_cursor(_wrap(payload))


def test_a_boolean_skip_is_not_accepted_as_an_integer() -> None:
    """`True` is an int in Python; the guard must still reject it."""
    with pytest.raises(ValueError, match="invalid runtime discovery cursor"):
        _decode_runtime_cursor(_wrap({"provider": "token", "skip": True}))
