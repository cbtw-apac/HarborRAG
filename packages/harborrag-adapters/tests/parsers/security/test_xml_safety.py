"""XML parser hardening tests for EPUB parsing."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.blackbox


def test_epub_xml_uses_defused_parser() -> None:
    from harborrag_adapters.parsers.document.engines.epub import engine as ebook

    ebook._ensure_defusedxml()
    assert "defusedxml" in ebook._xml_fromstring.__module__


def test_epub_parsing_fails_closed_when_defusedxml_missing(monkeypatch) -> None:
    import builtins

    from harborrag_adapters.parsers.document.engines.epub import engine as ebook
    from harborrag_adapters.parsers.errors import ParseError

    monkeypatch.setattr(ebook, "_xml_fromstring", None)
    monkeypatch.setattr(ebook, "_XmlParseError", None)

    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "defusedxml.ElementTree" or name.startswith("defusedxml"):
            raise ImportError("simulated missing defusedxml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)

    with pytest.raises(ParseError, match="defusedxml"):
        ebook._ensure_defusedxml()
