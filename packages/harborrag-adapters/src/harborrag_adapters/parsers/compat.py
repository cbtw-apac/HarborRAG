"""Explicit migration imports for pre-family parser integrations.

This module is intentionally absent from the package ``__all__``. Applications
should resolve complete parser families through ``HarborParserFactory`` and
``HarborParserRegistry``; these aliases only keep provider-focused tests and
incremental downstream migrations readable.
"""

from harborrag_adapters.parsers.common.base import BaseParser
from harborrag_adapters.parsers.common.utils import (
    PARSER_LOGGER_NAME,
    get_parser_logger,
    parser_log_extra,
)
from harborrag_adapters.parsers.document.engines.docx.engine import DocxParser
from harborrag_adapters.parsers.document.engines.epub.engine import EpubParser
from harborrag_adapters.parsers.document.engines.odt.engine import OdtParser
from harborrag_adapters.parsers.errors import (
    ParseError,
    PasswordProtectedError,
    UnsupportedFormatError,
)
from harborrag_adapters.parsers.image.engines.ocr.engine import ImageParser
from harborrag_adapters.parsers.markup.engines.html.engine import HtmlParser
from harborrag_adapters.parsers.markup.engines.markdown.engine import MarkdownParser
from harborrag_adapters.parsers.pdf.base import PdfBackend
from harborrag_adapters.parsers.pdf.config import PdfParserProfile
from harborrag_adapters.parsers.pdf.engines.docling.engine import (
    DoclingBackend,
    DoclingBackendOptions,
)
from harborrag_adapters.parsers.pdf.engines.liteparse.engine import (
    LiteParseBackend,
    LiteParseBackendOptions,
)
from harborrag_adapters.parsers.pdf.engines.mineru.engine import (
    MinerUBackend,
    MinerUBackendOptions,
)
from harborrag_adapters.parsers.pdf.engines.paddleocr.engine import (
    PaddleOcrBackend,
    PaddleOcrBackendOptions,
)
from harborrag_adapters.parsers.pdf.engines.pymupdf.engine import PyMuPdfBackend
from harborrag_adapters.parsers.pdf.models import PdfParseResult
from harborrag_adapters.parsers.pdf.parser import PdfParser
from harborrag_adapters.parsers.presentation.engines.python_pptx.engine import PptxParser
from harborrag_adapters.parsers.spreadsheet.engines.csv.engine import CsvParser
from harborrag_adapters.parsers.spreadsheet.engines.openpyxl.engine import ExcelParser
from harborrag_adapters.parsers.structured.engines.json.engine import JsonParser
from harborrag_adapters.parsers.text.engines.plain_text.engine import TextParser

__all__ = [
    "PARSER_LOGGER_NAME",
    "BaseParser",
    "CsvParser",
    "DoclingBackend",
    "DoclingBackendOptions",
    "DocxParser",
    "EpubParser",
    "ExcelParser",
    "HtmlParser",
    "ImageParser",
    "JsonParser",
    "LiteParseBackend",
    "LiteParseBackendOptions",
    "MarkdownParser",
    "MinerUBackend",
    "MinerUBackendOptions",
    "OdtParser",
    "PaddleOcrBackend",
    "PaddleOcrBackendOptions",
    "ParseError",
    "PasswordProtectedError",
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
