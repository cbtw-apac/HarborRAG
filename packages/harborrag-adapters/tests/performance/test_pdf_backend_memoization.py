"""PDF backend memoization tests for expensive parser resources."""
from __future__ import annotations

import pytest

from harborrag_adapters.parsers.pdf_engine.base import PdfBackend, PdfParseResult
from harborrag_adapters.parsers.pdf_engine.docling import (
    DoclingBackend,
    DoclingBackendOptions,
)
from harborrag_adapters.parsers.pdf_engine.liteparse import (
    LiteParseBackend,
    LiteParseBackendOptions,
)
from harborrag_adapters.parsers.pdf_engine.parser import PdfParser
from harborrag_core.domain.parser import ParseInput


pytestmark = [pytest.mark.slow, pytest.mark.graybox, pytest.mark.timeout(30)]


_MODEL_BUILDS = 0


class _CountingPdfBackend(PdfBackend):
    name = "counting-fake"

    def __init__(self) -> None:
        self._cached_model = None

    def _model(self):
        if self._cached_model is None:
            global _MODEL_BUILDS
            _MODEL_BUILDS += 1
            self._cached_model = object()
        return self._cached_model

    def parse(self, input: ParseInput) -> PdfParseResult:
        model = self._model()
        assert model is self._cached_model
        return PdfParseResult(
            content="fake extracted pdf content that is long enough",
            engine=self.name,
        )


class _FakeDoclingDocument:
    def export_to_markdown(self, **_kwargs) -> str:
        return "# Injected\n\nFake docling content long enough to be accepted."


class _FakeDoclingConverter:
    def __init__(self) -> None:
        self.convert_calls = 0

    def convert(self, path, **_kwargs):
        self.convert_calls += 1
        return _FakeDoclingDocument()


def test_pdf_parser_reuses_one_backend_instance_across_many_parses() -> None:
    global _MODEL_BUILDS
    _MODEL_BUILDS = 0

    parser = PdfParser(backends=[_CountingPdfBackend()], min_content_chars=5)
    for i in range(300):
        document = parser.parse(
            ParseInput(content=b"%PDF-1.4 fake", filename=f"scan{i}.pdf")
        )
        assert document.parser_name == "pdf"

    assert _MODEL_BUILDS == 1


def test_backend_cache_fields_start_none() -> None:
    assert DoclingBackend()._cached_converter is None
    assert LiteParseBackend()._cached_parser is None


def test_docling_injected_converter_is_reused_by_identity() -> None:
    converter = _FakeDoclingConverter()
    backend = DoclingBackend(DoclingBackendOptions(converter=converter))

    assert backend._converter() is converter
    assert backend._converter() is converter
    assert backend._cached_converter is None

    doc1 = backend.parse(ParseInput(content=b"%PDF-1.4", filename="a.pdf"))
    doc2 = backend.parse(ParseInput(content=b"%PDF-1.4", filename="b.pdf"))
    assert doc1.content and doc2.content
    assert converter.convert_calls == 2


def test_docling_cached_converter_branch_returns_same_identity() -> None:
    backend = DoclingBackend()
    sentinel = object()
    backend._cached_converter = sentinel

    assert backend._converter() is sentinel
    assert backend._converter() is sentinel


def test_liteparse_injected_parser_is_reused_by_identity() -> None:
    class _FakeLiteResult:
        text = "fake liteparse content long enough to accept"
        pages = []

    class _FakeLiteParse:
        def __init__(self) -> None:
            self.parse_calls = 0

        def parse(self, _path):
            self.parse_calls += 1
            return _FakeLiteResult()

    fake = _FakeLiteParse()
    backend = LiteParseBackend(LiteParseBackendOptions(parser=fake))

    assert backend._parser() is fake
    assert backend._parser() is fake
    assert backend._cached_parser is None

    backend.parse(ParseInput(content=b"%PDF-1.4", filename="a.pdf"))
    backend.parse(ParseInput(content=b"%PDF-1.4", filename="b.pdf"))
    assert fake.parse_calls == 2
