from __future__ import annotations

from harborrag_adapters.parsers.document.config import DocumentParserConfig
from harborrag_adapters.parsers.document.engines.docx.config import DocxEngineConfig
from harborrag_adapters.parsers.document.engines.odt.config import OdtEngineConfig
from harborrag_adapters.parsers.image.config import ImageParserConfig
from harborrag_adapters.parsers.image.engines.ocr.config import OcrEngineConfig
from harborrag_adapters.parsers.markup.config import MarkupParserConfig
from harborrag_adapters.parsers.markup.engines.html.config import HtmlEngineConfig
from harborrag_adapters.parsers.markup.engines.markdown.config import (
    MarkdownEngineConfig,
)
from harborrag_adapters.parsers.presentation.config import PresentationParserConfig
from harborrag_adapters.parsers.presentation.engines.python_pptx.config import (
    PythonPptxEngineConfig,
)
from harborrag_adapters.parsers.spreadsheet.config import SpreadsheetParserConfig
from harborrag_adapters.parsers.spreadsheet.engines.csv.config import CsvEngineConfig
from harborrag_adapters.parsers.spreadsheet.engines.openpyxl.config import (
    OpenPyxlEngineConfig,
)
from harborrag_adapters.parsers.structured.config import StructuredParserConfig
from harborrag_adapters.parsers.structured.engines.json.config import JsonEngineConfig
from harborrag_adapters.parsers.text.config import TextParserConfig


def test_parser_family_configuration_defaults_define_deterministic_routing() -> None:
    assert DocumentParserConfig().engine_order == ("docx", "odt", "epub")
    assert isinstance(DocxEngineConfig(), DocxEngineConfig)
    assert isinstance(OdtEngineConfig(), OdtEngineConfig)

    image = ImageParserConfig()
    ocr = OcrEngineConfig()
    assert (image.engine, image.max_pixels) == ("pytesseract", 100_000_000)
    assert (ocr.provider, ocr.timeout, ocr.max_pixels) == (
        "pytesseract",
        60,
        100_000_000,
    )

    assert MarkupParserConfig().engine_order == ("html", "markdown")
    assert HtmlEngineConfig().preserve_links is False
    assert MarkdownEngineConfig().preserve_code_blocks is True
    assert PresentationParserConfig().engine == "python_pptx"
    assert PythonPptxEngineConfig().include_notes is True

    assert SpreadsheetParserConfig().engine_order == ("excel", "csv")
    assert CsvEngineConfig().delimiter is None
    assert OpenPyxlEngineConfig().data_only is False
    assert StructuredParserConfig().engine_order == ("json",)
    assert JsonEngineConfig().max_flatten_depth == 200
    assert TextParserConfig().engine == "text"
