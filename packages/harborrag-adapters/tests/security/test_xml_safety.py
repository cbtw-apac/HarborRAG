"""XML parser hardening tests for EPUB parsing."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.blackbox


def test_epub_xml_uses_defused_parser() -> None:
    from harborrag_adapters.parsers import ebook

    assert "defusedxml" in ebook._xml_fromstring.__module__
