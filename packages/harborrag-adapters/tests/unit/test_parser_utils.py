"""Unit tests for shared parser utility helpers."""
from __future__ import annotations

import pytest


pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_html_to_text_bytes_and_entities() -> None:
    from harborrag_adapters.parsers.utils import html_to_text

    out = html_to_text(b"<p>Hello&amp;bye</p><script>x()</script>")
    assert "Hello&bye" in out
    assert "x()" not in out


def test_fallback_html_parser_used_when_bs4_absent(monkeypatch) -> None:
    import builtins

    from harborrag_adapters.parsers import utils

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "bs4":
            raise ImportError("no bs4")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    text, engine = utils.html_to_text_with_engine(
        "<div>A</div><p>B</p><script>hide()</script>"
    )
    assert engine == "python/html.parser"
    assert "A" in text and "B" in text and "hide()" not in text


def test_wrap_parse_errors_passthrough_and_normalize() -> None:
    from harborrag_adapters.parsers.exceptions import ParseError
    from harborrag_adapters.parsers.utils import wrap_parse_errors

    with pytest.raises(ParseError, match="already"):
        with wrap_parse_errors("eng"):
            raise ParseError("already")

    with pytest.raises(ParseError, match="eng failed"):
        with wrap_parse_errors("eng"):
            raise KeyError("boom")


def test_guard_input_size_ok_and_over() -> None:
    from harborrag_adapters.parsers.exceptions import ParseError
    from harborrag_adapters.parsers.utils import guard_input_size

    assert guard_input_size(b"abc", max_bytes=10) == b"abc"
    with pytest.raises(ParseError, match="max_input_bytes"):
        guard_input_size(b"abcdef", max_bytes=3)


def test_open_guarded_zip_member_count_limit(monkeypatch) -> None:
    import io
    import zipfile

    from harborrag_adapters.parsers import utils
    from harborrag_adapters.parsers.exceptions import ParseError

    monkeypatch.setattr(utils, "MAX_ARCHIVE_MEMBERS", 2)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for i in range(3):
            archive.writestr(f"f{i}.txt", "hi")
    with pytest.raises(ParseError, match="members"):
        utils.open_guarded_zip(buffer.getvalue())
