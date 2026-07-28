"""Unit tests for shared parser utility helpers."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_html_to_text_bytes_and_entities() -> None:
    from harborrag_adapters.parsers.common.normalization import html_to_text

    out = html_to_text(b"<p>Hello&amp;bye</p><script>x()</script>")
    assert "Hello&bye" in out
    assert "x()" not in out


def test_fallback_html_parser_used_when_bs4_absent(monkeypatch) -> None:
    import builtins
    from typing import Any

    from harborrag_adapters.parsers.common import normalization as text_extraction

    real_import = builtins.__import__
    fallback_closed = False

    class TrackingFallbackParser(text_extraction._FallbackHTMLTextParser):
        def close(self) -> None:
            nonlocal fallback_closed
            fallback_closed = True
            super().close()

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "bs4":
            raise ImportError
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(text_extraction, "_FallbackHTMLTextParser", TrackingFallbackParser)
    text, engine = text_extraction.html_to_text_with_engine(
        "<div>A</div><p>B</p><script>hide()</script>"
    )
    assert engine == "python/html.parser"
    assert "A" in text
    assert "B" in text
    assert "hide()" not in text
    assert fallback_closed is True


def test_wrap_parse_errors_passthrough_and_normalize() -> None:
    from harborrag_adapters.parsers.common.validation import wrap_parse_errors
    from harborrag_adapters.parsers.errors import ParseError

    with pytest.raises(ParseError, match="already"), wrap_parse_errors("eng"):
        raise ParseError("already")

    with pytest.raises(ParseError, match="eng failed"), wrap_parse_errors("eng"):
        raise KeyError("boom")


def test_guard_input_size_ok_and_over() -> None:
    from harborrag_adapters.parsers.common.validation import guard_input_size
    from harborrag_adapters.parsers.errors import ParseError

    assert guard_input_size(b"abc", max_bytes=10) == b"abc"
    with pytest.raises(ParseError, match="max_input_bytes"):
        guard_input_size(b"abcdef", max_bytes=3)


def test_open_guarded_zip_member_count_limit(monkeypatch) -> None:
    import io
    import zipfile

    from harborrag_adapters.parsers.common import validation as archive_safety
    from harborrag_adapters.parsers.errors import ParseError

    monkeypatch.setattr(archive_safety, "MAX_ARCHIVE_MEMBERS", 2)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for i in range(3):
            archive.writestr(f"f{i}.txt", "hi")
    with pytest.raises(ParseError, match="members"):
        archive_safety.open_guarded_zip(buffer.getvalue())
