"""Public parser factory, concrete parsers, PDF backends, and parser schemas."""

from harborrag_adapters.parsers.base import BaseParser
from harborrag_adapters.parsers.ebook import EpubParser
from harborrag_adapters.parsers.engine import HarborParser
from harborrag_adapters.parsers.exceptions import ParseError, UnsupportedFormatError
from harborrag_adapters.parsers.html_engine import HtmlParser
from harborrag_adapters.parsers.image import ImageParser
from harborrag_adapters.parsers.markdown import MarkdownParser
from harborrag_adapters.parsers.mock import MockMarkdownParser
from harborrag_adapters.parsers.office import DocxParser, ExcelParser, PptxParser
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
    PdfParseResult,
    PdfParser,
    PdfParserProfile,
    PyMuPdfBackend,
)
from harborrag_adapters.parsers.structured import CsvParser, JsonParser
from harborrag_adapters.parsers.text import TextParser
from harborrag_core.domain.parser import ParsedDocument, ParserFormat, ParseInput

__all__ = [
    "BaseParser",
    "CsvParser",
    "DocxParser",
    "DoclingBackend",
    "DoclingBackendOptions",
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
    "MockMarkdownParser",
    "PARSER_LOGGER_NAME",
    "PaddleOcrBackend",
    "PaddleOcrBackendOptions",
    "ParsedDocument",
    "ParseError",
    "ParseInput",
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
