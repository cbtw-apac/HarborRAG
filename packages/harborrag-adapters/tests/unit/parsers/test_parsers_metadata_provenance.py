"""White-box unit tests for BaseParser.metadata_for provenance handling."""

from __future__ import annotations

import pytest
from harborrag_adapters.parsers.utils import parse_metadata
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_parse_metadata_document_cannot_override_computed_provenance():
    metadata = parse_metadata(
        ParseInput(
            content="x",
            filename="trusted.txt",
            content_type="text/plain",
            metadata={
                "filename": "spoofed.exe",
                "content_type": "application/x-evil",
                "extra": "kept",
            },
        )
    )
    assert metadata["filename"] == "trusted.txt"
    assert metadata["content_type"] == "text/plain"
    assert metadata["extra"] == "kept"


def test_parse_metadata_drops_none_values_but_keeps_extra():
    metadata = parse_metadata(
        ParseInput(content="x", filename="a.txt"),
        computed="present",
        skipped=None,
    )
    assert metadata["computed"] == "present"
    assert "skipped" not in metadata
    # content_type is None here, so it must not leak into provenance.
    assert "content_type" not in metadata
