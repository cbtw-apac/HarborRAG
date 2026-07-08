"""Composable PDF parser backends used by the top-level PdfParser."""

from harborrag_adapters.parsers.pdf_engine.base import PdfBackend, PdfParseResult
from harborrag_adapters.parsers.pdf_engine.docling import (
    DoclingBackend,
    DoclingBackendOptions,
)
from harborrag_adapters.parsers.pdf_engine.liteparse import (
    LiteParseBackend,
    LiteParseBackendOptions,
)
from harborrag_adapters.parsers.pdf_engine.mineru import (
    MinerUBackend,
    MinerUBackendOptions,
)
from harborrag_adapters.parsers.pdf_engine.paddleocr import (
    PaddleOcrBackend,
    PaddleOcrBackendOptions,
)
from harborrag_adapters.parsers.pdf_engine.parser import PdfParser, PdfParserProfile
from harborrag_adapters.parsers.pdf_engine.pymupdf import PyMuPdfBackend

__all__ = [
    "DoclingBackend",
    "DoclingBackendOptions",
    "LiteParseBackend",
    "LiteParseBackendOptions",
    "MinerUBackend",
    "MinerUBackendOptions",
    "PaddleOcrBackend",
    "PaddleOcrBackendOptions",
    "PdfBackend",
    "PdfParseResult",
    "PdfParser",
    "PdfParserProfile",
    "PyMuPdfBackend",
]
