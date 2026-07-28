"""White-box unit tests for shared parser utility helpers."""

from __future__ import annotations

import pytest

from harborrag_adapters.parsers.common.normalization import (
    compact_text,
    html_to_text_with_engine,
)
from harborrag_adapters.parsers.common.validation import (
    DEFAULT_MAX_INPUT_BYTES,
    guard_input_size,
)
from harborrag_adapters.parsers.errors import ParseError

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_guard_input_size_allows_under_cap_and_rejects_over_cap():
    assert guard_input_size(b"small", max_bytes=10) == b"small"
    with pytest.raises(ParseError, match="exceeds max_input_bytes"):
        guard_input_size(b"x" * 11, max_bytes=10)
    # The default cap constant is exposed for callers to reason about.
    assert DEFAULT_MAX_INPUT_BYTES == 512 * 1024 * 1024


def test_compact_text_preserves_paragraph_breaks():
    assert compact_text("  a  \n\n\n  b  \n") == "a\n\nb"
    assert compact_text("\n\n") == ""


def test_html_to_text_with_engine_reports_beautifulsoup_backend():
    text, engine = html_to_text_with_engine("<p>Hello <b>world</b></p>")
    assert text == "Hello\nworld"
    assert engine == "beautifulsoup4/html.parser"
