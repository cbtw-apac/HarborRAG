"""API contracts that accepted or returned more than they should have."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]


def test_settings_are_redacted_before_a_reader_sees_them() -> None:
    """The document is schemaless, so a key can end up in it.

    It was returned verbatim to the lowest role that can reach the route.
    """

    from harborrag_core.security.redaction import redact_mapping

    stored = {
        "webhook_url": "https://hooks.example.com/abc",
        "api_key": "sk-live-abcdefghijklmnop",
        "retention_days": 30,
    }

    redacted = redact_mapping(stored)

    assert redacted["api_key"] == "<redacted>"
    assert redacted["retention_days"] == 30
