"""Contract coverage for user-owned conversation identity and listing helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from harborrag_core.ports.conversation import (
    CONVERSATION_CURSOR_ERROR,
    MAX_CONVERSATION_TITLE_LENGTH,
    ConversationIdentity,
    ConversationPage,
    ConversationSummaryRow,
    decode_conversation_cursor,
    encode_conversation_cursor,
    normalize_conversation_title,
)

pytestmark = pytest.mark.unit


@pytest.mark.whitebox
def test_identity_requires_a_user_and_keeps_the_principal_for_audit() -> None:
    identity = ConversationIdentity("ACME", "principal-1", "session-1", "user-1")

    assert identity.user_id == "user-1"
    assert identity.principal_id == "principal-1"
    # Two credentials fronting the same human are the same conversation owner.
    assert ConversationIdentity("ACME", "principal-2", "session-1", "user-1") != identity
    with pytest.raises(TypeError):
        ConversationIdentity("ACME", "principal-1", "session-1")  # type: ignore[call-arg]


@pytest.mark.whitebox
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Quarterly review  ", "Quarterly review"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_titles_are_trimmed_and_blank_titles_become_none(
    raw: str | None, expected: str | None
) -> None:
    assert normalize_conversation_title(raw) == expected


@pytest.mark.whitebox
def test_titles_are_truncated_rather_than_rejected() -> None:
    normalized = normalize_conversation_title("t" * 500)

    assert normalized is not None
    assert len(normalized) == MAX_CONVERSATION_TITLE_LENGTH


@pytest.mark.whitebox
def test_listing_cursors_round_trip_and_reject_garbage() -> None:
    cursor = encode_conversation_cursor("session-1")

    assert "=" not in cursor
    assert decode_conversation_cursor(cursor) == "session-1"
    for malformed in ("not-base64!!", "", encode_conversation_cursor("   ")):
        with pytest.raises(ValueError, match=CONVERSATION_CURSOR_ERROR):
            decode_conversation_cursor(malformed)


@pytest.mark.whitebox
def test_a_page_carries_its_rows_and_next_cursor() -> None:
    now = datetime.now(UTC)
    row = ConversationSummaryRow(
        session_id="session-1",
        kind="chat",
        title=None,
        created_at=now,
        updated_at=now,
        message_count=4,
    )
    page = ConversationPage(conversations=(row,), next_cursor=None)

    assert page.conversations[0].message_count == 4
    assert page.next_cursor is None
