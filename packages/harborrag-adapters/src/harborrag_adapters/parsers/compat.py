"""Explicit migration imports for pre-family parser integrations.

This module is intentionally absent from the package ``__all__``. Applications
should resolve complete parser families through ``HarborParserFactory`` and
``HarborParserRegistry``; these aliases only keep provider-focused tests and
incremental downstream migrations readable.
"""

from harborrag_adapters.parsers.common.base import BaseParser as BaseParser
from harborrag_adapters.parsers.common.utils import (
    PARSER_LOGGER_NAME as PARSER_LOGGER_NAME,
)
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger as get_parser_logger,
)
from harborrag_adapters.parsers.common.utils import (
    parser_log_extra as parser_log_extra,
)
from harborrag_adapters.parsers.document.engines.docx.engine import DocxParser as DocxParser
from harborrag_adapters.parsers.document.engines.epub.engine import EpubParser as EpubParser
from harborrag_adapters.parsers.document.engines.msword.engine import MsWordParser as MsWordParser
from harborrag_adapters.parsers.document.engines.odt.engine import OdtParser as OdtParser
from harborrag_adapters.parsers.errors import (
    ParseError as ParseError,
)
from harborrag_adapters.parsers.errors import (
    PasswordProtectedError as PasswordProtectedError,
)
from harborrag_adapters.parsers.errors import (
    UnsupportedFormatError as UnsupportedFormatError,
)
from harborrag_adapters.parsers.image.engines.ocr.engine import ImageParser as ImageParser
from harborrag_adapters.parsers.markup.engines.html.engine import HtmlParser as HtmlParser
from harborrag_adapters.parsers.markup.engines.markdown.engine import (
    MarkdownParser as MarkdownParser,
)
from harborrag_adapters.parsers.pdf.base import PdfBackend as PdfBackend
from harborrag_adapters.parsers.pdf.config import PdfParserProfile as PdfParserProfile
from harborrag_adapters.parsers.pdf.engines.docling.engine import (
    DoclingBackend as DoclingBackend,
)
from harborrag_adapters.parsers.pdf.engines.docling.engine import (
    DoclingBackendOptions as DoclingBackendOptions,
)
from harborrag_adapters.parsers.pdf.engines.liteparse.engine import (
    LiteParseBackend as LiteParseBackend,
)
from harborrag_adapters.parsers.pdf.engines.liteparse.engine import (
    LiteParseBackendOptions as LiteParseBackendOptions,
)
from harborrag_adapters.parsers.pdf.engines.mineru.engine import (
    MinerUBackend as MinerUBackend,
)
from harborrag_adapters.parsers.pdf.engines.mineru.engine import (
    MinerUBackendOptions as MinerUBackendOptions,
)
from harborrag_adapters.parsers.pdf.engines.paddleocr.engine import (
    PaddleOcrBackend as PaddleOcrBackend,
)
from harborrag_adapters.parsers.pdf.engines.paddleocr.engine import (
    PaddleOcrBackendOptions as PaddleOcrBackendOptions,
)
from harborrag_adapters.parsers.pdf.engines.pymupdf.engine import PyMuPdfBackend as PyMuPdfBackend
from harborrag_adapters.parsers.pdf.models import PdfParseResult as PdfParseResult
from harborrag_adapters.parsers.pdf.parser import PdfParser as PdfParser
from harborrag_adapters.parsers.presentation.engines.msppt.engine import PptParser as PptParser
from harborrag_adapters.parsers.presentation.engines.python_pptx.engine import (
    PptxParser as PptxParser,
)
from harborrag_adapters.parsers.spreadsheet.engines.csv.engine import CsvParser as CsvParser
from harborrag_adapters.parsers.spreadsheet.engines.openpyxl.engine import (
    ExcelParser as ExcelParser,
)
from harborrag_adapters.parsers.structured.engines.json.engine import JsonParser as JsonParser
from harborrag_adapters.parsers.text.engines.plain_text.engine import TextParser as TextParser
