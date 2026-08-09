from __future__ import annotations

import pytest

from harborrag_adapters.connectors.attachments import (
    attachment_ids_from_filters,
    select_attachment_payloads,
)


def test_attachment_selection_normalizes_deduplicates_and_filters() -> None:
    selected = attachment_ids_from_filters({"attachment_ids": [" a2 ", "a1", "a2"]})
    payloads = select_attachment_payloads(
        (
            {"id": "a1", "title": "first.md"},
            {"id": "a2", "title": "second.md"},
            {"id": "a3", "title": "third.md"},
        ),
        selected,
    )

    assert selected == ("a2", "a1")
    assert [payload["id"] for payload in payloads] == ["a1", "a2"]


@pytest.mark.parametrize(
    "value",
    [[], [""], 42],
)
def test_attachment_selection_rejects_invalid_values(value) -> None:
    with pytest.raises(ValueError, match="attachment_ids"):
        attachment_ids_from_filters({"attachment_ids": value})
