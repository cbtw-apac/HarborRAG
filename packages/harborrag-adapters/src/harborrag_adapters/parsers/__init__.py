"""Public parser factory, concrete parsers, PDF backends, and parser schemas."""

from harborrag_core.domain.parser import ParsedDocument, ParseInput, ParserFormat

from harborrag_adapters.parsers.base import BaseParser
from harborrag_adapters.parsers.csv import CsvParser
from harborrag_adapters.parsers.docx import DocxParser
from harborrag_adapters.parsers.ebook import EpubParser
from harborrag_adapters.parsers.engine import HarborParser
from harborrag_adapters.parsers.excel import ExcelParser
from harborrag_adapters.parsers.exceptions import ParseError, UnsupportedFormatError
from harborrag_adapters.parsers.html_engine import HtmlParser
from harborrag_adapters.parsers.image import ImageParser
from harborrag_adapters.parsers.markdown import MarkdownParser
from harborrag_adapters.parsers.parser_logging import (
    PARSER_LOGGER_NAME,
    get_parser_logger,
    parser_log_extra,
)
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
    PdfParseResult,
    PdfParserProfile,
    PyMuPdfBackend,
)
from harborrag_adapters.parsers.pptx import PptxParser
from harborrag_adapters.parsers.structured import JsonParser
from harborrag_adapters.parsers.text import TextParser

__all__ = [
    "PARSER_LOGGER_NAME",
    "BaseParser",
    "CsvParser",
    "DoclingBackend",
    "DoclingBackendOptions",
    "DocxParser",
    "EpubParser",
    "ExcelParser",
    "HarborParser",
    "HtmlParser",
    "ImageParser",
    "JsonParser",
    "LiteParseBackend",
    "LiteParseBackendOptions",
    "MarkdownParser",
    "MinerUBackend",
    "MinerUBackendOptions",
    "PaddleOcrBackend",
    "PaddleOcrBackendOptions",
    "ParseError",
    "ParseInput",
    "ParsedDocument",
    "ParserFormat",
    "PdfBackend",
    "PdfParseResult",
    "PdfParser",
    "PdfParserProfile",
    "PptxParser",
    "PyMuPdfBackend",
    "TextParser",
    "UnsupportedFormatError",
    "get_parser_logger",
    "parser_log_extra",
]
