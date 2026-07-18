from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from harborrag_adapters.parsers.base import BaseParser
from harborrag_adapters.parsers.csv import CsvParser
from harborrag_adapters.parsers.docx import DocxParser
from harborrag_adapters.parsers.ebook import EpubParser
from harborrag_adapters.parsers.excel import ExcelParser
from harborrag_adapters.parsers.html_engine import HtmlParser
from harborrag_adapters.parsers.image import ImageParser
from harborrag_adapters.parsers.markdown import MarkdownParser
from harborrag_adapters.parsers.pdf_engine import (
    DoclingBackend,
    DoclingBackendOptions,
    LiteParseBackend,
    LiteParseBackendOptions,
    MinerUBackend,
    MinerUBackendOptions,
    PaddleOcrBackend,
    PaddleOcrBackendOptions,
    PdfBackend,
    PdfParser,
    PyMuPdfBackend,
)
from harborrag_adapters.parsers.pptx import PptxParser
from harborrag_adapters.parsers.structured import JsonParser
from harborrag_adapters.parsers.text import TextParser

type ParserFactory = Callable[..., BaseParser[Any, Any]]
type PdfBackendFactory = Callable[..., PdfBackend]
type OptionFactory = Callable[..., object]


@dataclass(frozen=True, slots=True)
class PdfBackendSpec:
    """Constructor metadata for one declaratively configurable PDF backend."""

    factory: PdfBackendFactory
    option_factory: OptionFactory | None = None

    def setting_names(self) -> set[str]:
        """Return YAML option names accepted by this backend."""
        if self.option_factory is None:
            return set()
        return {item.name for item in fields(self.option_factory)}  # type: ignore[arg-type]

    def build(self, settings: Mapping[str, Any]) -> PdfBackend:
        """Construct a backend and its typed options from YAML settings."""
        if self.option_factory is None:
            return self.factory(**settings)
        options = self.option_factory(**settings)
        return self.factory(options)


_PARSER_FACTORIES: Mapping[str, ParserFactory] = {
    "csv": CsvParser,
    "docx": DocxParser,
    "epub": EpubParser,
    "excel": ExcelParser,
    "html": HtmlParser,
    "image": ImageParser,
    "json": JsonParser,
    "markdown": MarkdownParser,
    "pdf": PdfParser,
    "pptx": PptxParser,
    "text": TextParser,
}
_PARSER_SETTING_NAMES: Mapping[str, frozenset[str]] = {
    **{name: frozenset() for name in _PARSER_FACTORIES if name != "pdf"},
    "pdf": frozenset({"min_content_chars", "profile"}),
}
_PDF_BACKENDS: Mapping[str, PdfBackendSpec] = {
    "docling": PdfBackendSpec(DoclingBackend, DoclingBackendOptions),
    "liteparse": PdfBackendSpec(LiteParseBackend, LiteParseBackendOptions),
    "mineru": PdfBackendSpec(MinerUBackend, MinerUBackendOptions),
    "paddleocr": PdfBackendSpec(PaddleOcrBackend, PaddleOcrBackendOptions),
    "pymupdf": PdfBackendSpec(PyMuPdfBackend),
}

PDF_BACKEND_SECRET_FIELDS = frozenset({"password"})
PDF_BACKEND_PYTHON_ONLY_FIELDS = frozenset({"converter", "env", "parser", "pipeline_options"})


def parser_factory(parser: str) -> ParserFactory | None:
    """Return a parser constructor by stable parser name."""
    return _PARSER_FACTORIES.get(parser)


def parser_setting_names(parser: str) -> frozenset[str]:
    """Return YAML settings accepted by a parser constructor."""
    return _PARSER_SETTING_NAMES.get(parser, frozenset())


def supported_parser_names() -> list[str]:
    """Return configurable parser names in stable display order."""
    return sorted(_PARSER_FACTORIES)


def pdf_backend_spec(backend: str) -> PdfBackendSpec | None:
    """Return constructor metadata for a PDF backend name."""
    return _PDF_BACKENDS.get(backend)


def supported_pdf_backend_names() -> list[str]:
    """Return configurable PDF backend names in deterministic order."""
    return sorted(_PDF_BACKENDS)
