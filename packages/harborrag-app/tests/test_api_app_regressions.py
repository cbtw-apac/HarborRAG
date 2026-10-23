"""API contracts that accepted or returned more than they should have."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_app.api.routes.sources import SourceUpdateInput

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize("status", ["active", "paused", "error"])
def test_a_known_source_status_is_accepted(status: str) -> None:
    assert SourceUpdateInput(status=status).status == status


@pytest.mark.parametrize("status", ["banana", "ACTIVE", "", "deleted"])
def test_an_unknown_source_status_is_refused(status: str) -> None:
    """The domain type is a Literal on a plain dataclass, so nothing checked it.

    PATCH {"status": "banana"} persisted, and any scheduler keyed on
    active/paused/error then mishandled the row.
    """

    with pytest.raises(ValidationError):
        SourceUpdateInput(status=status)


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
