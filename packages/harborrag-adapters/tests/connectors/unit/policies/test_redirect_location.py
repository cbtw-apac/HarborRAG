"""Redirect targets keep the bytes the server sent, whatever their encoding."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.policies.network import redirect_location

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("https://files.example.com/Q3%20review.pdf", "https://files.example.com/Q3%20review.pdf"),
        (
            "https://files.example.com/Q3\xc2\xa0review.pdf",
            "https://files.example.com/Q3\u00a0review.pdf",
        ),
        ("https://files.example.com/caf\xe9.pdf", "https://files.example.com/caf%E9.pdf"),
        (
            "https://files.example.com/Q3\u202freview.pdf",
            "https://files.example.com/Q3\u202freview.pdf",
        ),
    ],
    ids=["percent-encoded", "utf8-bytes", "latin1-byte", "already-text"],
)
def test_redirect_location_keeps_the_target_the_server_meant(header: str, expected: str) -> None:
    """UTF-8 bytes decode as UTF-8, other bytes stay percent escapes, and text passes through."""
    assert redirect_location(header) == expected
